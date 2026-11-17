"""Run the Ansible comparison with service snapshots and unconditional recovery."""
import concurrent.futures
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

root = Path(__file__).resolve().parent
secret = sys.stdin.readline().rstrip('\r\n')
assert secret
variables = root / 'run-vars.json'
settings = json.loads(variables.read_text())
output = Path(settings['chain_result_dir'])
assert output.is_absolute(), 'An absolute isolated result directory is required'
run = settings['chain_run_id']
assert re.fullmatch(r'[A-Za-z0-9_-]{1,60}', run), 'Run ID must also satisfy the service guard limit'
settings.setdefault('chain_run_group', run)
assert settings['chain_run_group'] == run, 'Service, benchmark and recovery must use the same run group'
settings.setdefault('chain_run_owner', uuid.uuid4().hex)
assert re.fullmatch(r'[0-9a-f]{32}', settings['chain_run_owner']), 'Invalid run ownership token'
output.mkdir(exist_ok=False, parents=True)
# A generated token must survive the benchmark process for the outer finally.
# Keep the caller's input untouched and pass this exact effective file everywhere.
variables = output / 'effective-vars.json'
settings['chain_extra_vars_file'] = str(variables)
variables.write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')
os.chmod(variables, 0o600)
env = {**os.environ, 'CHAIN_SSH_PASSWORD': secret, 'PATH': str(root / 'venv/bin') + ':' + os.environ['PATH'],
       'ANSIBLE_HOST_KEY_CHECKING': 'True', 'ANSIBLE_NOCOLOR': '1'}
inventory = json.loads((root / 'ansible-inventory.json').read_text())['all']['children']['chain']['hosts']
sys.path.insert(0, str(root / 'runner/Test'))
import Cluster
from AnsibleBenchmark import Discover


def Save(name, value):
    (output / name).write_text(json.dumps(value, indent=2), encoding='utf-8')


def Snapshot():
    def One(item):
        name, data = item
        client = Cluster.NodeClient({'id': name, 'host': data['chain_p2p_address'], 'port': 8089})
        try:
            stats, state = client.Query(13), client.Query(35)
            assert not stats['failed']
            return name, dict(height=stats['height'], head=stats['head'],
                transactions=stats['chain_transactions'], state=state, peers=stats['peers'])
        finally:
            client.Close()
    with concurrent.futures.ThreadPoolExecutor(20) as pool:
        return dict(pool.map(One, inventory.items()))


def Play(name, extra=None):
    command = [str(root / 'venv/bin/ansible-playbook'), '-i', str(root / 'ansible-inventory.json'),
        str(root / 'runner/ansible' / (name + '.yml')), '--forks', '20', '-e', '@' + str(variables)]
    if extra:
        command += ['-e', json.dumps(extra)]
    with (output / (name + '-' + str(time.time_ns()) + '.log')).open('w') as log:
        child = subprocess.Popen(command, cwd=root / 'runner/ansible', env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace', start_new_session=True)
        errors = []
        def Drain():
            try:
                for line in child.stdout:
                    log.write(line.replace(secret, '[redacted]'))
                    log.flush()
            except BaseException as error:
                errors.append(str(error))
        reader = threading.Thread(target=Drain, daemon=True)
        reader.start()
        try:
            # The permanent-service watchdog fires after 40m. Abort earlier so
            # test traffic cannot silently overlap automatic service recovery.
            deadline = 2200 if name == 'benchmark' else 300
            code = child.wait(timeout=deadline)
            reader.join(timeout=10)
            if errors:
                raise RuntimeError('Ansible evidence reader failed: ' + errors[0])
            if reader.is_alive():
                raise RuntimeError('Ansible output pipe remained open after exit')
            return code
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            reader.join(timeout=10)


def Interrupt(signum, frame):
    raise KeyboardInterrupt('Controller interrupted')


for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(signum, Interrupt)
result = dict(completed=False, restored=False, cleanup=False)
before = None
try:
    before = Snapshot()
    assert all(node['peers'] == 19 for node in before.values()), 'Permanent graph must be restored before another benchmark'
    Save('permanent-before.json', before)
    for name in ('pause', 'restore'):
        (output / ('services-' + name)).mkdir()
    code = Play('service_guard', dict(chain_service_guard_action='pause',
        chain_service_guard_results_dir=str(output / 'services-pause')))
    assert code == 0, 'Could not establish a uniform service background'
    (output / 'firewall-allow').mkdir()
    code = Play('firewall_guard', dict(chain_firewall_guard_action='allow',
        chain_firewall_guard_results_dir=str(output / 'firewall-allow')))
    assert code == 0, 'Could not establish the isolated test-port firewall rules'
    result['benchmark_exit'] = Play('benchmark')
    result['completed'] = True
except BaseException as error:
    result['error'] = (type(error).__name__ + ': ' + str(error)).replace(secret, '[redacted]')
finally:
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, signal.SIG_IGN)
    try:
        result['cleanup_exit'] = Play('cleanup')
        result['cleanup'] = result['cleanup_exit'] == 0
    except Exception as error:
        result['cleanup_error'] = str(error).replace(secret, '[redacted]')
    try:
        (output / 'firewall-restore').mkdir(exist_ok=True)
        result['firewall_restore_exit'] = Play('firewall_guard', dict(chain_firewall_guard_action='restore',
            chain_firewall_guard_results_dir=str(output / 'firewall-restore')))
        result['firewall_restored'] = result['firewall_restore_exit'] == 0
    except Exception as error:
        result['firewall_restore_error'] = str(error).replace(secret, '[redacted]')
    try:
        (output / 'services-restore').mkdir(exist_ok=True)
        result['restore_exit'] = Play('service_guard', dict(chain_service_guard_action='restore',
            chain_service_guard_results_dir=str(output / 'services-restore')))
        result['restored'] = result['restore_exit'] == 0
        deadline = time.monotonic() + 40
        while True:
            try:
                Discover([{'id': name, 'host': data['chain_p2p_address'], 'port': 8089}
                          for name, data in inventory.items()])
                after = Snapshot()
            except (OSError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
                continue
            if all(node['peers'] == 19 for node in after.values()) or time.monotonic() >= deadline:
                break
            time.sleep(1)
        Save('permanent-after.json', after)
        result['permanent_peers_restored'] = all(node['peers'] == 19 for node in after.values())
        # Peer reconnection is measured separately from persistent ledger state.
        result['permanent_chain_unchanged'] = before is not None and {
            name: {key: value for key, value in node.items() if key != 'peers'} for name, node in before.items()
        } == {
            name: {key: value for key, value in node.items() if key != 'peers'} for name, node in after.items()
        }
    except Exception as error:
        result['restore_error'] = str(error).replace(secret, '[redacted]')
    result['finished'] = time.time()
    Save('comparison-result.json', result)
secret = None
env.pop('CHAIN_SSH_PASSWORD', None)
print(json.dumps(result), flush=True)
sys.exit(0 if result['completed'] and result['restored'] and result['cleanup'] and
         result.get('firewall_restored') and
         result.get('permanent_chain_unchanged') and result.get('permanent_peers_restored') and
         result.get('benchmark_exit') == 0 else 1)
