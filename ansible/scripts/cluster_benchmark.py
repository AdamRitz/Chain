"""Run fixed-data native-transfer tests on an Ansible-managed sparse graph."""
import argparse
import collections
import concurrent.futures
import hashlib
import json
import math
from pathlib import Path
import re
import socket
import subprocess
import struct
import sys
import time
import traceback
from types import SimpleNamespace

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'Test'))
import Cluster as ExistingCluster
import Experiments as Experiments
from cluster_plan import MakePlan


def Save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def Digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def ProbeReady(node, timeout):
    """One absolute deadline covers connect, frame header and complete response."""
    deadline = time.monotonic() + timeout
    with socket.create_connection((node['host'], node['port']), timeout=timeout) as connection:
        def Read(count):
            data = bytearray()
            while len(data) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Node readiness query timed out')
                connection.settimeout(remaining)
                chunk = connection.recv(count - len(data))
                if not chunk:
                    raise ConnectionError('Node readiness response ended early')
                data.extend(chunk)
            return bytes(data)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Node readiness connection timed out')
        connection.settimeout(remaining)
        connection.sendall(b'\x02' + struct.pack('<BI', 13, 0))
        kind, size = struct.unpack('<BI', Read(5))
        if kind != 14 or size > 16384:
            raise ValueError('Invalid node readiness response')
        stats = json.loads(Read(size))
        if stats.get('failed') is not False:
            raise RuntimeError('Node reported an execution failure')
        return stats


def AnsibleErrors(stdout, stderr):
    """Classify failures without retaining raw output or credential values."""
    text = (stdout or '') + '\n' + (stderr or '')
    patterns = [('Permission denied', 'SSH authentication failed; check the inventory credentials'),
                ('Host key verification failed', 'SSH host key check failed; update the verified known_hosts entry'),
                ('UNREACHABLE!', 'Ansible could not reach one or more inventory hosts'),
                ('Connection timed out', 'SSH connection timed out'),
                ('undefined variable', 'An Ansible variable is missing'),
                ("couldn't resolve module", 'An Ansible module is unavailable in the controller environment'),
                ('Syntax Error', 'Ansible could not parse a playbook or variable file')]
    return [message for pattern, message in patterns if pattern.lower() in text.lower()]


def ValidateSettings(settings):
    if not isinstance(settings, dict):
        raise ValueError('node_settings must be an object')
    forbidden = {'data', 'genesis', 'metrics', 'config', 'port', 'bind', 'seed', 'seeds-file',
                 'peer-discovery', 'max-peers', 'max-connections', 'target-peers', 'backup-seed',
                 'relay-transactions', 'run-seconds', 'stop-file', 'vdf-key', 'vdf-bootstrap-produce'}
    if any(not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9-]+', key) or key in forbidden or
           not isinstance(value, (str, int, float, bool)) for key, value in settings.items()):
        raise ValueError('Node settings cannot replace lifecycle, topology or identity arguments')


class LowTpsStop:
    """Stop after three slow 15-second windows with increasing transaction backlog."""
    def __init__(self, rate, warmup, *, ratio=.7, start=30, window=15, windows=3):
        self.rate, self.warmup, self.ratio = rate, warmup, ratio
        self.start, self.window, self.windows = start, window, windows
        self.previous, self.slow = None, []

    def __call__(self, point):
        if not point.get('common') or point['seconds'] < self.warmup + self.start:
            return None
        current = {'seconds': point['seconds'], 'transactions': point['common']['transactions'],
                   'backlog': point['sent'] - point['common']['transactions']}
        if self.previous is None:
            self.previous = current
            return None
        elapsed = current['seconds'] - self.previous['seconds']
        if elapsed < self.window:
            return None
        throughput = (current['transactions'] - self.previous['transactions']) / elapsed
        growth = current['backlog'] - self.previous['backlog']
        record = {'begin_seconds': self.previous['seconds'], 'end_seconds': current['seconds'],
                  'tps': throughput, 'backlog_growth': growth}
        self.previous = current
        if throughput < self.rate * self.ratio and growth > 0:
            self.slow.append(record)
        else:
            self.slow.clear()
        if len(self.slow) < self.windows:
            return None
        return {'reason': 'TPS below threshold and transaction backlog increased in consecutive windows',
                'threshold_tps': self.rate * self.ratio, 'windows': self.slow[-self.windows:]}


