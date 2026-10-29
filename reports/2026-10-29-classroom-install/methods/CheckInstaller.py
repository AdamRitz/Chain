"""Run one installer integration step; never close/kill the application or read wallets.

Use on a disposable Windows test account or after checking its existing installation.
This writes actual HKCU installation state and shortcuts. Each invocation saves evidence.
UI observations/normal window close are separate computer-use steps.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import winreg
import psutil
import ctypes
import msvcrt

REPO = Path(__file__).resolve().parents[3]
REPORT = Path(__file__).resolve().parents[1]
INSTALL = Path(os.environ['LOCALAPPDATA']) / 'Chain Classroom'
DATA = Path(os.environ['LOCALAPPDATA']) / 'edu.chain.classroom'
MARKER = DATA / 'installer-preservation-probe-20261029.txt'
MARKER_BYTES = b'Chain Classroom installer data preservation test, 2026-10-29\n'


def Sha(path):
    return hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()


def PhysicalPath(path):
    # A packaged development host can virtualize LocalAppData. Resolve the
    # actual file handle before matching executable paths reported by Windows.
    with path.open('rb') as file:
        function = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        count = function(msvcrt.get_osfhandle(file.fileno()), buffer, len(buffer), 0)
        if not 0 < count < len(buffer):
            raise OSError('Cannot resolve installed file handle')
        return Path(buffer.value.removeprefix('\\\\?\\'))


def Snapshot():
    registry = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Chain Classroom',
                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            for field in ('DisplayVersion', 'InstallLocation', 'UninstallString'):
                registry[field] = winreg.QueryValueEx(key, field)[0]
    except FileNotFoundError:
        pass
    main = INSTALL / 'chain-classroom.exe'
    physical_install = PhysicalPath(main).parent if main.exists() else INSTALL
    processes = []
    for process in psutil.process_iter(['pid', 'exe', 'create_time']):
        try:
            executable = process.info['exe']
            if executable and (Path(executable).is_relative_to(INSTALL) or Path(executable).is_relative_to(physical_install)):
                processes.append(process.info)
        except (OSError, psutil.Error):
            pass
    matches = []
    manifest_path = INSTALL / 'runtime/manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text('utf-8-sig'))
        for entry in manifest['files']:
            path = INSTALL / 'runtime' / entry['name']
            matches.append({'file': entry['name'], 'matches': path.is_file() and Sha(path) == entry['sha256']})
    return {
        'registry': registry, 'processes': processes,
        'physical_install': str(physical_install),
        'installed_exe': str(main), 'main_sha256': Sha(main) if main.exists() else None,
        'runtime_files': matches,
        'data_probe_preserved': MARKER.exists() and MARKER.read_bytes() == MARKER_BYTES,
        'start_menu_shortcut': (Path(os.environ['APPDATA']) /
            'Microsoft/Windows/Start Menu/Programs/Chain Classroom.lnk').exists(),
    }


def Main():
    parser = argparse.ArgumentParser()
    parser.add_argument('label')
    parser.add_argument('--action', choices=('install', 'old-install', 'uninstall', 'inspect', 'probe'), default='inspect')
    parser.add_argument('--expected-code', type=int, default=0)
    parser.add_argument('flags', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.label.replace('-', '').isalnum():
        raise ValueError('Use a simple evidence label')
    result = {'label': args.label, 'action': args.action, 'before': Snapshot()}
    if args.action == 'probe':
        if MARKER.exists():
            raise RuntimeError('Probe already exists; do not overwrite')
        DATA.mkdir(parents=True, exist_ok=True)
        MARKER.write_bytes(MARKER_BYTES)
    elif args.action != 'inspect':
        if args.action == 'uninstall':
            executable = INSTALL / 'uninstall.exe'
            flags = ['/S', '_?=' + str(INSTALL)]
            # NSIS requires _?= to be last and UNQUOTED even with spaces.
            # Quoting that argument would instead run a detached temporary copy.
            command_line = subprocess.list2cmdline([str(executable), '/S']) + ' _?=' + str(INSTALL)
        else:
            version = '0.1.0' if args.action == 'old-install' else '0.1.1'
            executable = REPO / f'Classroom/release/ChainClassroom-{version}-windows-x64-setup.exe'
            flags = args.flags
            command_line = [str(executable), *flags]
        result['command'] = [str(executable), *flags]
        result['installer_sha256'] = Sha(executable)
        start = time.monotonic()
        # No terminal window or developer tools on PATH. Setup owns its progress UI.
        environment = {**os.environ, 'PATH': os.environ['SystemRoot'] + r'\System32'}
        process = subprocess.Popen(command_line, env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW)
        result['exit_code'] = process.wait(timeout=240)
        result['elapsed_seconds'] = round(time.monotonic() - start, 3)
        time.sleep(2)
    result['after'] = Snapshot()
    (REPORT / 'data' / f'{args.label}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'label': args.label, 'exit_code': result.get('exit_code'),
        'elapsed_seconds': result.get('elapsed_seconds'), 'snapshot': result['after']}, ensure_ascii=False))
    if 'exit_code' in result and result['exit_code'] != args.expected_code:
        raise SystemExit('Unexpected installer exit code')


if __name__ == '__main__':
    Main()
