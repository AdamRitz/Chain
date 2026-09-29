"""Linux ingress impairment, limited to configured chain peers and ports."""
import hashlib
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path


def NetemCommands(interface, port, peers, settings, tag):
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
        raise ValueError('Invalid network interface')
    if not 1 <= int(port) <= 65535 or not peers:
        raise ValueError('A node port and peer list are required')
    delay = float(settings.get('delay_ms', 0))
    jitter = float(settings.get('jitter_ms', 0))
    loss = float(settings.get('loss_percent', 0))
    if not 0 <= jitter <= delay <= 5000 or not 0 <= loss <= 100:
        raise ValueError('Invalid delay, jitter or loss')
    device = 'ch' + hashlib.sha256(tag.encode()).hexdigest()[:11]
    commands = [['ip', 'link', 'add', device, 'type', 'ifb'],
                ['ip', 'link', 'set', device, 'up']]
    queue = int(settings.get('queue_packets', 100000))
    if not 1 <= queue <= 1000000:
        raise ValueError('queue_packets must be 1..1000000')
    options = ['limit', str(queue)]
    if delay:
        options += ['delay', f'{delay}ms']
        if jitter:
            options += [f'{jitter}ms', 'distribution', 'normal']
    if loss:
        options += ['loss', 'random', f'{loss}%']
    commands.append(['tc', 'qdisc', 'add', 'dev', device, 'root', 'netem', *options])
    filters = []
    for index, peer in enumerate(peers):
        address = str(ipaddress.IPv4Address(peer['host']))
        remote_port = int(peer['port'])
        if not 1 <= remote_port <= 65535:
            raise ValueError('Invalid peer port')
        for direction, value in [('src_port', remote_port), ('dst_port', int(port))]:
            preference = 45000 + index * 2 + (direction == 'dst_port')
            if preference > 48000:
                raise ValueError('Too many impairment filters')
            filters.append(preference)
            commands.append(['tc', 'filter', 'add', 'dev', interface, 'ingress', 'protocol', 'ip',
                             'pref', str(preference), 'flower', 'ip_proto', 'tcp',
                             'src_ip', address, direction, str(value),
                             'action', 'mirred', 'egress', 'redirect', 'dev', device])
    return device, filters, commands


def Execute(command, sudo=False, check=True):
    result = subprocess.run((['sudo', '-n'] if sudo else []) + command,
                            capture_output=True, text=True, timeout=15)
    if check and result.returncode:
        raise RuntimeError(f'{command}: {result.stderr.strip()}')
    return result


def ClearNetem(path):
    path = Path(path)
    if not path.exists():
        return {'cleared': False}
    state = json.loads(path.read_text())
    errors = []
    for preference in reversed(list(state['installed_filters'])):
        result = Execute(['tc', 'filter', 'del', 'dev', state['interface'], 'ingress',
                          'protocol', 'ip', 'pref', str(preference)], state['sudo'], False)
        if result.returncode:
            errors.append(result.stderr.strip())
        else:
            state['installed_filters'].remove(preference)
            path.write_text(json.dumps(state))
    if state.get('device_created') and not errors:
        result = Execute(['ip', 'link', 'del', state['device']], state['sudo'], False)
        if result.returncode:
            errors.append(result.stderr.strip())
        else:
            state['device_created'] = False
            path.write_text(json.dumps(state))
    # A shared clsact is left in place; only this experiment's filters/device are removed.
    if errors:
        raise RuntimeError('Network cleanup incomplete; retain lease: ' + '; '.join(errors))
    path.unlink()
    return {'cleared': True}


def InstallNetem(path, interface, port, peers, settings, sudo=False):
    if sys.platform != 'linux':
        raise RuntimeError('Packet delay/loss/partition experiments require Linux tc/netem')
    path = Path(path)
    if path.exists():
        raise RuntimeError(f'Existing impairment lease: {path}; clear it before starting')
    device, preferences, commands = NetemCommands(interface, port, peers, settings, str(path))
    existing = json.loads(Execute(['tc', '-j', 'filter', 'show', 'dev', interface, 'ingress'], sudo).stdout or '[]')
    if any(int(item.get('pref', 0)) in preferences for item in existing):
        raise RuntimeError('Experiment filter priorities are already occupied')
    qdiscs = json.loads(Execute(['tc', '-j', 'qdisc', 'show', 'dev', interface], sudo).stdout)
    if any(item['kind'] == 'ingress' for item in qdiscs):
        raise RuntimeError('An existing ingress qdisc needs manual review before installing clsact')
    if not any(item['kind'] == 'clsact' for item in qdiscs):
        Execute(['tc', 'qdisc', 'add', 'dev', interface, 'clsact'], sudo)
    state = {'interface': interface, 'device': device, 'sudo': sudo,
             'installed_filters': [], 'device_created': False, 'settings': settings}
    path.write_text(json.dumps(state))
    try:
        for command in commands:
            Execute(command, sudo)
            if command[:3] == ['ip', 'link', 'add']:
                state['device_created'] = True
            if command[:3] == ['tc', 'filter', 'add']:
                state['installed_filters'].append(int(command[command.index('pref')+1]))
            path.write_text(json.dumps(state))
    except BaseException:
        ClearNetem(path)
        raise
    return state
