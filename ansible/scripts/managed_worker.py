"""Ansible lifecycle adapter. ClusterWorker retains process and DB ownership checks."""
import argparse
import errno
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

# Releases are immutable manifest-verified trees. This also covers transitive
# imports such as Netem when loading ClusterWorker and the expiry subprocess.
sys.dont_write_bytecode = True


def Identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError('Invalid run, group or node identifier')
    return value


def Root(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() == Path(path.anchor):
        raise ValueError('The test root must be an absolute managed directory')
    return path.resolve()


def Save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def Owner(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('A per-run UUID owner token is required')
    return value


def Authorize(ledger, root, run, node, group, owner, release, ledger_file, reservation):
    if ledger.get('owner') != owner or ledger.get('run_group') != group or ledger.get('ledger') != str(ledger_file):
        raise ValueError('Lifecycle ledger belongs to another run owner/group')
    original = ledger['request']
    if Root(original['root']) != root or original['run'] != run or original['node'] != node or ledger['release_dir'] != str(release):
        raise ValueError('Lifecycle ledger identity differs from this request')
    if reservation.is_symlink() or not reservation.resolve().is_relative_to(root):
        raise ValueError('Unsafe shared case reservation')
    reserved = json.loads(reservation.read_text())
    if reserved != {'owner': owner, 'run_group': group, 'ledger': str(ledger_file)}:
        raise ValueError('Case directory belongs to another run owner/group')


def RecordedProcess(ledger, case):
    expected = ledger.get('process')
    if not expected:
        raise ValueError('Start did not record an owned process; refusing to signal or delete a preexisting case')
    record_file = case / 'process.json'
    if record_file.is_symlink():
        raise ValueError('Process ownership record must not be a link')
    actual = json.loads(record_file.read_text())
    if any(actual.get(key) != expected.get(key) for key in ('pid', 'created', 'binary', 'command')):
        raise ValueError('Process ownership record changed after the successful start')
    return actual


def Worker(release):
    source = Path(release) / 'source' / 'Test'
    sys.path.insert(0, str(source))
    spec = importlib.util.spec_from_file_location('ChainAnsibleClusterWorker', source / 'ClusterWorker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def WaitBindablePort(address, port, timeout=70, socket_factory=socket.socket,
                     clock=time.monotonic, sleep=time.sleep):
    address = ipaddress.ip_address(address)
    if port == 8089 or not 1024 <= port <= 65535 or not 0 < timeout <= 70:
        raise ValueError('Port wait requires an isolated test port and a timeout of at most 70 seconds')
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    started = clock()
    deadline = started + timeout
    while True:
        try:
            # Match the node's bind without SO_REUSEADDR. This waits out an old
            # listener/TIME_WAIT and neither signals processes nor accepts peers.
            with socket_factory(family, socket.SOCK_STREAM) as probe:
                probe.bind((str(address), port))
            return clock() - started
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError('Isolated test port %s:%s is still occupied after %ss; no process was stopped' %
                                   (address, port, timeout)) from error
            sleep(min(.25, remaining))


def StopGuard(ledger):
    guard = ledger.get('guard')
    if not guard:
        return
    import psutil
    try:
        process = psutil.Process(guard['pid'])
        if abs(process.create_time() - guard['created']) > .01:
            return
        command = process.cmdline()
        if '--expire-ledger' not in command or ledger['ledger'] not in command:
            raise RuntimeError('Guard identity changed; refusing to signal it')
        process.terminate()
        try:
            process.wait(timeout=5)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except psutil.NoSuchProcess:
        pass


def PreserveIdentity(case):
    source, target = case / 'db' / 'vdf-identity.seed', case / 'vdf-identity.seed'
    if not source.exists() and not source.is_symlink():
        return
    if source.parent.is_symlink() or source.is_symlink() or not source.is_file() or source.stat().st_size > 128 or target.is_symlink():
        raise ValueError('Unsafe VDF identity path; database retained')
    data = source.read_bytes()
    if not re.fullmatch(rb'[0-9a-fA-F]{64}\s*', data):
        raise ValueError('Invalid VDF identity; database retained')
    if target.exists():
        if not target.is_file() or target.read_bytes() != data:
            raise ValueError('Retained VDF identity differs; database retained')
        target.chmod(0o600)
        return
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def Expire(path, seconds, pid, created):
    time.sleep(seconds)
    ledger = json.loads(Path(path).read_text())
    record_file = Path(ledger['request']['root']) / ledger['request']['run'] / ledger['request']['node'] / 'process.json'
    if record_file.exists():
        record = json.loads(record_file.read_text())
        expected = ledger.get('process', {})
        if record['pid'] == expected.get('pid') == pid and abs(record['created'] - created) <= .01 and record.get('command') == expected.get('command'):
            result = Worker(ledger['release_dir']).Handle({**ledger['request'], 'action': 'stop'})
            Save(Path(path).with_suffix('.expired.json'), result)


def One(request):
    action = request['action']
    if action not in ('start', 'stop', 'collect', 'delete-db', 'export-headers'):
        raise ValueError('Unsupported lifecycle action')
    root = Root(request['root'])
    run, node, group = (Identifier(request[key]) for key in ('run', 'node', 'run_group'))
    owner = Owner(request['owner'])
    release_root = Root(request['release_root'])
    release = Path(request['release_dir']).resolve()
    if release.parent != release_root or not (release / '.chain-release.json').is_file():
        raise ValueError('Worker must use a verified immutable release')
    ledger_file = root / '.ansible-runs' / group / f'{run}--{node}.json'
    if ledger_file.is_symlink() or not ledger_file.resolve().is_relative_to(root):
        raise ValueError('Unsafe lifecycle ledger path')
    case = root / run / node
    if case.resolve() != case or case.is_symlink():
        raise ValueError('Case path contains a symbolic link')
    reservation = root / '.ansible-case-owners' / f'{run}--{node}.json'
    work = {'root': str(root), 'run': run, 'node': node, 'action': action}
    worker = Worker(release)
    if action == 'start':
        restart = bool(request.get('restart', False))
        if not restart and (case.exists() or ledger_file.exists() or reservation.exists()):
            raise FileExistsError('Case or ownership ledger already exists; refusing to adopt it')
        if restart:
            ledger = json.loads(ledger_file.read_text())
            Authorize(ledger, root, run, node, group, owner, release, ledger_file, reservation)
            if worker.FindProcess(RecordedProcess(ledger, case)) is not None:
                raise RuntimeError('Stop the owned node before restarting its database')
        arguments = list(map(str, request['arguments']))
        for value in arguments:
            if any(value == option or value.startswith(option + '=') for option in ('--data', '--genesis', '--metrics')):
                raise ValueError('Worker owns data, genesis and metrics paths')
        port = int(request.get('test_port', 18089))
        if port == 8089 or not 1024 <= port <= 65535 or arguments.count('--port') != 1 or any(value.startswith('--port=') for value in arguments):
            raise ValueError('An isolated non-production test port is required')
        if int(arguments[arguments.index('--port') + 1]) != port:
            raise ValueError('Node arguments do not use the isolated test port')
        if arguments.count('--bind') > 1 or any(value.startswith('--bind=') for value in arguments):
            raise ValueError('Node arguments must use at most one explicit --bind address')
        bind_index = arguments.index('--bind') + 1 if '--bind' in arguments else None
        if bind_index is not None and bind_index >= len(arguments):
            raise ValueError('Missing node bind address')
        bind_address = str(ipaddress.ip_address(arguments[bind_index] if bind_index is not None else '0.0.0.0'))
        lifetime = int(request.get('guard_seconds', 300))
        if not 30 <= lifetime <= 3600:
            raise ValueError('Guard lifetime must be between 30 and 3600 seconds')
        work.update(binary=str(release / 'bin' / 'boost'), genesis=request['genesis'], arguments=arguments,
                    restart=restart)
        # Readiness precedes process creation and the measured load window. A
        # timeout leaves a fresh case unclaimed and an audit restart ledger intact.
        port_probe = dict(address=bind_address, port=port, timeout_seconds=70,
                          wait_seconds=WaitBindablePort(bind_address, port))
        if not restart:
            reservation.parent.mkdir(parents=True, exist_ok=True)
            if not reservation.resolve().is_relative_to(root):
                raise ValueError('Shared case reservation escapes the test root')
            # Different run groups still share this reservation, preventing a race for one case path.
            with reservation.open('x', encoding='utf-8') as output:
                json.dump({'owner': owner, 'run_group': group, 'ledger': str(ledger_file)}, output)
        # Persist intent before starting so controller interruption retains a cleanup target.
        ledger = {'schema': 1, 'owner': owner, 'run_group': group, 'ledger': str(ledger_file), 'release_dir': str(release),
                  'request': work, 'port_bind_probe': port_probe}
        Save(ledger_file, ledger)
        result = worker.Handle(work)
        ledger['process'] = result
        Save(ledger_file, ledger)
        with ledger_file.with_suffix('.guard.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--expire-ledger', str(ledger_file),
                                      '--seconds', str(lifetime), '--pid', str(result['pid']), '--created', str(result['created'])],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        import psutil
        ledger['guard'] = {'pid': child.pid, 'created': psutil.Process(child.pid).create_time()}
        Save(ledger_file, ledger)
        return {**result, 'port_bind_probe': port_probe}
    ledger = json.loads(ledger_file.read_text())
    Authorize(ledger, root, run, node, group, owner, release, ledger_file, reservation)
    RecordedProcess(ledger, case)
    if action == 'stop':
        result = worker.Handle(work)
        StopGuard(ledger)
        return result
    if action == 'export-headers':
        library = release / 'lib' / 'librocksdb.so'
        if not library.is_file():
            matches = sorted((release / 'lib').glob('librocksdb.so.*'))
            library = next((path for path in matches if path.is_file()), None)
        if library is None:
            raise ValueError('Release does not contain a RocksDB shared library')
        spec = importlib.util.spec_from_file_location('ChainAnsibleHeaderReader', Path(__file__).with_name('HeaderReader.py'))
        reader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reader)
        return reader.Export({**work, 'library': str(library)}, worker=worker)
    if action == 'delete-db':
        if worker.FindProcess(RecordedProcess(ledger, case)) is not None:
            raise RuntimeError('Stop the experiment node before preserving its identity')
        PreserveIdentity(case)
    return worker.Handle(work)


def Cleanup(request):
    root, group = Root(request['root']), Identifier(request['run_group'])
    owner = Owner(request['owner'])
    directory = root / '.ansible-runs' / group
    results, errors = [], []
    if directory.is_symlink() or not directory.resolve().is_relative_to(root):
        raise ValueError('Cleanup ledger directory must not be a link')
    if directory.exists():
        for path in sorted(directory.glob('*.json')):
            if path.name.endswith(('.expired.json', '.cleanup.json', '.collected.json')):
                continue
            try:
                if path.is_symlink() or path.resolve().parent != directory.resolve():
                    raise ValueError('Unsafe cleanup ledger')
                ledger = json.loads(path.read_text())
                if ledger.get('owner') != owner or ledger.get('run_group') != group:
                    raise ValueError('Cleanup ledger belongs to another owner/group')
                original = ledger['request']
                if Root(original['root']) != root or path.name != f"{Identifier(original['run'])}--{Identifier(original['node'])}.json":
                    raise ValueError('Cleanup ledger identity mismatch')
                common = {**original, 'release_root': request['release_root'], 'owner': owner,
                          'release_dir': ledger['release_dir'], 'run_group': group}
                if not ledger.get('process'):
                    case = root / original['run'] / original['node']
                    if case.exists():
                        raise ValueError('Incomplete start has no process ownership proof; case retained for inspection')
                    results.append({'run': original['run'], 'node': original['node'], 'not_started': True})
                    continue
                stopped = One({**common, 'action': 'stop'})
                collected = One({**common, 'action': 'collect'})
                Save(path.with_suffix('.collected.json'), collected)
                removed = One({**common, 'action': 'delete-db'})
                result = {'run': original['run'], 'node': original['node'], 'stopped': stopped,
                          'collected': collected, 'database': removed}
                Save(path.with_suffix('.cleanup.json'), result)
                results.append(result)
            except Exception as error:
                errors.append({'ledger': str(path), 'error': str(error)})
    return {'ok': not errors, 'results': results, 'errors': errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--expire-ledger')
    parser.add_argument('--seconds', type=int)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--created', type=float)
    args = parser.parse_args()
    if args.expire_ledger:
        Expire(args.expire_ledger, args.seconds, args.pid, args.created)
    else:
        try:
            request = json.load(sys.stdin)
            result = Cleanup(request) if request['action'] == 'cleanup-group' else One(request)
            print(json.dumps({'ok': result.get('ok', True), 'result': result}))
            sys.exit(0 if result.get('ok', True) else 1)
        except Exception as error:
            print(json.dumps({'ok': False, 'error': str(error)}))
            sys.exit(1)
