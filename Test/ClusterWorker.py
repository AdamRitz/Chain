"""Local/SSH process lifecycle. Requests are JSON on stdin; no background service."""
import json
import hashlib
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
import psutil
from Netem import InstallNetem, ClearNetem


def CaseDirectory(request):
    root = Path(request['root']).expanduser().resolve()
    for key in ['run', 'node']:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request[key]):
            raise ValueError(f'Invalid {key} identifier')
    directory = (root / request['run'] / request['node']).resolve()
    # Windows can return an extended path when another worker creates an ancestor concurrently.
    if os.name == 'nt':
        def NormalPath(path):
            text = str(path)
            if text.startswith('\\\\?\\UNC\\'):
                return Path('\\\\'+text[8:])
            return Path(text.removeprefix('\\\\?\\'))
        root, directory = NormalPath(root), NormalPath(directory)
    if not directory.is_relative_to(root):
        raise ValueError(f'Experiment directory escapes root: {directory!s} (root {root!s})')
    return directory


def FindProcess(record):
    try:
        process = psutil.Process(record['pid'])
        if abs(process.create_time() - record['created']) > .01:
            return None
        if Path(process.exe()).resolve() != Path(record['binary']).resolve():
            raise RuntimeError('PID executable changed; refusing to signal it')
        return process
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None


def Handle(request):
    directory = CaseDirectory(request)
    record_file = directory / 'process.json'
    action = request['action']
    if action == 'start':
        binary = Path(request['binary']).expanduser().resolve(strict=True)
        if directory.exists():
            if not request.get('restart'):
                raise FileExistsError(f'Experiment directory already exists: {directory}')
            old = json.loads(record_file.read_text())
            if FindProcess(old):
                raise RuntimeError('Node is already running')
        else:
            directory.mkdir(parents=True)
        genesis = directory / 'genesis.json'
        if genesis.exists() and json.loads(genesis.read_text()) != request['genesis']:
            raise ValueError('Restart genesis mismatch')
        genesis.write_text(json.dumps(request['genesis']), encoding='utf-8')
        command = [str(binary), '--data', str(directory/'db'), '--genesis', str(genesis),
                   '--metrics', str(directory/'final.json'), *map(str, request['arguments'])]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with (directory/'node.log').open('a', encoding='utf-8') as log:
            child = subprocess.Popen(command, cwd=directory, stdout=log, stderr=log,
                                     stdin=subprocess.DEVNULL, creationflags=flags,
                                     start_new_session=os.name != 'nt')
        record = {'pid': child.pid, 'created': psutil.Process(child.pid).create_time(),
                  'binary': str(binary), 'command': command}
        record_file.write_text(json.dumps(record, indent=2))
        return {**record, 'directory': str(directory), 'system': platform.platform(),
                'logical_cpus': psutil.cpu_count(), 'physical_cores': psutil.cpu_count(False),
                'memory_bytes': psutil.virtual_memory().total,
                'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}
    if action == 'stop':
        if not record_file.exists():
            return {'stopped': False}
        process = FindProcess(json.loads(record_file.read_text()))
        if process:
            process.terminate()
            try:
                process.wait(timeout=20)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return {'stopped': True}
    if action == 'collect':
        output = {}
        for name in ['process.json', 'final.json', 'netem.json']:
            path = directory/name
            if path.exists():
                output[name] = json.loads(path.read_text(encoding='utf-8'))
        path = directory/'node.log'
        if path.exists():
            with path.open('rb') as source:
                source.seek(max(0, path.stat().st_size - 65536))
                output['log_tail'] = source.read().decode('utf-8', errors='replace')
        return output
    if action == 'fault':
        return InstallNetem(directory/'netem.json', request['interface'], request['port'],
                            request['peers'], request['settings'], request.get('sudo', False))
    if action == 'clear-fault':
        return ClearNetem(directory/'netem.json')
    raise ValueError(f'Unknown worker action: {action}')


if __name__ == '__main__':
    try:
        print(json.dumps({'ok': True, 'result': Handle(json.load(sys.stdin))}))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        sys.exit(1)
