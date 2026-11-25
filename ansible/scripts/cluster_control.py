"""Control only services with matching retained Chain deployment records."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
import vdf_instance as instance
from vdf_build import Hash, SafePath


def Validate(request):
    root, paths, argv, contents, unit, proof = instance.Validate(request)
    state = json.loads(SafePath(root / 'deployment.json').read_text(encoding='utf-8'))
    identity = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    unit_path = SafePath(instance.SYSTEMD_ROOT / request['service'])
    if state.get('request_sha256') != identity or state.get('state') != 'active' or unit_path.read_bytes() != unit:
        raise ValueError('Service does not match its successful deployment record')
    for name, data in contents.items():
        if SafePath(name).read_bytes() != data:
            raise ValueError('Persistent genesis/configuration has changed')
    return argv


def Status(request, argv):
    import psutil
    raw = instance.Run('systemctl', 'show', request['service'], '--property=MainPID',
                       '--property=ActiveState', '--property=SubState').stdout
    state = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    pid = int(state['MainPID'])
    result = {'active': False, 'service': request['service'],
              'active_state': state['ActiveState'], 'sub_state': state['SubState']}
    if not pid:
        return result
    try:
        process = psutil.Process(pid)
        actual = process.cmdline()
        executable = process.exe()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return {**result, 'process_exited': True}
    if len(actual) != len(argv) or actual[1:] != argv[1:]:
        raise ValueError('Running service arguments differ from the owned instance')
    binary = SafePath(actual[0])
    if str(Path(executable).resolve()) != str(binary):
        raise ValueError('Main PID executable differs from its command')
    if actual[0] != argv[0]:
        # Existing deploy.yml may switch only the executable to another staged release.
        from stage_release import Validate as ValidateRelease
        release = binary.parent.parent
        if request.get('release_format') != 'prebuilt' or release.parent != Path(request['release_dir']).parent or binary.name != 'boost' or binary.parent.name != 'bin':
            raise ValueError('Running executable is outside the owned release directory')
        ValidateRelease(release)
        if not SafePath(release / '.chain-release.json').is_file():
            raise ValueError('Running release was not staged')
    result.update(active=True, pid=pid, binary_sha256=Hash(binary))
    try:
        result['head'] = instance.Head(request.get('health_host', '127.0.0.1'), request['port'])
    except (OSError, ValueError, ConnectionError) as error:
        result['head_error'] = str(error)
    return result


def Control(request, action):
    if action not in ('status', 'start', 'stop', 'collect'):
        raise ValueError('Unsupported cluster service action')
    argv = Validate(request)
    before = Status(request, argv)
    changed = False
    if action == 'start' and (not before['active'] or 'head' not in before):
        if not before['active']:
            instance.Run('systemctl', 'start', request['service'])
            changed = True
        deadline = time.monotonic() + request.get('health_timeout', 45)
        while True:
            try:
                before = Status(request, argv)
                if before['active'] and 'head' in before:
                    break
            except (OSError, ConnectionError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Owned service did not become ready')
            time.sleep(.2)
    if action == 'stop':
        changed = before['active'] or before.get('active_state', 'inactive') not in ('inactive', 'failed')
        # MainPID can be zero during RestartSec. stop must cancel the restart job.
        instance.Run('systemctl', 'stop', request['service'])
        before = Status(request, argv)
        if before['active'] or before.get('active_state', 'inactive') not in ('inactive', 'failed'):
            raise RuntimeError('Owned service did not stop')
    if action == 'collect':
        before['journal'] = instance.Run('journalctl', '-u', request['service'], '--no-pager', '-n', '200').stdout
        metrics = SafePath(request['metrics_path'])
        if metrics.exists():
            before['metrics'] = json.loads(metrics.read_text(encoding='utf-8'))
    return {'ok': True, 'changed': changed, **before}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True)
    parser.add_argument('--action', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(Control(json.loads(SafePath(args.request).read_text(encoding='utf-8')), args.action)))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        raise SystemExit(1)
