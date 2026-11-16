#!/usr/bin/env python3
"""External twenty-host Chain short-test adapter. Default operation is a dry-run."""
import argparse
import collections
import concurrent.futures
import datetime
import getpass
import hashlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import statistics
import struct
import sys
import threading
import time
import uuid
from types import SimpleNamespace


COMMIT = '9d34b55f1d5bf8ebab1f3fe44a86df31e18b1899'
SERVICE = 'chain-node.service'
RATES = [20000, 30000, 40000, 50000, 60000]
SECRET = None


def Utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def Safe(value):
    text = str(value)
    return text.replace(SECRET, '[redacted]') if SECRET else text


def Save(path, value):
    text = json.dumps(value, ensure_ascii=True, indent=2)
    if SECRET and SECRET in text:
        raise RuntimeError('Sensitive output refused')
    Path(path).write_text(text + '\n', encoding='utf-8')


def Require(condition, message):
    if not condition:
        raise RuntimeError(message)


def Rows(path):
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    rows = data.get('hosts') if isinstance(data, dict) else data
    Require(isinstance(rows, list) and len(rows) == 20, 'Exactly twenty inventory hosts are required')
    hosts = []
    for row in rows:
        name, host = row.get('name'), row.get('private_ipv4')
        Require(isinstance(name, str) and re.fullmatch('[A-Za-z0-9_-]{1,40}', name), 'Invalid host name')
        host = str(ipaddress.IPv4Address(host))
        aliases = [host]
        if row.get('public_ip'):
            aliases.append(str(ipaddress.IPv4Address(row['public_ip'])))
        hosts.append({'id': name, 'host': host, 'ssh_aliases': aliases})
    Require(len({r['id'] for r in hosts}) == 20 and len({r['host'] for r in hosts}) == 20,
            'Duplicate inventory host')
    return sorted(hosts, key=lambda row: row['id'])


def Regression(x, y, proportional=False):
    # Python 3.9 compatibility for the two-argument calls in Experiments.py.
    Require(not proportional and len(x) == len(y) and len(x) >= 2, 'Invalid regression inputs')
    mean_x, mean_y = statistics.mean(x), statistics.mean(y)
    denominator = math.fsum((value - mean_x) ** 2 for value in x)
    if not denominator:
        raise statistics.StatisticsError('x is constant')
    slope = math.fsum((a - mean_x) * (b - mean_y) for a, b in zip(x, y)) / denominator
    return collections.namedtuple('LinearRegression', 'slope intercept')(slope, mean_y - slope * mean_x)


REMOTE_FACTS = r'''
import hashlib,json,os,subprocess,sys
from pathlib import Path
import psutil
request=json.load(sys.stdin)
release=Path(request['release'])
def run(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=30)
 if p.returncode: raise RuntimeError('Read-only command failed: '+args[0])
 return p.stdout
props={}
for line in run(['systemctl','show','chain-node.service','--no-pager','-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID','-p','FragmentPath','-p','DropInPaths','-p','ExecStart']).splitlines():
 if '=' in line:
  k,v=line.split('=',1);props[k]=v
fisco=[]
for p in psutil.process_iter(['pid','name','exe','create_time']):
 try:
  if 'fisco' in (p.info['name'] or '').lower(): fisco.append(p.info)
 except (psutil.NoSuchProcess,psutil.AccessDenied): pass
files={}
for name in ['boost','sender','evm_sender']:
 p=release/'build'/name
 files[name]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
worker=release/'source'/'Test'/'ClusterWorker.py'
libraries=[]
for p in sorted((release/'build'/'lib').iterdir()):
 if p.is_file():
  digest=hashlib.sha256()
  with p.open('rb') as stream:
   for chunk in iter(lambda:stream.read(1024*1024),b''): digest.update(chunk)
  libraries.append({'name':p.name,'bytes':p.stat().st_size,'sha256':digest.hexdigest(),'symlink':p.is_symlink()})
main_pid=int(props.get('MainPID','0'))
main_executable=psutil.Process(main_pid).exe() if main_pid else None
listeners=[]
for c in psutil.net_connections(kind='tcp'):
 if c.status==psutil.CONN_LISTEN and c.laddr.port in (8089,20200,30300):
  listeners.append({'address':c.laddr.ip,'port':c.laddr.port,'pid':c.pid})
unit=run(['systemctl','cat','chain-node.service']) if props.get('LoadState')=='loaded' else ''
print(json.dumps({'uid':os.geteuid(),'commit':(release/'SOURCE_COMMIT').read_text().strip(),'files':files,'libraries':libraries,'worker_sha256':hashlib.sha256(worker.read_bytes()).hexdigest(),'service':props,'main_executable':main_executable,'unit_definition_sha256':hashlib.sha256(unit.encode()).hexdigest(),'fisco':sorted(fisco,key=lambda p:p['pid']),'listeners':sorted(listeners,key=lambda r:(r['port'],r['address'],r['pid'] or 0)),'existing_run_paths':[p for p in request['run_paths'] if os.path.lexists(p)]}))
'''