def Validate(context):
    for key in ('inventory', 'plan', 'dataset', 'genesis', 'output', 'ansible_dir', 'ansible_playbook'):
        if not isinstance(context.get(key), str) or not Path(context[key]).is_absolute():
            raise ValueError(key + ' must be an absolute path')
    if not isinstance(context.get('inventories', [context['inventory']]), list) or not context.get('inventories', [context['inventory']]) or any(
            not isinstance(path, str) or not Path(path).is_absolute() for path in context.get('inventories', [context['inventory']])):
        raise ValueError('Inventory sources must be a nonempty list of absolute paths')
    for key in ('run', 'release_id'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', context.get(key, '')):
            raise ValueError('Invalid ' + key)
    if not re.fullmatch(r'[0-9a-f]{32}', context.get('owner', '')):
        raise ValueError('A run ownership token is required')
    for key in ('dataset', 'genesis'):
        if Digest(context[key]) != context.get(key + '_sha256'):
            raise ValueError(key + ' SHA256 mismatch')
    if not re.fullmatch(r'[0-9a-f]{64}', context.get('binary_sha256', '')):
        raise ValueError('A pinned binary SHA256 is required')
    for key, lower, upper in (('rate', 1, 10000000), ('wallets', 1, 100000), ('forks', 1, 128),
                              ('query_workers', 1, 128), ('batch', 1, 10000)):
        if type(context.get(key)) is not int or not lower <= context[key] <= upper:
            raise ValueError('Invalid ' + key)
    for key, lower, upper in (('duration', .1, 3600), ('warmup', 0, 600), ('drain_seconds', .1, 600),
                              ('poll_seconds', .01, 60), ('sample_hz', .1, 128), ('metrics_period', .1, 60)):
        value = context.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError('Invalid ' + key)
    readiness = context.get('readiness_timeout', 300)
    if isinstance(readiness, bool) or not isinstance(readiness, (int, float)) or not math.isfinite(readiness) or not 1 <= readiness <= 600:
        raise ValueError('Invalid readiness_timeout')
    if type(context.get('bootstrap_producers', 1)) is not int or not 0 <= context.get('bootstrap_producers', 1) <= 64:
        raise ValueError('Invalid bootstrap_producers')
    if type(context.get('early_stop_enabled', True)) is not bool:
        raise ValueError('Invalid early_stop_enabled')
    for key, default, lower, upper in (('early_stop_ratio', .7, .01, .99),
            ('early_stop_start_seconds', 30, 0, 600), ('early_stop_window_seconds', 15, .1, 600),
            ('max_backlog_growth', 10, 0, 10000000)):
        value = context.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError('Invalid ' + key)
    if type(context.get('early_stop_windows', 3)) is not int or not 1 <= context.get('early_stop_windows', 3) <= 20:
        raise ValueError('Invalid early_stop_windows')
    if context['warmup'] + context['duration'] + context['drain_seconds'] + 2 * readiness > 3000:
        raise ValueError('Leave time for deployment and cleanup before the one-hour process guard')
    count = int(context['rate'] * (context['warmup'] + context['duration']))
    if count < 1 or Path(context['dataset']).stat().st_size % 176 or Path(context['dataset']).stat().st_size < count * 176:
        raise ValueError('Prepared dataset is too small')
    plan = json.loads(Path(context['plan']).read_text(encoding='utf-8'))
    keys = {'id', 'host', 'ansible_host', 'ansible_user', 'ansible_port', 'port', 'bind', 'query_host'}
    rebuilt = MakePlan([{key: value for key, value in node.items() if key in keys} for node in plan['nodes']],
                       plan['cluster'], plan['requested_degree'], plan['seed'], len(plan['injectors']))
    if rebuilt != plan:
        raise ValueError('Plan does not match its deterministic graph')
    if len({node['port'] for node in plan['nodes']}) != 1 or plan['nodes'][0]['port'] == 8089:
        raise ValueError('Benchmark nodes must share one isolated test port other than 8089')
    if context.get('node_ids') != [node['id'] for node in plan['nodes']]:
        raise ValueError('Inventory host order/identities must match the generated plan')
    genesis = json.loads(Path(context['genesis']).read_text(encoding='utf-8'))
    if len(genesis.get('accounts', [])) != context['wallets'] or type(genesis.get('base_fee')) is not int:
        raise ValueError('Use a sender --prepare genesis with the declared wallet count')
    ValidateSettings(context.get('node_settings', {}))
    return plan, genesis


def Arguments(plan, settings, producer_ids=None):
    ValidateSettings(settings)
    result = {}
    for node in plan['nodes']:
        values = {'io-threads': 1, 'verify-threads': 1, 'candidate-threads': 1,
                  'block-verify-threads': 1, 'query-threads': 1, 'sync': 1, 'produce': 1,
                  'experiment-index': 1, 'block-ms': 200, 'max-block-txs': 6000,
                  'clock-sync': 0, **settings, 'port': node['port'], 'bind': node['bind'],
                  'peer-discovery': 0, 'max-peers': len(node['neighbors']) + 4, 'target-peers': len(node['neighbors']),
                  'max-connections': len(node['neighbors']) + 36, 'relay-transactions': 1}
        if producer_ids is not None:
            # Every node keeps its identity and VDF calculation thread. Only the
            # first two epochs restrict which local node creates blocks.
            values['produce'] = 1
            values['vdf-bootstrap-produce'] = int(node['id'] in producer_ids)
        arguments = [part for key, value in values.items() for part in
                     ('--' + key, str(int(value) if isinstance(value, bool) else value))]
        for peer in node['seeds']:
            arguments += ['--seed', peer]
        for peer in node['backup_seeds']:
            arguments += ['--backup-seed', peer]
        result[node['id']] = arguments
    return result


def ConsensusReady(rows, genesis):
    """Accept a statistics sweep crossing an epoch boundary, but never bootstrap."""
    config = genesis.get('vdf', {})
    if not config.get('enabled', bool(config)):
        return True
    expected = min(5, len(rows))
    identities = {row.get('vdf', {}).get('public_key') for row in rows}
    committees = {}
    for row in rows:
        state = row.get('vdf', {})
        epoch, producers = state.get('epoch', -1), state.get('producers', [])
        if (not state.get('enabled') or epoch < 2 or len(producers) != expected or
                len(set(producers)) != expected or not set(producers).issubset(identities)):
            return False
        if config.get('producer_rotation') and state.get('producer_rotation') is not True:
            return False
        if config.get('consecutive_blocks', 0) > 0 and (
                state.get('consecutive_blocks') != config['consecutive_blocks'] or
                state.get('producer_timeout_slots') != config.get('producer_timeout_slots', 8)):
            return False
        committee = tuple(producers)
        if epoch in committees and committees[epoch] != committee:
            return False
        committees[epoch] = committee
    return True


class Workers:
    def __init__(self, context, plan):
        self.context, self.plan, self.sequence = context, plan, 0

    def Call(self, action, genesis=None, arguments=None):
        self.sequence += 1
        output = Path(self.context['output']) / f'{self.sequence:02}-{action}'
        output.mkdir()
        variables = dict(self.context.get('extra_vars', {}))
        variables.update(chain_worker_action=action, chain_worker_release_id=self.context['release_id'],
                         chain_run_id=self.context['run'], chain_run_group=self.context['run'],
                         chain_run_owner=self.context['owner'], chain_worker_results_dir=str(output),
                         chain_worker_genesis=genesis or {}, chain_worker_arguments_by_node=arguments or {},
                         chain_guard_seconds=3600, chain_test_port=self.plan['nodes'][0]['port'])
        request = output / 'request.json'
        Save(request, variables)
        command = [self.context['ansible_playbook']]
        for inventory in self.context.get('inventories') or [self.context['inventory']]:
            command += ['-i', inventory]
        command += [str(Path(self.context['ansible_dir']) / 'workers.yml'), '--forks', str(self.context['forks'])]
        if self.context.get('extra_vars_file'):
            command += ['-e', '@' + self.context['extra_vars_file']]
        command += ['-e', '@' + str(request)]
        try:
            process = subprocess.run(command, cwd=self.context['ansible_dir'], capture_output=True,
                                     text=True, encoding='utf-8', errors='replace', timeout=3300)
        except subprocess.TimeoutExpired:
            Save(output / 'operation.json', {'returncode': None, 'error': 'Ansible exceeded the 3300 second lifecycle timeout'})
            raise RuntimeError(f'{action}: Ansible timed out; partial starts will be recovered; inspect {output}') from None
        # Structured worker artifacts contain no credentials. Raw Ansible output may.
        result, errors = {}, []
        for node in self.plan['nodes']:
            path = output / (node['id'] + '.json')
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                if not value.get('ok'):
                    raise RuntimeError(value.get('error', 'worker failed'))
                result[node['id']] = value['result']
            except Exception as error:
                errors.append({'node': node['id'], 'error': str(error)})
        diagnostics = AnsibleErrors(process.stdout, process.stderr) if process.returncode else []
        Save(output / 'operation.json', {'returncode': process.returncode, 'errors': errors, 'diagnostics': diagnostics})
        if errors or process.returncode:
            hint = '; '.join(diagnostics) or 'inspect the per-node worker results and inventory configuration'
            raise RuntimeError(f'{action} failed: {hint}; inspect {output}')
        return result


class SparseCluster(ExistingCluster.Cluster):
    def __init__(self, context, plan, genesis):
        nodes = [{'id': node['id'], 'host': node['query_host'], 'port': node['port']} for node in plan['nodes']]
        super().__init__(nodes, context['run'], genesis, context.get('node_settings', {}), context['output'])
        self.pool.shutdown(wait=True)
        from concurrent.futures import ThreadPoolExecutor
        self.query_workers = min(context['query_workers'], len(nodes))
        self.pool = ThreadPoolExecutor(max_workers=self.query_workers)
        self.workers, self.plan = Workers(context, plan), plan
        self.touched = False
        self.query_sweeps = []
        self.metrics_period = context['metrics_period']
        self.cached_stats, self.last_stats = None, 0

    def All(self, function, indices=None):
        started = time.monotonic()
        try:
            return super().All(function, indices)
        finally:
            self.query_sweeps.append({'started': started, 'seconds': time.monotonic() - started})

    def Stats(self):
        if self.cached_stats is None or time.monotonic() - self.last_stats >= self.metrics_period:
            self.cached_stats = super().Stats()
            self.last_stats = time.monotonic()
            return self.cached_stats
        # Full counters are several KB per node. Poll only the 80-byte published
        # head between counter collections; common-prefix and sample checks stay exact.
        def Head(index):
            payload = self.clients[index].Query(19, raw=True)
            if len(payload) != 80:
                raise ValueError('Invalid published-head length')
            height, transactions = struct.unpack_from('<QQ', payload, 64)
            return {**self.cached_stats[index], 'head': payload[32:64].hex(),
                    'height': height, 'chain_transactions': transactions}
        return self.All(Head)

    def WaitReady(self, indices, timeout=180, probe=ProbeReady):
        waiting = set(indices)
        queue = collections.deque((index, 0) for index in sorted(waiting))
        pending, errors = {}, {}
        deadline = time.monotonic() + timeout
        def ProbeOne(index):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Readiness deadline reached before querying the node')
            return probe(self.nodes[index], min(2.0, remaining))
        try:
            while waiting and time.monotonic() < deadline:
                now = time.monotonic()
                while queue and len(pending) < self.query_workers and queue[0][1] <= now:
                    index, _ = queue.popleft()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    future = self.pool.submit(ProbeOne, index)
                    pending[future] = index
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wake = min(remaining, max(.001, queue[0][1] - time.monotonic())) if queue and len(pending) < self.query_workers else remaining
                if pending:
                    done, _ = concurrent.futures.wait(pending, timeout=wake,
                                                      return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        index = pending.pop(future)
                        try:
                            future.result()
                            waiting.remove(index)
                            errors.pop(self.nodes[index]['id'], None)
                        except Exception as error:
                            errors[self.nodes[index]['id']] = f'{type(error).__name__}: {error}'
                            queue.append((index, time.monotonic() + .25))
                else:
                    time.sleep(wake)
        finally:
            for future in pending:
                future.cancel()
        if waiting:
            names = [self.nodes[index]['id'] for index in sorted(waiting)]
            Save(self.output / 'readiness-errors.json', {'timeout_seconds': timeout, 'nodes': names, 'errors': errors})
            raise TimeoutError(f'{len(names)} nodes did not become ready within {timeout}s: {names[:8]}; inspect readiness-errors.json')

    def WaitConnected(self, name):
        timeout = self.workers.context.get('readiness_timeout', 300)
        deadline = time.monotonic() + timeout
        error, rows, common = '', [], None
        while True:
            try:
                # Force fresh counters here: cached head-only replies retain old VDF lists.
                rows = super().Stats()
                peers_ready = all(not row['failed'] and row['peers'] >= len(node['neighbors'])
                                  for node, row in zip(self.plan['nodes'], rows))
                if peers_ready and ConsensusReady(rows, self.genesis):
                    common = self.Common(rows)
                    config = self.genesis.get('vdf', {})
                    required = 1 + 2 * int(config.get('epoch_blocks', 32)) if config.get('enabled', bool(config)) else 1
                    if common and common['height'] >= required:
                        Save(self.output / name, {'nodes': rows, 'common': common})
                        self.cached_stats, self.last_stats = None, 0
                        return rows
            except (OSError, ValueError, RuntimeError, ConnectionError) as failure:
                error = str(failure)
            if time.monotonic() >= deadline:
                Save(self.output / 'consensus-readiness-errors.json',
                     {'nodes': rows, 'common': common, 'error': error, 'timeout_seconds': timeout})
                raise TimeoutError('Peer graph and common VDF producer list did not become ready; inspect consensus-readiness-errors.json')
            time.sleep(.5)

    def Start(self):
        self.touched = True
        config = self.genesis.get('vdf', {})
        count = self.workers.context.get('bootstrap_producers', 1)
        bootstrap = bool(config.get('producer_rotation') and 0 < count < len(self.nodes))
        producers = {node['id'] for node in self.plan['nodes'][:count]} if bootstrap else None
        machines = self.workers.Call('start', self.genesis, Arguments(self.plan, self.settings, producers))
        if {row['binary_sha256'] for row in machines.values()} != {self.workers.context['binary_sha256']}:
            raise ValueError('Cluster binaries differ from the pinned release')
        Save(self.output / 'machines.json', machines)
        self.WaitReady(range(len(self.nodes)), timeout=self.workers.context.get('readiness_timeout', 300))
        self.WaitConnected('readiness.json')

    def Close(self):
        for client in self.clients:
            client.Close()
        self.pool.shutdown(wait=True)
        if self.touched:
            for action in ('stop', 'collect'):
                try:
                    self.workers.Call(action)
                except Exception as error:
                    self.cleanup_errors.append(str(error))
        # cluster_benchmark.yml always runs cleanup-group, including partial starts.


def Run(context):
    plan, genesis = Validate(context)
    output = Path(context['output'])
    output.mkdir(parents=True, exist_ok=False)
    Save(output / 'context.json', context)
    cluster = SparseCluster(context, plan, genesis)
    original = Experiments.Load
    indices = [index for index, node in enumerate(plan['nodes']) if node['id'] in plan['injectors']]

    class InjectorLoad(original):
        def __init__(self, full_cluster, *arguments, **kwargs):
            view = SimpleNamespace(nodes=[full_cluster.nodes[index] for index in indices], offline=set())
            super().__init__(view, *arguments, **kwargs)

    Experiments.Load = InjectorLoad
    result = {'ok': False, 'status': 'failed', 'nodes': len(plan['nodes']), 'injectors': plan['injectors'], 'graph': plan['graph']}
    phase = 'start'
    try:
        cluster.Start()
        phase = 'benchmark'
        parameters = {key: context[key] for key in ('warmup', 'duration', 'drain_seconds', 'poll_seconds', 'sample_hz', 'batch', 'wallets')}
        parameters.update(ingress='shard', deadline_ms=1000, max_backlog_growth=context.get('max_backlog_growth', 10),
                          parallel_ingress=True)
        if context.get('early_stop_enabled', True):
            parameters['stop_when'] = LowTpsStop(context['rate'], context['warmup'],
                ratio=context.get('early_stop_ratio', .7), start=context.get('early_stop_start_seconds', 30),
                window=context.get('early_stop_window_seconds', 15), windows=context.get('early_stop_windows', 3))
        result['metrics'] = Experiments.LoadCase(cluster, {'rate': context['rate'], 'suite': 'throughput'},
                                                parameters, context['dataset'], genesis, output)
        result['ok'] = bool(result['metrics']['drained'] and not result['metrics']['unresolved_samples'] and
                            not result['metrics'].get('early_stop'))
        result['status'] = 'stopped_early' if result['metrics'].get('early_stop') else ('complete' if result['ok'] else 'failed')
    except Exception as error:
        result.update(error=str(error), phase=phase, error_type=type(error).__name__,
                      error_location=[{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                                      for frame in traceback.extract_tb(error.__traceback__)])
    finally:
        Experiments.Load = original
        cluster.Close()
        sweeps = [row['seconds'] for row in cluster.query_sweeps]
        result['query_sweep_seconds'] = {'count': len(sweeps), 'p50': Experiments.Percentile(sweeps, .5),
                                        'p95': Experiments.Percentile(sweeps, .95), 'max': max(sweeps, default=None)}
        result['metrics_period_seconds'] = context['metrics_period']
        Save(output / 'query-sweeps.json', cluster.query_sweeps)
        if cluster.cleanup_errors:
            result.update(ok=False, status='failed', cleanup_errors=cluster.cleanup_errors)
        Save(output / 'result.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', type=Path, required=True)
    args = parser.parse_args()
    outcome = Run(json.loads(args.context.read_text(encoding='utf-8')))
    print(json.dumps(outcome))
    raise SystemExit(0 if outcome['ok'] else 1)
