"""Replace one systemd executable, preserve argv/DB, and roll back failed health checks."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time

SYSTEMD_ROOT = Path('/etc/systemd/system')


def Run(*arguments):
    return subprocess.run(list(arguments), check=True, capture_output=True, text=True, timeout=60).stdout.strip()


def ReadAll(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError('Published-head response ended early')
        data.extend(chunk)
    return bytes(data)


def Head(port):
    with socket.create_connection(('127.0.0.1', port), timeout=2) as connection:
        connection.settimeout(2)
        connection.sendall(b'\x02' + struct.pack('<BI', 19, 0))
        kind, size = struct.unpack('<BI', ReadAll(connection, 5))
        if kind != 20 or size != 80:
            raise ValueError('Unexpected published-head reply')
        data = ReadAll(connection, 80)
    height, transactions = struct.unpack_from('<QQ', data, 64)
    return {'genesis': data[:32].hex(), 'hash': data[32:64].hex(),
            'height': height, 'transactions': transactions}


def Process(service):
    import psutil
    pid = int(Run('systemctl', 'show', service, '--property=MainPID', '--value'))
    if pid <= 0:
        raise RuntimeError('Existing Chain service must be running')
    process = psutil.Process(pid)
    arguments = process.cmdline()
    if arguments.count('--data') != 1 or arguments.index('--data') + 1 >= len(arguments):
        raise ValueError('Running service must have exactly one --data argument')
    data = Path(arguments[arguments.index('--data') + 1])
    if not data.is_absolute() or not data.is_dir():
        raise ValueError('Existing persistent data directory is invalid')
    return {'pid': pid, 'created': process.create_time(), 'binary': str(Path(process.exe()).resolve()),
            'arguments': arguments, 'data': str(data.resolve()),
            'library_path': process.environ().get('LD_LIBRARY_PATH', '')}


def Quote(value, command=False):
    value = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        value = value.replace('$', '$$')
    value = ''.join(f'\\x{ord(character):02x}' if ord(character) < 32 or ord(character) == 127 else character for character in value)
    return '"' + value + '"'


def Atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Refusing a symbolic-link service override or state record')
    descriptor, temporary = tempfile.mkstemp(prefix='.chain-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def Save(path, record):
    Atomic(path, (json.dumps(record, indent=2) + '\n').encode('utf-8'))


def Health(service, expected, previous, port, timeout=25):
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            process = Process(service)
            if process['binary'] != str(Path(expected).resolve()) or process['data'] != previous['process']['data']:
                raise RuntimeError('Service executable or persistent DB changed unexpectedly')
            if process['arguments'][1:] != previous['process']['arguments'][1:]:
                raise RuntimeError('Service arguments changed during the executable replacement')
            head = Head(port)
            old = previous['head']
            if head['genesis'] != old['genesis'] or (head['transactions'], head['height']) < (old['transactions'], old['height']):
                raise RuntimeError('Recovered service has another genesis or a lower-ranked head')
            return {'process': process, 'head': head}
        except Exception as caught:
            error = caught
            time.sleep(.25)
    raise RuntimeError(f'Service health check failed: {error}')


def Restore(record):
    override = Path(record['override_path'])
    if record['old_override'] is None:
        if override.exists():
            override.unlink()
    else:
        Atomic(override, base64.b64decode(record['old_override']))
    Run('systemctl', 'daemon-reload')
    Run('systemctl', 'restart', record['service'])
    return Health(record['service'], record['before']['process']['binary'], record['before'], record['port'])


def Deploy(args):
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', args.service):
        raise ValueError('Invalid systemd service name')
    for value in (args.deployment_id, args.node):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
            raise ValueError('Invalid deployment/node identifier')
    state_root = Path(args.state_root).resolve()
    if not Path(args.state_root).is_absolute() or state_root == Path('/'):
        raise ValueError('An absolute managed deployment state root is required')
    state = state_root / args.deployment_id / (args.node + '.json')
    if state.is_symlink() or not state.resolve().is_relative_to(state_root):
        raise ValueError('Deployment record escapes the managed state root')
    if args.action == 'rollback':
        record = json.loads(state.read_text())
        if record['service'] != args.service:
            raise ValueError('Rollback service differs from its recorded deployment')
        if record['state'] == 'rolled_back':
            return {'ok': True, 'changed': False, 'state': record['state']}
        override = Path(record['override_path'])
        if not override.is_file() or override.read_text() != record['new_override']:
            raise RuntimeError('Service override changed after deployment; refusing to overwrite it')
        restored = Restore(record)
        record.update(state='rolled_back', restored=restored)
        Save(state, record)
        return {'ok': True, 'changed': True, 'record': str(state), 'state': record['state'], 'restored': restored}
    release_root = Path(args.release_root).resolve()
    release = Path(args.release_dir).resolve()
    if release.parent != release_root:
        raise ValueError('Release escapes the managed release root')
    marker = json.loads((release / '.chain-release.json').read_text())
    if marker['package_sha256'] != args.sha256:
        raise ValueError('Deployment package differs from the verified staged release')
    binary = release / 'bin' / 'boost'
    if state.exists():
        record = json.loads(state.read_text())
        if record['release_dir'] != str(release) or record['state'] != 'active':
            raise ValueError('Deployment ID already records another or rolled-back deployment')
        after = Health(args.service, binary, record['before'], args.port)
        return {'ok': True, 'changed': False, 'record': str(state), 'state': 'active', 'after': after}
    before = {'process': Process(args.service), 'head': Head(args.port)}
    override = SYSTEMD_ROOT / (args.service + '.d') / '90-chain-release.conf'
    if override.is_symlink():
        raise ValueError('Service override must not be a symbolic link')
    old_override = base64.b64encode(override.read_bytes()).decode('ascii') if override.exists() else None
    arguments = [str(binary), *before['process']['arguments'][1:]]
    libraries = str(release / 'lib')
    if before['process']['library_path']:
        libraries += ':' + before['process']['library_path']
    # An existing EnvironmentFile can override Environment= even in a later drop-in.
    # env execs the node with the release libraries and preserves its actual argv.
    command = ['/usr/bin/env', 'LD_LIBRARY_PATH=' + libraries, *arguments]
    content = '[Service]\nExecStart=\nExecStart=' + ' '.join(Quote(value, command=True) for value in command) + '\n'
    record = {'schema': 1, 'state': 'prepared', 'service': args.service, 'port': args.port,
              'release_dir': str(release), 'package_sha256': args.sha256,
              'before': before, 'override_path': str(override), 'old_override': old_override,
              'new_override': content}
    Save(state, record)
    try:
        Atomic(override, content.encode('utf-8'))
        Run('systemctl', 'daemon-reload')
        Run('systemctl', 'restart', args.service)
        after = Health(args.service, binary, before, args.port)
        record.update(state='active', after=after)
        Save(state, record)
        return {'ok': True, 'changed': True, 'state': 'active', 'record': str(state), 'after': after}
    except Exception as error:
        record['activation_error'] = str(error)
        try:
            record['restored'] = Restore(record)
            record['state'] = 'rolled_back'
        except Exception as rollback_error:
            record.update(state='rollback_failed', rollback_error=str(rollback_error))
        Save(state, record)
        return {'ok': False, 'changed': True, 'state': record['state'], 'record': str(state),
                'error': str(error), 'rollback_error': record.get('rollback_error')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['activate', 'rollback'])
    for name in ('service', 'state-root', 'deployment-id', 'node'):
        parser.add_argument('--' + name, required=True)
    for name in ('release-root', 'release-dir', 'sha256'):
        parser.add_argument('--' + name)
    parser.add_argument('--port', type=int, default=8089)
    try:
        result = Deploy(parser.parse_args())
        print(json.dumps(result))
        raise SystemExit(0 if result['ok'] else 1)
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        raise SystemExit(1)