WAIT_BIND = r'''
import socket,sys,time
deadline=time.monotonic()+75
while True:
 s=socket.socket()
 try:
  s.bind((sys.argv[1],int(sys.argv[2])))
  s.close()
  break
 except OSError:
  s.close()
  if time.monotonic()>=deadline: raise RuntimeError('Test listener cannot rebind after the bounded quiescence wait')
  time.sleep(2)
'''


class Remotes:
    def __init__(self, nodes, args):
        import paramiko
        self.paramiko, self.args = paramiko, args
        self.nodes = {node['id']: node for node in nodes}
        self.clients, self.locks = {}, {node['id']: threading.RLock() for node in nodes}
        self.keys = paramiko.HostKeys(str(args.known_hosts))

    def Client(self, name):
        with self.locks[name]:
            client = self.clients.get(name)
            if client and client.get_transport() and client.get_transport().is_active():
                return client
            if client:
                client.close()
            node = self.nodes[name]
            client = self.paramiko.SSHClient()
            expected = {}
            for alias in node['ssh_aliases']:
                for kind, key in (self.keys.lookup(alias) or {}).items():
                    if kind in expected and expected[kind] != key:
                        raise RuntimeError(name + ': conflicting known private/public host keys')
                    expected[kind] = key
            Require(expected, name + ': no pinned SSH host key for private/public inventory address')
            for kind, key in expected.items():
                client.get_host_keys().add(node['host'], kind, key)
            client.set_missing_host_key_policy(self.paramiko.RejectPolicy())
            client.connect(node['host'], username=self.args.ssh_user, password=SECRET,
                           timeout=10, auth_timeout=15, banner_timeout=15,
                           look_for_keys=False, allow_agent=False)
            client.get_transport().set_keepalive(20)
            self.clients[name] = client
            return client

    def Run(self, name, command, payload=None, timeout=90, check=True):
        with self.locks[name]:
            channel = self.Client(name).get_transport().open_session(timeout=15)
            try:
                channel.exec_command(command)
                if payload is not None:
                    channel.sendall(json.dumps(payload).encode('utf-8'))
                channel.shutdown_write()
                started, out, err = time.monotonic(), bytearray(), bytearray()
                while True:
                    if channel.recv_ready():
                        out.extend(channel.recv(65536))
                    if channel.recv_stderr_ready():
                        err.extend(channel.recv_stderr(65536))
                    if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                        break
                    if time.monotonic() - started > timeout:
                        raise TimeoutError(name + ': command deadline; no unrelated process was killed')
                    Require(len(out) + len(err) <= 4 * 1024 * 1024, 'Remote output too large')
                    time.sleep(.02)
                result = {'code': channel.recv_exit_status(), 'stdout': out.decode('utf-8', 'replace'),
                          'stderr': err.decode('utf-8', 'replace')}
                if check and result['code']:
                    raise RuntimeError(name + ': command failed: ' + Safe(result['stderr'][-1200:]))
                return result
            finally:
                channel.close()

    def Facts(self, name):
        command = shlex.join([self.args.remote_python, '-c', REMOTE_FACTS])
        payload = {'release': str(self.args.release),
                   'run_paths': [str(Path(self.args.remote_root) / run / name) for run in self.args.run_names]}
        return json.loads(self.Run(name, command, payload)['stdout'])

    def Worker(self, node, request):
        # Only lifecycle requests; the existing worker validates process and DB ownership.
        Require(request['action'] in ('start', 'stop', 'collect', 'delete-db'), 'Unneeded worker action refused')
        if request['action'] == 'start':
            self.Run(node['id'], shlex.join([self.args.remote_python, '-c', WAIT_BIND,
                                            node['host'], str(node['port'])]), timeout=85)
        payload = {**request, 'root': node['root'], 'node': node['id']}
        command = shlex.join(['env', 'LD_LIBRARY_PATH=' + str(self.args.binary_dir / 'lib'),
                             self.args.remote_python, node['worker']])
        result = self.Run(node['id'], command, payload)
        response = json.loads(result['stdout'])
        Require(response.get('ok') is True, node['id'] + ': worker: ' + Safe(response.get('error', 'unknown error')))
        return response['result']

    def All(self, function, names=None):
        names = list(names or self.nodes)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            futures = {name: pool.submit(function, name) for name in names}
            results, errors = {}, {}
            for name, future in futures.items():
                try:
                    results[name] = future.result()
                except Exception as error:
                    errors[name] = Safe(type(error).__name__ + ': ' + str(error))
        return results, errors

    def Close(self):
        for client in self.clients.values():
            client.close()


