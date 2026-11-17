#!/usr/bin/env python3
"""Lease only the isolated benchmark's private-subnet TCP port in firewalld."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

STATE_ROOT = Path('/var/lib/chain/ansible-firewall-guards')
TIMEOUT = 2400
NETWORK = '192.168.0.0/24'
PORT = 18089


def Require(value, message):
    if not value:
        raise RuntimeError(message)


def Run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def Rule(run, owner):
    # A run-specific positive priority distinguishes this exact runtime rule
    # without changing the subnet/port or preceding existing reject rules.
    priority = 1 + int(hashlib.sha256((run + ':' + owner).encode()).hexdigest()[:8], 16) % 32767
    return 'rule priority="%s" family="ipv4" source address="%s" port port="%s" protocol="tcp" accept' % (
        priority, NETWORK, PORT)


def Save(path, state):
    Require(not path.is_symlink(), 'Guard state cannot be a symlink')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(state, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink()
            raise
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class Firewall:
    def __init__(self, runner=Run, executable=None):
        self.runner = runner
        self.executable = executable or shutil.which('firewall-cmd')

    def Call(self, *arguments, allowed=(0,)):
        Require(self.executable is not None, 'firewall-cmd is unavailable')
        result = self.runner([self.executable, *arguments])
        Require(result.returncode in allowed, 'firewall-cmd failed (%s): %s' %
                (result.returncode, (result.stderr or result.stdout)[-800:]))
        return result

    def Active(self):
        if self.executable is None:
            return False
        # NOT_RUNNING=252; other failures must not look like an inactive daemon.
        return self.Call('--state', allowed=(0, 252)).returncode == 0

    def Zones(self):
        lines = self.Call('--get-active-zones').stdout.splitlines()
        zones = [line.strip().split()[0] for line in lines if line.strip() and not line[0].isspace()]
        if not zones:
            zones = [self.Call('--get-default-zone').stdout.strip()]
        Require(all(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', zone) for zone in zones), 'Invalid active zone')
        return sorted(set(zones))

    def Present(self, zone, rule):
        return self.Call('--zone=' + zone, '--query-rich-rule=' + rule, allowed=(0, 1)).returncode == 0

    def Add(self, zone, rule, seconds):
        self.Call('--zone=' + zone, '--add-rich-rule=' + rule, '--timeout=%ss' % seconds)

    def Remove(self, zone, rule):
        self.Call('--zone=' + zone, '--remove-rich-rule=' + rule)


def ValidateState(state, run, owner):
    Require(state.get('schema') == 1 and state.get('run') == run and state.get('owner') == owner,
            'Guard ownership mismatch')
    Require(state.get('rule') == Rule(run, owner) and state.get('timeout_seconds') == TIMEOUT,
            'Guard rule or timeout changed')
    Require(type(state.get('expires_at')) in (int, float) and math.isfinite(state['expires_at']),
            'Invalid guard deadline')
    seen = set()
    for row in state.get('zones', []):
        zone = row.get('zone')
        Require(isinstance(zone, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', zone) and zone not in seen,
                'Invalid or duplicate saved zone')
        Require(type(row.get('preexisting')) is bool and type(row.get('intent')) is bool,
                'Missing rule ownership evidence')
        Require(not row['preexisting'] or not row['intent'], 'Preexisting rules cannot be owned')
        seen.add(zone)


def Guard(action, run, owner, path, firewall, now=time.time):
    Require(action in ('allow', 'restore', 'status'), 'Invalid guard action')
    Require(re.fullmatch(r'[A-Za-z0-9_-]{1,60}', run) and re.fullmatch(r'[0-9a-f]{32}', owner),
            'Simple run identifier and lowercase UUID hex owner are required')
    Require(not path.is_symlink(), 'Guard state cannot be a symlink')
    state = json.loads(path.read_text()) if path.exists() else None
    if state is not None:
        ValidateState(state, run, owner)
    if action == 'status':
        return dict(changed=False, firewalld_active=firewall.Active(), guard=state)
    if action == 'restore' and state is None:
        return dict(changed=False, restored=True, never_opened=True)
    if action == 'restore' and state['restored']:
        return dict(changed=False, restored=True, guard=state)
    if action == 'allow' and state is None:
        active = firewall.Active()
        rule = Rule(run, owner)
        zones = [dict(zone=zone, preexisting=firewall.Present(zone, rule), intent=False)
                 for zone in firewall.Zones()] if active else []
        state = dict(schema=1, run=run, owner=owner, rule=rule, timeout_seconds=TIMEOUT,
                     created_at=now(), expires_at=now() + TIMEOUT, firewalld_active=active,
                     restored=False, zones=zones)
        Save(path, state)
    changed = False
    try:
        if action == 'allow':
            Require(not state['restored'], 'Guard already restored; use a fresh run identifier')
            if not state['firewalld_active']:
                Require(not firewall.Active(), 'firewalld became active; use a fresh run identifier')
                return dict(changed=False, skipped='firewalld inactive or unavailable', guard=state)
            Require(firewall.Active(), 'firewalld stopped during guard setup')
            for row in state['zones']:
                if row['preexisting']:
                    Require(firewall.Present(row['zone'], state['rule']), 'Preexisting rule disappeared')
                    continue
                seconds = math.floor(state['expires_at'] - now())
                Require(seconds > 0, 'Guard lease expired; restore and use a fresh run identifier')
                if not row['intent']:
                    # Persist ownership before mutation so a lost reply or crash
                    # can be retried/restored using exactly this one rule.
                    Require(not firewall.Present(row['zone'], state['rule']), 'Rule appeared before ownership claim')
                    row['intent'] = True
                    Save(path, state)
                if not firewall.Present(row['zone'], state['rule']):
                    firewall.Add(row['zone'], state['rule'], seconds)
                    changed = True
                Require(firewall.Present(row['zone'], state['rule']), 'Added rule is not present')
                row['confirmed'] = True
                Save(path, state)
            state['opened'] = True
        else:
            active = firewall.Active()
            for row in state['zones']:
                if row['intent'] and not row['preexisting']:
                    if active and firewall.Present(row['zone'], state['rule']):
                        firewall.Remove(row['zone'], state['rule'])
                        changed = True
                    Require(not active or not firewall.Present(row['zone'], state['rule']),
                            'Owned runtime rule still present')
                    row['removed_or_expired'] = True
            state['restored'] = True
            state['restored_at'] = now()
        state.pop('last_error', None)
        Save(path, state)
    except BaseException as error:
        state['last_error'] = type(error).__name__ + ': ' + str(error)
        Save(path, state)
        raise
    return dict(changed=changed, restored=state['restored'], guard=state)


def SecureRoot(root):
    Require(root.is_absolute(), 'State root must be absolute')
    for path in [*reversed(root.parents), root]:
        Require(not path.is_symlink(), 'State directory ancestor cannot be a symlink')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    Require(root.is_dir() and not root.is_symlink(), 'Invalid state root')
    if hasattr(os, 'geteuid'):
        Require(root.stat().st_uid == 0 and root.stat().st_mode & 0o077 == 0,
                'State root must be root-owned and private')


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['allow', 'restore', 'status'])
    parser.add_argument('--run', required=True)
    parser.add_argument('--owner', required=True)
    args = parser.parse_args()
    Require(os.geteuid() == 0, 'Root is required')
    Require(re.fullmatch(r'[A-Za-z0-9_-]{1,60}', args.run) and re.fullmatch(r'[0-9a-f]{32}', args.owner),
            'Invalid run or owner')
    os.umask(0o077)
    SecureRoot(STATE_ROOT)
    import fcntl
    descriptor = os.open(STATE_ROOT / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return Guard(args.action, args.run, args.owner, STATE_ROOT / (args.run + '.json'), Firewall())


if __name__ == '__main__':
    try:
        print(json.dumps(dict(ok=True, result=Main())))
    except Exception as error:
        print(json.dumps(dict(ok=False, error=type(error).__name__ + ': ' + str(error))))
        sys.exit(1)
