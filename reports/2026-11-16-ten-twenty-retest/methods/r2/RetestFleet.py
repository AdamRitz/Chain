"""Fixed 10/20-node comparison; pinned binaries, memory SSH, guarded restoration."""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import shlex
import signal
import sys
import time
from types import SimpleNamespace

import tps_runner as T
T.RATES = [20000, 30000]

FISCO = 'fisco-bcos.service'

UNIT_CODE = r'''
import hashlib,json,subprocess
def get(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=20)
 return p.stdout
props={}
for line in get(['systemctl','show','fisco-bcos.service','--no-pager','-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID','-p','ExecStart']).splitlines():
 if '=' in line:
  k,v=line.split('=',1);props[k]=v
unit=get(['systemctl','cat','fisco-bcos.service']) if props.get('LoadState')=='loaded' else ''
print(json.dumps({'properties':props,'unit_sha256':hashlib.sha256(unit.encode()).hexdigest()}))
'''


def Run(args):
    T.Require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Linux root controller required')
    T.SECRET = sys.stdin.readline().rstrip('\r\n')
    T.Require(T.SECRET, 'Credential input is empty')
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    T.Require(not args.work.exists(), 'Use a new result directory')
    args.work.mkdir(parents=True)
    rows = T.Rows(args.inventory)
    # Keep the same ten public hosts and ingress ordering in both groups.
    rows.sort(key=lambda n: (not n['id'].startswith('ecs-c381-'), n['id']))
    groups = [(10, 'r2-n10-20261116'), (20, 'r2-n20-20261116')]
    args.run_names = [f'{name}-{rate}-r1' for _, name in groups for rate in T.RATES]
    args.binary_dir = args.release / 'build'
    args.source = args.release / 'source'
    nodes = [{**row, 'port': 8089, 'bind': row['host'], 'transport': 'ssh',
              'ssh': 'root@' + row['host'], 'binary': str(args.binary_dir / 'boost'),
              'worker': str(args.source / 'Test/ClusterWorker.py'), 'root': args.remote_root,
              'allow_netem': False} for row in rows]
    remotes = T.Remotes(nodes, args)
    result = {'source_commit': T.COMMIT, 'started_utc': T.Utc(), 'completed': False,
              'errors': [], 'restored': False, 'groups': []}
    before, units, stopped, timers, clusters = {}, {}, {}, set(), []
    snapshots = None
    timer_name = 'chain-retest2-20261116-recover'
    sys.path.insert(0, str(args.source / 'Test'))
    import Cluster as C
    import Experiments as E
    import statistics
    if not hasattr(statistics, 'linear_regression'):
        statistics.linear_regression = T.Regression
    old_start = C.Cluster.Start
    old_signals = {}
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Controller signal ' + str(signum))
    def Unit(name):
        return json.loads(remotes.Run(name, shlex.join([args.remote_python, '-c', UNIT_CODE]))['stdout'])
    def ChainSnapshots():
        def one(name):
            node = next(n for n in nodes if n['id'] == name)
            client = C.NodeClient(node)
            try:
                stats, state = client.Query(13), client.Query(35)
                return {'failed': stats['failed'], 'height': stats['height'], 'head': stats['head'],
                        'chain_transactions': stats['chain_transactions'], 'state': state}
            finally:
                client.Close()
        values, errors = remotes.All(one)
        return {'nodes': values, 'errors': errors}
    def Start(cluster):
        clusters.append(cluster)
        old_start(cluster)
        T.Save(cluster.output / 'discovery-adapter.json', {'entries': T.Discover(cluster),
               'target_unique_peers': len(cluster.nodes) - 1})
    C.Worker = remotes.Worker
    C.Cluster.Start = Start
    reader_source = Path(__file__).with_name('HeaderReader.py').read_text()
    def ExportHeaders(node, run):
        command = shlex.join(['env', 'LD_LIBRARY_PATH=' + str(args.binary_dir / 'lib'),
            args.remote_python, '-c', reader_source, '--root', node['root'], '--run', run,
            '--node', node['id'], '--library', str(args.binary_dir / 'lib/librocksdb.so.6.26')])
        value = json.loads(remotes.Run(node['id'], command, timeout=60)['stdout'])
        T.Require(value.get('ok') and value.get('read_only'), node['id'] + ': header export failed')
        return value
    C.ExportHeaders = ExportHeaders
    for signum in (signal.SIGTERM, signal.SIGINT):
        old_signals[signum] = signal.signal(signum, interrupted)
    try:
        proof = json.loads((args.release / 'deploy/build-proof.json').read_text())
        T.Require(proof.get('passed') and proof.get('commit') == T.COMMIT, 'Native build proof mismatch')
        wanted = {v['name']: {'bytes': v['bytes'], 'sha256': v['sha256']} for v in proof['files']}
        worker_sha = hashlib.sha256((args.source / 'Test/ClusterWorker.py').read_bytes()).hexdigest()
        before, errors = remotes.All(remotes.Facts)
        T.Save(args.work / 'preflight.json', {'nodes': before, 'errors': errors})
        T.Require(not errors and len(before) == 20, 'All twenty preflights required')
        for name, facts in before.items():
            T.Require(facts['commit'] == T.COMMIT and facts['files'] == wanted and
                      facts['worker_sha256'] == worker_sha and not facts['existing_run_paths'],
                      name + ': immutable source/binary or isolated-path check failed')
            T.Require(facts['service']['ActiveState'] == 'active', name + ': original Chain is not active')
        units, errors = remotes.All(Unit)
        T.Save(args.work / 'fisco-before.json', {'nodes': units, 'errors': errors})
        T.Require(not errors, 'FISCO unit discovery failed')
        snapshots = ChainSnapshots()
        T.Save(args.work / 'permanent-before.json', snapshots)
        T.Require(not snapshots['errors'] and len(snapshots['nodes']) == 20 and
                  all(not s['failed'] for s in snapshots['nodes'].values()), 'Permanent chain snapshot failed')
        def Suspend(name):
            active = [T.SERVICE]
            if units[name]['properties'].get('ActiveState') == 'active':
                active.append(FISCO)
            remotes.Run(name, shlex.join(['systemd-run', '--quiet', '--unit=' + timer_name,
                        '--on-active=40m', '--timer-property=AccuracySec=1s',
                        '/bin/systemctl', 'start', *active]), timeout=40)
            timers.add(name)
            stopped[name] = active
            remotes.Run(name, shlex.join(['systemctl', 'stop', *active]), timeout=90)
            facts = remotes.Facts(name)
            T.Require(facts['service']['ActiveState'] == 'inactive' and not facts['fisco'] and
                      not facts['listeners'], name + ': test background did not stop cleanly')
            return {'stopped_original_services': active, 'fisco_processes': facts['fisco'],
                    'listeners': facts['listeners']}
        suspended, errors = remotes.All(Suspend)
        T.Save(args.work / 'suspended.json', {'nodes': suspended, 'errors': errors})
        T.Require(not errors, 'Could not establish matching idle service background')
        os.environ['LD_LIBRARY_PATH'] = str(args.binary_dir / 'lib')
        dataset_sha = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
        T.Save(args.work / 'comparison-plan.json', {'source_commit': T.COMMIT, 'binary_files': wanted,
               'dataset_sha256': dataset_sha, 'node_counts': [10, 20], 'rates': T.RATES,
               'warmup': 2, 'duration': 10, 'drain_seconds': 5, 'repeats': 1,
               'controller_host': 'ecs-c381-0003', 'pause_original_chain_on_all_20': True,
               'pause_previously_active_fisco': True, 'group_order': [10, 20]})
        from RetestCases import RunGroup
        for count, name in groups:
            group_args = SimpleNamespace(source=args.source, bin=args.binary_dir, work=args.work / name,
                    known_hosts=args.known_hosts, source_revision=T.COMMIT,
                    expected_binary_sha256=wanted['boost']['sha256'])
            plan = json.loads((args.source / 'Experiments/plan.json').read_text())
            plan.update(nodes=count, node_counts=[count], throughput_rates=T.RATES,
                        throughput_modes=['throughput'], warmup=2, duration=10, drain_seconds=5, repeats=1)
            group_rows = RunGroup(group_args, nodes[:count], plan, args.dataset)
            result['groups'].append({'nodes': count, 'directory': str(group_args.work), 'cases': len(group_rows)})
            for row in group_rows:
                if row['status'] != 'complete':
                    result['errors'].append(f"{count} nodes case failed: " + T.Safe(row.get('error', row['case'])))
        cleanup = list(args.work.glob('*/*/*-database-cleanup.json'))
        result['databases_removed'] = len(cleanup) == 60 and all(json.loads(p.read_text()).get('removed') for p in cleanup)
        T.Require(result['databases_removed'], 'Missing 60 guarded DB cleanup proofs')
        result['completed'] = not result['errors']
    except BaseException as error:
        result['errors'].append(T.Safe(type(error).__name__ + ': ' + str(error)))
    finally:
        for signum in old_signals:
            signal.signal(signum, signal.SIG_IGN)
        # RetestCases normally closes each cluster. Retry only still-existing owned DBs.
        emergency = {}
        for cluster in clusters:
            for index in sorted(cluster.started):
                node = cluster.nodes[index]
                try:
                    remotes.Worker(node, {'action': 'stop', 'run': cluster.run})
                    remotes.Worker(node, {'action': 'delete-db', 'run': cluster.run})
                except Exception as error:
                    emergency[node['id'] + '/' + cluster.run] = T.Safe(error)
        T.Save(args.work / 'emergency-cleanup.json', emergency)
        def Restore(name):
            if name in stopped:
                deadline = time.monotonic() + 100
                while time.monotonic() < deadline:
                    remotes.Run(name, shlex.join(['systemctl', 'start', *stopped[name]]), timeout=35, check=False)
                    time.sleep(1)
                    facts, unit = remotes.Facts(name), Unit(name)
                    chain_ok = facts['service']['ActiveState'] == 'active' and facts['main_executable'] == before[name]['main_executable']
                    fisco_ok = unit['properties'].get('ActiveState') == units[name]['properties'].get('ActiveState')
                    before_ports = sorted((s['address'], s['port']) for s in before[name]['listeners'] if s['port'] in (20200, 30300))
                    after_ports = sorted((s['address'], s['port']) for s in facts['listeners'] if s['port'] in (20200, 30300))
                    fisco_ok = fisco_ok and before_ports == after_ports and sorted(p['exe'] for p in facts['fisco']) == sorted(p['exe'] for p in before[name]['fisco'])
                    port_ok = any(s['port'] == 8089 and s['pid'] == int(facts['service']['MainPID']) for s in facts['listeners'])
                    if chain_ok and fisco_ok and port_ok:
                        break
                else:
                    raise RuntimeError(name + ': original services failed to recover; watchdog retained')
                T.Require(facts['unit_definition_sha256'] == before[name]['unit_definition_sha256'] and
                          unit['unit_sha256'] == units[name]['unit_sha256'], name + ': service definition changed')
            else:
                facts, unit = remotes.Facts(name), Unit(name)
            if name in timers:
                remotes.Run(name, shlex.join(['systemctl', 'stop', timer_name + '.timer']), timeout=20)
            return {'facts': facts, 'fisco_unit': unit}
        after, errors = remotes.All(Restore, before.keys()) if before else ({}, {})
        T.Save(args.work / 'postflight.json', {'nodes': after, 'errors': errors})
        result['restore_errors'] = errors
        result['restored'] = not errors and len(after) == 20
        if result['restored'] and snapshots is not None:
            permanent_after = ChainSnapshots()
            T.Save(args.work / 'permanent-after.json', permanent_after)
            result['permanent_chain_unchanged'] = permanent_after == snapshots
        result['cleanup_errors'] = emergency
        result['finished_utc'] = T.Utc()
        T.Save(args.work / 'retest-result.json', result)
        for signum, previous in old_signals.items():
            signal.signal(signum, previous)
        remotes.Close()
        T.SECRET = None
    print(json.dumps(result), flush=True)
    return 0 if result['completed'] and result['restored'] and result.get('permanent_chain_unchanged') and not result['errors'] and not result['cleanup_errors'] else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('inventory', 'known-hosts', 'work', 'dataset'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--release', type=Path, default=Path('/opt/chain/releases') / T.COMMIT)
    p.add_argument('--remote-root', default='/var/tmp/chain-retest2-20261116')
    p.add_argument('--remote-python', required=True)
    p.add_argument('--ssh-user', default='root')
    sys.exit(Run(p.parse_args()))
