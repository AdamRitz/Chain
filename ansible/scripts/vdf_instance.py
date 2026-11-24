"""Create one explicitly configured new chain service, retaining its data on failure."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import time

from vdf_build import Hash, SafePath, Save, Sources


SYSTEMD_ROOT = Path('/etc/systemd/system')


def Run(*arguments, check=True):
    return subprocess.run(list(arguments), check=check, capture_output=True, text=True, timeout=70)


def Quote(value, command=False):
    value = str(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('Control characters are not accepted in service arguments')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def WorkingDirectory(value):
    """Encode systemd's single path value, whose parser does not unquote it."""
    value = str(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('Control characters are not accepted in the working directory')
    # config_parse_working_directory expands specifiers but does not perform
    # token splitting or C unescaping. Quotes and backslashes remain literal.
    value = value.replace('%', '%%')
    # The common config parser strips trailing whitespace and treats a final
    # backslash as line continuation. A final /. names the same directory.
    if value.endswith((' ', '\\')):
        value += '/.'
    return value


def Head(host, port):
    def Read(connection, count):
        result = bytearray()
        while len(result) < count:
            chunk = connection.recv(count - len(result))
            if not chunk:
                raise ConnectionError('Node head response ended early')
            result.extend(chunk)
        return bytes(result)
    with socket.create_connection((host, port), timeout=2) as connection:
        connection.settimeout(2)
        connection.sendall(b'\x02' + struct.pack('<BI', 19, 0))
        kind, size = struct.unpack('<BI', Read(connection, 5))
        if kind != 20 or size != 80:
            raise ValueError('Unexpected published-head response')
        data = Read(connection, 80)
    height, transactions = struct.unpack_from('<QQ', data, 64)
    return {'genesis': data[:32].hex(), 'hash': data[32:64].hex(),
            'height': height, 'transactions': transactions}


def Validate(request):
    for key in ('instance_id', 'node_id'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request[key]):
            raise ValueError('Invalid instance/node identifier')
    if not re.fullmatch(r'chain-vdf-[A-Za-z0-9_-]{1,100}\.service', request['service']):
        raise ValueError('Use a distinct chain-vdf-<name>.service')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', request['user']) or request['user'] == 'root':
        raise ValueError('Use a dedicated unprivileged service user')
    release = SafePath(request['release_dir'])
    root = SafePath(request['instance_dir'])
    if len(root.parts) < 4 or root.is_relative_to(release) or release.is_relative_to(root):
        raise ValueError('Use a separate persistent instance directory')
    paths = {name: SafePath(request[name]) for name in ('data_dir', 'config_path', 'genesis_path', 'metrics_path')}
    if any(path == root or not path.is_relative_to(root) for path in paths.values()):
        raise ValueError('All instance paths must be strictly below its new directory')
    if len(set(paths.values())) != 4:
        raise ValueError('Instance paths must be distinct')
    if any(path.is_relative_to(paths['data_dir']) for name, path in paths.items() if name != 'data_dir'):
        raise ValueError('Configuration and logs cannot be inside the database directory')
    # Parent directories for metrics and database are writable by the node; configuration is not.
    if paths['metrics_path'].parent == root or paths['data_dir'].parent != root:
        raise ValueError('Use a direct data child and a separate metrics subdirectory')
    if any(paths[name].is_relative_to(paths['metrics_path'].parent) for name in ('data_dir', 'config_path', 'genesis_path')):
        raise ValueError('Metrics must use a separate writable directory')
    port = request['port']
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid explicit service port')
    arguments = request['arguments']
    if not isinstance(arguments, list) or any(not isinstance(value, str) for value in arguments) or len(arguments) % 2:
        raise ValueError('Node arguments must be an array of option/value string pairs')
    reserved = {'--config', '--data', '--genesis', '--metrics', '--port', '--run-seconds', '--stop-file'}
    if any(name in reserved or not name.startswith('--') for name in arguments[::2]):
        raise ValueError('Extra arguments cannot replace managed paths, port or lifetime')
    for value in arguments:
        Quote(value, command=True)
    if not isinstance(request['config_text'], str) or not request['config_text'].strip():
        raise ValueError('An explicit node configuration is required')
    if not isinstance(request['genesis'], dict):
        raise ValueError('An explicit genesis object is required')
    if request.get('expected_genesis') and not re.fullmatch(r'[0-9a-f]{64}', request['expected_genesis']):
        raise ValueError('Invalid expected genesis digest')
    if type(request.get('health_timeout', 45)) is not int or not 1 <= request.get('health_timeout', 45) <= 300:
        raise ValueError('Health timeout must be between 1 and 300 seconds')
    proof = json.loads(SafePath(release / 'build-proof.json').read_text(encoding='utf-8'))
    binary = SafePath(release / 'build' / 'boost')
    if (proof.get('ok') is not True or proof.get('complete') is not True or proof.get('binary_sha256') != Hash(binary) or
            proof.get('source_manifest_sha256') != Sources(release)):
        raise ValueError('Release no longer matches its successful build/test proof')
    argv = [str(binary), '--config', str(paths['config_path']), '--data', str(paths['data_dir']),
            '--genesis', str(paths['genesis_path']), '--metrics', str(paths['metrics_path']),
            '--port', str(port), *arguments]
    contents = {str(paths['config_path']): request['config_text'].encode('utf-8'),
                str(paths['genesis_path']): (json.dumps(request['genesis'], sort_keys=True, indent=2) + '\n').encode('utf-8')}
    library_path = proof['settings'].get('library_path', '')
    unit = ('[Unit]\nDescription=Chain VDF ' + request['instance_id'] + '\n'
            'After=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\n'
            'User=' + request['user'] + '\nGroup=' + request['user'] + '\n'
            'WorkingDirectory=' + WorkingDirectory(root) + '\n'
            'ExecStart=' + ' '.join(Quote(value, command=True) for value in argv) + '\n'
            'Environment=' + Quote('LD_LIBRARY_PATH=' + library_path) + '\n'
            'Restart=on-failure\nRestartSec=3\nTimeoutStopSec=60\nKillMode=control-group\n'
            'LimitNOFILE=65536\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\n'
            'ReadWritePaths=' + Quote(paths['data_dir']) + ' ' + Quote(paths['metrics_path'].parent) + '\n'
            '\n[Install]\nWantedBy=multi-user.target\n').encode('utf-8')
    return root, paths, argv, contents, unit, proof


def Process(request, argv):
    import psutil
    pid = int(Run('systemctl', 'show', request['service'], '--property=MainPID', '--value').stdout.strip())
    if pid <= 0:
        raise RuntimeError('The new service has no running main process')
    process = psutil.Process(pid)
    if str(Path(process.exe()).resolve()) != argv[0] or process.cmdline() != argv:
        raise RuntimeError('The active service does not match its executable and complete arguments')
    connections = getattr(process, 'net_connections', None) or process.connections
    if not any(connection.status == psutil.CONN_LISTEN and connection.laddr.port == request['port']
               for connection in connections(kind='tcp')):
        raise RuntimeError('The expected process does not own its listening port')
    return {'pid': pid, 'created': process.create_time(), 'binary': process.exe(), 'argv': argv}


def Health(request, argv):
    deadline = time.monotonic() + request.get('health_timeout', 45)
    last = None
    while time.monotonic() < deadline:
        try:
            process = Process(request, argv)
            head = Head(request.get('health_host', '127.0.0.1'), request['port'])
            if request.get('expected_genesis') and head['genesis'] != request['expected_genesis']:
                raise RuntimeError('Published genesis differs from the requested chain')
            time.sleep(1)
            if Process(request, argv)['created'] != process['created']:
                raise RuntimeError('Service restarted during its health check')
            return {'process': process, 'head': head}
        except Exception as error:
            last = str(error)
            time.sleep(.25)
    raise RuntimeError('Service health check failed: ' + str(last))


def Deploy(request):
    root, paths, argv, contents, unit, proof = Validate(request)
    state_path = root / 'deployment.json'
    unit_path = SafePath(SYSTEMD_ROOT / request['service'])
    identity = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if root.exists():
        state = json.loads(SafePath(state_path).read_text(encoding='utf-8'))
        if state.get('request_sha256') != identity or state.get('state') != 'active':
            raise ValueError('Existing instance is different or incomplete; preserve it and use a new instance ID')
        if not unit_path.is_file() or unit_path.read_bytes() != unit:
            raise ValueError('Existing service unit has changed')
        for name, data in contents.items():
            if SafePath(name).read_bytes() != data:
                raise ValueError('Existing genesis/configuration has changed; refusing replacement')
        return {'ok': True, 'changed': False, 'state': 'active', 'health': Health(request, argv), 'proof': str(state_path)}
    if unit_path.exists() or Run('systemctl', 'show', request['service'], '--property=LoadState', '--value').stdout.strip() != 'not-found':
        raise ValueError('The requested service already exists')
    # Check before making an instance; do not stop any current port owner.
    with socket.socket(socket.AF_INET6 if ':' in request.get('health_host', '') else socket.AF_INET) as probe:
        probe.bind((request.get('health_host', '127.0.0.1'), request['port']))
    import pwd
    account = pwd.getpwnam(request['user'])
    root.parent.mkdir(parents=True, exist_ok=True)
    SafePath(root.parent)
    root.mkdir(mode=0o755)
    state = {'state': 'preparing', 'ok': False, 'request_sha256': identity,
             'service': request['service'], 'instance_dir': str(root), 'argv': argv,
             'binary_sha256': proof['binary_sha256'], 'started_unix': time.time(),
             'files': {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
             'unit_sha256': hashlib.sha256(unit).hexdigest()}
    Save(state_path, state)
    unit_created = False
    try:
        for directory in (paths['data_dir'], paths['metrics_path'].parent):
            directory.mkdir(parents=True, mode=0o750)
            os.chown(directory, account.pw_uid, account.pw_gid)
        for name, data in contents.items():
            path = SafePath(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as output:
                output.write(data)
            path.chmod(0o644)
        Run('runuser', '-u', request['user'], '--', 'test', '-x', argv[0])
        for name in contents:
            Run('runuser', '-u', request['user'], '--', 'test', '-r', name)
        with unit_path.open('xb') as output:
            output.write(unit)
        unit_path.chmod(0o644)
        unit_created = True
        Run('systemctl', 'daemon-reload')
        state['state'] = 'starting'
        Save(state_path, state)
        Run('systemctl', 'enable', '--now', request['service'])
        health = Health(request, argv)
        state.update(state='active', ok=True, health=health, finished_unix=time.time())
        Save(state_path, state)
        return {'ok': True, 'changed': True, 'state': 'active', 'health': health, 'proof': str(state_path)}
    except Exception as error:
        recovery = {'attempted': unit_created, 'stopped': False, 'unit_removed': False}
        if unit_created:
            try:
                journal = Run('journalctl', '-u', request['service'], '--no-pager', '--output=short-iso', '-n', '200')
                journal_path = SafePath(root / 'service-journal.txt')
                with journal_path.open('x', encoding='utf-8') as output:
                    output.write(journal.stdout)
                state['journal'] = {'path': str(journal_path), 'sha256': Hash(journal_path)}
            except Exception as journal_error:
                state['journal_error'] = str(journal_error)
        if unit_created:
            try:
                if unit_path.read_bytes() != unit:
                    raise RuntimeError('Service unit changed; refusing to manage another definition')
                Run('systemctl', 'disable', '--now', request['service'])
                active = Run('systemctl', 'is-active', request['service'], check=False).stdout.strip()
                pid = Run('systemctl', 'show', request['service'], '--property=MainPID', '--value').stdout.strip()
                if active not in ('inactive', 'failed') or pid != '0':
                    raise RuntimeError('Could not prove that the newly created service stopped')
                recovery['stopped'] = True
                unit_path.unlink()
                recovery['unit_removed'] = True
                Run('systemctl', 'daemon-reload')
            except Exception as restore_error:
                recovery['error'] = str(restore_error)
        state.update(state='failed', error=str(error), recovery=recovery, finished_unix=time.time())
        Save(state_path, state)
        raise RuntimeError(f'{error}; recovery={recovery}; retained instance={root}') from error


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    try:
        request = json.loads(SafePath(args.request).read_text(encoding='utf-8'))
        print(json.dumps(Deploy(request)))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        raise SystemExit(1)


if __name__ == '__main__':
    Main()