def Discover(cluster):
    # One initiator per undirected edge. Type 9 is the existing IPv4 discovery
    # response and schedules Connect directly, bypassing only the seed scheduler.
    # Initial configured seeds and these entries name the same endpoints.
    records = []
    for index, node in enumerate(cluster.nodes):
        destinations = cluster.nodes[index + 1:]
        if not destinations:
            continue
        payload = b''.join(socket.inet_aton(peer['host']) + struct.pack('<H', peer['port'])
                           for peer in destinations)
        with socket.create_connection((node['host'], node['port']), timeout=3) as connection:
            connection.sendall(b'\x02' + struct.pack('<BI', 9, len(payload)) + payload)
            connection.shutdown(socket.SHUT_WR)
            while connection.recv(1024):
                pass
        records.append({'node': node['id'], 'requested_outgoing': len(destinations)})
    return records


def Execute(args, nodes, plan):
    global SECRET
    Require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the designated Linux controller as root')
    Require(args.known_hosts.is_file(), 'A pinned known_hosts file is required')
    Require(not args.work.exists(), 'Work directory already exists')
    if args.password_stdin:
        # Parent controller writes this one line directly into the SSH channel.
        SECRET = sys.stdin.readline().rstrip('\r\n')
    else:
        SECRET = getpass.getpass('Fleet SSH password: ')
    Require(bool(SECRET), 'Empty SSH password')
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    args.work.mkdir(parents=True, exist_ok=False)
    experiment_work = args.work / (args.work.name + '-' + uuid.uuid4().hex[:10])
    args.run_names = [experiment_work.name + '-%04d-throughput-r1' % index for index in range(1, 6)]
    Save(args.work / 'adapter-plan.json', {'commit': COMMIT, 'nodes': nodes, 'plan': plan,
                                         'source': str(args.source), 'binary': str(args.binary_dir),
                                         'run_names': args.run_names,
                                         'fisco_operations': [], 'started_utc': Utc()})
    result = {'passed': False, 'started_utc': Utc(), 'commit': COMMIT,
              'node_count': 20, 'rates': RATES, 'errors': [], 'restore_errors': {},
              'fisco_unchanged': False, 'all_databases_removed': False}
    remotes, before, stopped, clusters = Remotes(nodes, args), {}, set(), []
    timers = set()
    old_signals = {}
    def Interrupted(signum, frame):
        raise KeyboardInterrupt('Controller interrupted by signal ' + str(signum))
    for signum in (signal.SIGTERM, signal.SIGINT):
        old_signals[signum] = signal.signal(signum, Interrupted)
    try:
        sys.path.insert(0, str(args.source / 'Test'))
        import Cluster as cluster_module
        import Experiments as experiments
        if not hasattr(statistics, 'linear_regression'):
            statistics.linear_regression = Regression
        Require((args.release / 'SOURCE_COMMIT').read_text().strip() == COMMIT, 'Controller source marker mismatch')
        proof = json.loads((args.release / 'deploy' / 'build-proof.json').read_text())
        Require(proof.get('passed') is True and proof.get('commit') == COMMIT and proof.get('tests') == 12,
                'Pinned native build proof failed')
        wanted_files = {row['name']: {'bytes': row['bytes'], 'sha256': row['sha256']} for row in proof['files']}
        Require(set(wanted_files) == {'boost', 'sender', 'evm_sender'}, 'Unexpected build file manifest')
        worker_hash = hashlib.sha256((args.source / 'Test' / 'ClusterWorker.py').read_bytes()).hexdigest()
        before, errors = remotes.All(remotes.Facts)
        Save(args.work / 'preflight.json', {'nodes': before, 'errors': errors})
        Require(not errors and len(before) == 20, 'All twenty SSH/worker preflights must succeed')
        for name, facts in before.items():
            Require(facts['uid'] == 0 and facts['commit'] == COMMIT and facts['files'] == wanted_files and
                    facts['worker_sha256'] == worker_hash, name + ': source/runtime/worker identity mismatch')
            Require(not facts['existing_run_paths'], name + ': refusing an existing remote experiment directory')
            # Libraries may be host-specific CentOS compatibility variants.
            # Their public name/size/hash trace is evidence, not a cross-host
            # equality requirement; the three native binaries stay pinned.
            Require(bool(facts['libraries']), name + ': missing runtime library trace')
            Require(facts['service']['LoadState'] in ('loaded', 'not-found') and
                    facts['service']['ActiveState'] in ('active', 'inactive', 'failed'), name + ': unstable service state')
        original_start = cluster_module.Cluster.Start
        cluster_module.Worker = remotes.Worker
        def Start(cluster):
            clusters.append(cluster)
            original_start(cluster)
            # All nodes are live but no transaction load has started yet.
            discovery = Discover(cluster)
            Save(cluster.output / 'discovery-adapter.json', {'rpc': 9, 'entries': discovery,
                                                          'target_unique_peers': 19})
        cluster_module.Cluster.Start = Start
        # A transient recovery timer protects the original service if this
        # controller is killed. It never targets the FISCO services or data.
        timer_base = 'chain-tps-restore-' + args.work.name
        def Suspend(name):
            if before[name]['service']['ActiveState'] == 'active':
                command = shlex.join(['systemd-run', '--quiet', '--unit=' + timer_base,
                                      '--on-active=20m', '--timer-property=AccuracySec=1s',
                                      '/bin/systemctl', 'start', SERVICE])
                remotes.Run(name, command, timeout=40)
                timers.add(name)
                stopped.add(name)  # Restore even if stop returns an error.
                remotes.Run(name, shlex.join(['systemctl', 'stop', SERVICE]), timeout=90)
            check = remotes.Run(name, "ss -H -lnt 'sport = :8089'", timeout=15)['stdout'].strip()
            Require(not check, name + ': 8089 remains occupied; no unrelated process will be stopped')
            return {'original_active': before[name]['service']['ActiveState'] == 'active', 'port_8089_free': True}
        suspended, errors = remotes.All(Suspend)
        Save(args.work / 'suspended.json', {'nodes': suspended, 'errors': errors})
        Require(not errors, 'Failed to suspend all original Chain services safely')
        os.environ['LD_LIBRARY_PATH'] = str(args.binary_dir / 'lib')
        normalized = {'nodes': nodes}
        Save(args.work / 'inventory.json', normalized)
        Save(args.work / 'plan.json', plan)
        experiments.Main(SimpleNamespace(inventory=args.work / 'inventory.json', bin=args.binary_dir,
                                        plan=args.work / 'plan.json', work=experiment_work,
                                        suite='throughput', local=None, dry_run=False, keep_going=True))
        results = json.loads((experiment_work / 'results.json').read_text())
        result['experiment_directory'] = str(experiment_work)
        result['cases'] = [{'rate': row['case']['rate'], 'status': row['status'],
                            'drained': row.get('metrics', {}).get('drained'),
                            'common_commit_tps': row.get('metrics', {}).get('common_commit_tps'),
                            'achieved_send_tps': row.get('metrics', {}).get('achieved_send_tps')}
                           for row in results]
        Require(len(results) == 5 and [row['case']['rate'] for row in results] == RATES and
                all(row['status'] == 'complete' for row in results), 'One or more throughput rounds failed')
        cleanup = list(experiment_work.glob('*/*-database-cleanup.json'))
        result['all_databases_removed'] = len(cleanup) == 100 and all(
            json.loads(path.read_text()).get('removed') is True for path in cleanup)
        Require(result['all_databases_removed'], 'Database cleanup proof is incomplete')
        Require(all(row.get('metrics', {}).get('drained') is True and
                    row.get('metrics', {}).get('checks') for row in results),
                'Some offered loads did not drain and pass all-node final state checks within the fixed budget')
        result['passed'] = True
    except BaseException as error:
        result['errors'].append(Safe(type(error).__name__ + ': ' + str(error)))
    finally:
        # Finish owned-process cleanup and restoration even if a second ordinary
        # interrupt arrives. SIGKILL remains covered by the independent timer.
        for signum in old_signals:
            signal.signal(signum, signal.SIG_IGN)
        # Cluster.Close normally did this already. If an outer failure happened,
        # retry ONLY recorded experiment processes through the ownership checker.
        emergency = {}
        for cluster in clusters:
            for index in sorted(cluster.started):
                node = cluster.nodes[index]
                try:
                    remotes.Worker(node, {'action': 'stop', 'run': cluster.run})
                    collected = remotes.Worker(node, {'action': 'collect', 'run': cluster.run})
                    Save(cluster.output / (node['id'] + '-emergency-collect.json'), collected)
                    remotes.Worker(node, {'action': 'delete-db', 'run': cluster.run})
                except Exception as error:
                    emergency[node['id'] + '/' + cluster.run] = Safe(error)
        if emergency:
            result['errors'].append('Experiment cleanup retry failed')
            result['passed'] = False
        def Restore(name):
            if name in stopped:
                # Listen() does not set reuse_address. A real established TCP
                # socket can delay rebinding, so retry service recovery only;
                # measurement and drain periods above remain unchanged.
                deadline = time.monotonic() + 90
                facts = None
                last_error = None
                while time.monotonic() < deadline:
                    try:
                        remotes.Run(name, shlex.join(['systemctl', 'start', SERVICE]), timeout=30, check=False)
                        time.sleep(1)
                        facts = remotes.Facts(name)
                        if (facts['service']['ActiveState'] == 'active' and
                                facts['main_executable'] == before[name]['main_executable'] and
                                any(row['port'] == 8089 and row['pid'] == int(facts['service']['MainPID'])
                                    for row in facts['listeners'])):
                            break
                    except Exception as error:
                        last_error = Safe(error)
                    time.sleep(1)
                else:
                    raise RuntimeError(name + ': original executable/listener did not recover; watchdog retained; ' + str(last_error))
            if name in timers:
                remotes.Run(name, shlex.join(['systemctl', 'stop', timer_base + '.timer']), timeout=20)
            return remotes.Facts(name)
        # Names that never passed preflight have no mutation to restore.
        after, errors = remotes.All(Restore, before.keys()) if before else ({}, {})
        Save(args.work / 'emergency-cleanup.json', {'errors': emergency})
        Save(args.work / 'postflight.json', {'nodes': after, 'errors': errors})
        result['restore_errors'] = errors
        result['original_services_restored'] = not errors and len(after) == 20 and all(
            (before[name]['service']['ActiveState'] == 'active') == (facts['service']['ActiveState'] == 'active')
            and before[name]['unit_definition_sha256'] == facts['unit_definition_sha256']
            and (before[name]['service']['ActiveState'] != 'active' or
                 before[name]['main_executable'] == facts['main_executable'])
            for name, facts in after.items())
        result['fisco_unchanged'] = not errors and len(after) == 20 and all(
            facts['fisco'] == before[name]['fisco'] and
            [row for row in facts['listeners'] if row['port'] in (20200, 30300)] ==
            [row for row in before[name]['listeners'] if row['port'] in (20200, 30300)]
            for name, facts in after.items())
        result['passed'] = bool(result['passed'] and result['original_services_restored'] and result['fisco_unchanged'])
        result['finished_utc'] = Utc()
        Save(args.work / 'adapter-result.json', result)
        for signum, previous in old_signals.items():
            signal.signal(signum, previous)
        remotes.Close()
        SECRET = None
    print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)
    return 0 if result['passed'] else 1


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--release', type=Path, default=Path('/opt/chain/releases') / COMMIT)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--remote-root', default='/var/tmp/chain-tps-20261116')
    parser.add_argument('--remote-python', default='python3')
    parser.add_argument('--ssh-user', default='root')
    parser.add_argument('--known-hosts', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--password-stdin', action='store_true')
    args = parser.parse_args()
    Require(re.fullmatch('[A-Za-z0-9_-]{1,30}', args.work.name), 'Use a new short alphanumeric work directory name')
    Require(args.release.is_absolute() and args.remote_root.startswith('/') and args.work.is_absolute(),
            'Release, remote root and work paths must be absolute')
    Require('..' not in Path(args.remote_root).parts and args.remote_root not in ('/', '/opt', '/opt/chain'),
            'Use an isolated experiment root')
    args.source, args.binary_dir = args.release / 'source', args.release / 'build'
    rows = Rows(args.inventory)
    nodes = [{**row, 'port': 8089, 'bind': row['host'], 'transport': 'ssh',
              'ssh': args.ssh_user + '@' + row['host'], 'binary': str(args.binary_dir / 'boost'),
              'worker': str(args.source / 'Test' / 'ClusterWorker.py'), 'root': args.remote_root,
              'allow_netem': False} for row in rows]
    plan = json.loads((args.source / 'Experiments' / 'plan.json').read_text())
    plan.update(nodes=20, node_counts=[20], throughput_rates=RATES, throughput_modes=['throughput'],
                warmup=2, duration=10, drain_seconds=5, repeats=1)
    if not args.execute:
        print(json.dumps({'execute': False, 'commit': COMMIT, 'nodes': nodes, 'plan': plan,
                          'adapter': 'memory-only SSH auth; native worker; type9 discovery; fixed short tests'}, indent=2))
        return 0
    return Execute(args, nodes, plan)


if __name__ == '__main__':
    try:
        sys.exit(Main())
    except Exception as error:
        print(json.dumps({'passed': False, 'error': Safe(type(error).__name__ + ': ' + str(error))}), flush=True)
        sys.exit(1)
