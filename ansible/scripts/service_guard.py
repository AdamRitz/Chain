#!/usr/bin/env python3
"""Pause only named benchmark background services, with a timed restoration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SERVICES = ('chain-node.service', 'fisco-bcos.service')
STATE_ROOT = Path('/var/lib/chain/ansible-service-guards')


def Run(argv, check=True):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=110)
    if check and p.returncode:
        raise RuntimeError('Command failed: ' + argv[0] + ': ' + p.stderr[-800:])
    return p


def Facts():
    result = {}
    for service in SERVICES:
        p = Run(['systemctl', 'show', service, '--no-pager', '-p', 'LoadState',
                 '-p', 'ActiveState', '-p', 'MainPID'], check=False)
        props = dict(line.split('=', 1) for line in p.stdout.splitlines() if '=' in line)
        if not props.get('LoadState'):
            raise RuntimeError('Cannot inspect ' + service)
        unit = Run(['systemctl', 'cat', service]).stdout if props['LoadState'] == 'loaded' else ''
        pid = int(props.get('MainPID', 0))
        exe, argv = None, []
        if pid:
            exe = os.readlink('/proc/%d/exe' % pid)
            argv = Path('/proc/%d/cmdline' % pid).read_bytes().rstrip(b'\0').decode().split('\0')
        result[service] = {'load': props['LoadState'], 'active': props.get('ActiveState'),
                           'unit_sha256': hashlib.sha256(unit.encode()).hexdigest(),
                           'exe': exe, 'argv': argv}
    return result


def Save(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def Restore(path, timer):
    state = json.loads(path.read_text())
    before = state['before']
    wanted = [s for s in SERVICES if before[s]['active'] == 'active']
    # Never rewrite service definitions or restore a different application's unit.
    now = Facts()
    for s in SERVICES:
        if now[s]['unit_sha256'] != before[s]['unit_sha256']:
            raise RuntimeError('Service definition changed while paused: ' + s)
    if wanted:
        Run(['systemctl', 'start', *wanted])
    deadline = time.monotonic() + 45
    while True:
        after = Facts()
        matched = all(after[s]['active'] == before[s]['active'] and
                      after[s]['load'] == before[s]['load'] and
                      after[s]['unit_sha256'] == before[s]['unit_sha256'] and
                      (s not in wanted or (after[s]['exe'] == before[s]['exe'] and
                                           after[s]['argv'] == before[s]['argv']))
                      for s in SERVICES)
        if matched:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Original services have not recovered; recovery timer retained')
        time.sleep(1)
    Run(['systemctl', 'stop', timer + '.timer'], check=False)
    state.update(restored=True, after=after)
    Save(path, state)
    return state


def Main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['pause', 'restore', 'status'])
    p.add_argument('--run', required=True)
    args = p.parse_args()
    if os.geteuid() or not re.fullmatch('[A-Za-z0-9_-]{1,60}', args.run):
        raise RuntimeError('Root and a simple unique run identifier are required')
    os.umask(0o077)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    if STATE_ROOT.is_symlink():
        raise RuntimeError('Guard state root cannot be a symlink')
    path = STATE_ROOT / (args.run + '.json')
    timer = 'chain-ansible-' + args.run + '-recover'
    if args.action == 'status':
        return {'services': Facts(), 'guard': json.loads(path.read_text()) if path.exists() else None}
    if path.is_symlink():
        raise RuntimeError('Guard state cannot be a symlink')
    if args.action == 'restore':
        return Restore(path, timer) if path.exists() else {'restored': True, 'never_paused': True}
    if path.exists():
        raise RuntimeError('Run identifier already used; refusing to overwrite recovery state')
    before = Facts()
    if before[SERVICES[0]]['active'] != 'active':
        raise RuntimeError('Permanent Chain service must be active before the benchmark')
    if any(v['active'] not in ('active', 'inactive', 'failed') for v in before.values()):
        raise RuntimeError('Wait for background service transitions to finish')
    state = {'run': args.run, 'before': before, 'restored': False}
    Save(path, state)
    try:
        Run(['systemd-run', '--quiet', '--unit=' + timer, '--on-active=40m',
             '--timer-property=AccuracySec=1s', sys.executable, str(Path(__file__).resolve()),
             'restore', '--run', args.run])
        wanted = [s for s in SERVICES if before[s]['active'] == 'active']
        Run(['systemctl', 'stop', *wanted])
        state['paused'] = Facts()
        if any(state['paused'][s]['active'] != 'inactive' for s in wanted):
            raise RuntimeError('Background service did not stop')
        Save(path, state)
        return state
    except BaseException:
        Restore(path, timer)
        raise


if __name__ == '__main__':
    try:
        print(json.dumps({'ok': True, 'result': Main()}))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        sys.exit(1)
