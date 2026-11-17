#!/usr/bin/env python3
"""Fixed twenty-node native-transfer release or block-step comparison via Ansible.

The archived r2 measurement code supplies the unchanged batch128 broadcast
sender, exact 2s/12s head observations and independent persisted ledger audit.
No credentials, SSH subprocesses or permanent-service operations live here.
"""
import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
METHODS = REPO / 'reports/2026-11-16-ten-twenty-retest/methods/r2'
RATES = (20000, 30000)
PORT = 18089
DATASET_COUNT = 720000
DATASET_SHA256 = '4038ab1b4a1517a00e8fb7587c7c752310b2aef3b849057bc2ac2e24c3a87e36'
GENESIS_SHA256 = 'c7d9198003bebf791e0f0977dfedf8755a5b7ea26f6cdb75ea2780ed3886e998'
ID = re.compile(r'[A-Za-z0-9_-]{1,65}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def Save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')


def Require(condition, message):
    if not condition:
        raise ValueError(message)


def Digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def PlaybookPath(context):
    explicit = context.get('ansible_playbook')
    executable = Path(explicit) if explicit else Path(context.get('controller_python') or sys.executable).parent / 'ansible-playbook'
    Require(executable.is_absolute(), 'ansible_playbook/controller_python must identify an absolute executable path')
    return str(executable)


def ComparisonSchedule(context):
    releases = context.get('releases')
    if 'block_tx_steps' in context:
        steps = context['block_tx_steps']
        Require(isinstance(steps, list) and steps and
                all(type(step) is int and 0 <= step <= 10000 for step in steps),
                'block_tx_steps must be a nonempty list of integers from 0 to 10000')
        Require(len(set(steps)) == len(steps), 'block_tx_steps must not contain duplicates')
        Require(isinstance(releases, list) and len(releases) == 1,
                'Block-step comparison requires exactly one pinned release')
    else:
        steps = [None]
        Require(isinstance(releases, list) and len(releases) == 2,
                'Exactly two ordered A/B releases are required')
    variants = [(release, step) for release in releases for step in steps]
    # RPC31 observation changes measurement overhead: keep it out of TPS cases.
    return ([(release, 'throughput', rate, step) for rate in RATES for release, step in variants] +
            [(release, 'latency', 20000, step) for release, step in variants])


def ReadContext(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    for name in ('inventory_file', 'ansible_dir', 'test_root', 'result_dir', 'dataset', 'genesis_file'):
        Require(isinstance(value.get(name), str) and Path(value[name]).is_absolute(), name + ' must be absolute')
    root = PurePosixPath(value['test_root'])
    Require(root.is_absolute() and '..' not in root.parts and len(root.parts) >= 4 and
            root.parts[:3] == ('/', 'var', 'tmp'), 'test_root must be isolated below /var/tmp')
    Require(ID.fullmatch(str(value.get('run_id', ''))), 'Invalid run_id')
    Require(re.fullmatch(r'[0-9a-f]{32}', str(value.get('run_owner', ''))), 'run_owner must be a lowercase UUID hex ownership token')
    nodes = value.get('nodes')
    Require(isinstance(nodes, list) and len(nodes) == 20, 'Exactly twenty context nodes are required')
    ids, endpoints = set(), set()
    for node in nodes:
        name = node.get('node_id')
        Require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,40}', name) and name not in ids,
                'Invalid or duplicate node_id')
        address = str(ipaddress.ip_address(node['p2p_address']))
        Require(type(node.get('port')) is int and node['port'] == PORT, 'All test ports must be 18089')
        Require((address, PORT) not in endpoints and bool(node.get('ansible_host')), 'Duplicate endpoint or missing ansible_host')
        node['p2p_address'] = address
        ids.add(name)
        endpoints.add((address, PORT))
    releases = value.get('releases')
    ComparisonSchedule(value)
    release_ids = set()
    for release in releases:
        Require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', str(release.get('id', ''))) and
                release['id'] not in release_ids, 'Invalid or duplicate release id')
        Require(PurePosixPath(release['directory']).is_absolute() and
                PurePosixPath(release['directory']).name == release['id'], 'Release directory/id mismatch')
        Require(SHA256.fullmatch(str(release.get('sha256', ''))), 'Pinned release boost SHA256 is required')
        release_ids.add(release['id'])
    for name in ('dataset_sha256', 'genesis_sha256'):
        Require(SHA256.fullmatch(str(value.get(name, ''))), name + ' is required')
    Require(value['dataset_sha256'] == DATASET_SHA256 and value['genesis_sha256'] == GENESIS_SHA256,
            'This comparison requires the pinned original dataset and genesis')
    Require(Path(value['dataset']).stat().st_size == DATASET_COUNT * 176, 'The original 720000-transfer dataset is required')
    Require(Digest(value['dataset']) == value['dataset_sha256'] and
            Digest(value['genesis_file']) == value['genesis_sha256'], 'Immutable dataset/genesis SHA256 mismatch')
    genesis = json.loads(Path(value['genesis_file']).read_text(encoding='utf-8'))
    Require(len(genesis.get('accounts', [])) == 1024, 'Expected the original 1024 genesis accounts')
    # OfflineAudit locates no sidecar itself; only the explicitly pinned file is used.
    Require(Path(value['inventory_file']).is_file() and
            (Path(value['ansible_dir']) / 'workers.yml').is_file(), 'Missing inventory or workers.yml')
    inventories = value.get('inventory_files') or [value['inventory_file']]
    Require(isinstance(inventories, list) and inventories and all(isinstance(item, str) and item for item in inventories),
            'inventory_files must be a nonempty list of inventory sources')
    Require(isinstance(value.get('ansible_extra_vars', {}), dict), 'ansible_extra_vars must be an object')
    Require(Path(PlaybookPath(value)).is_file(), 'The controller Python environment must contain ansible-playbook')
    if value.get('extra_vars_file'):
        Require(Path(value['extra_vars_file']).is_absolute() and Path(value['extra_vars_file']).is_file(),
                'extra_vars_file must be an existing absolute path')
    return value, genesis


class AnsibleWorkers:
    """One playbook invocation per full-fleet lifecycle operation."""
    def __init__(self, context, output, runner=subprocess.run):
        self.context, self.output, self.runner = context, Path(output), runner
        self.sequence = 0
        self.lock = threading.Lock()

    def Call(self, action, run, release, genesis=None, arguments=None, restart=False):
        Require(action in ('start', 'stop', 'collect', 'delete-db', 'export-headers'), 'Unsupported action')
        with self.lock:
            self.sequence += 1
            output = self.output / ('%03d-%s-%s' % (self.sequence, run, action))
            output.mkdir(parents=True, exist_ok=False)
            variables = dict(self.context.get('ansible_extra_vars', {}))
            variables.update(chain_worker_action=action, chain_worker_release_id=release['id'],
                chain_run_id=run, chain_run_group=self.context['run_id'],
                chain_run_owner=self.context['run_owner'],
                chain_worker_results_dir=str(output), chain_worker_genesis=genesis or {},
                chain_worker_arguments_by_node=arguments or {}, chain_worker_restart=restart,
                chain_test_root=self.context['test_root'], chain_test_port=PORT,
                chain_guard_seconds=300)
            variable_file = output / 'request.json'
            Save(variable_file, variables)
            command = [PlaybookPath(self.context)]
            for inventory in self.context.get('inventory_files') or [self.context['inventory_file']]:
                command += ['-i', inventory]
            command += [str(Path(self.context['ansible_dir']) / 'workers.yml'), '--forks', '20']
            if self.context.get('extra_vars_file'):
                command += ['--extra-vars', '@' + self.context['extra_vars_file']]
            command += ['--extra-vars', '@' + str(variable_file)]
            completed = self.runner(command, cwd=self.context['ansible_dir'], capture_output=True,
                text=True, encoding='utf-8', errors='replace', timeout=420)
            # Retain a bounded diagnostic tail only on failure. Values from
            # credential-like environment variables are removed before saving.
            diagnostics = {}
            if completed.returncode:
                for stream in ('stdout', 'stderr'):
                    tail = str(getattr(completed, stream, '') or '')[-4000:]
                    for key, secret in os.environ.items():
                        if secret and re.search(r'PASSWORD|PASSWD|TOKEN|SECRET|PRIVATE_KEY', key, re.I):
                            tail = tail.replace(secret, '[redacted]')
                    diagnostics[stream + '_tail'] = tail
            values, errors = {}, {}
            for node in self.context['nodes']:
                name = node['node_id']
                choices = (output / (name + '.json'), output / (name + '-' + action + '.json'))
                paths = [path for path in choices if path.is_file()]
                try:
                    Require(len(paths) == 1, name + ': missing or duplicate worker artifact')
                    result = json.loads(paths[0].read_text(encoding='utf-8'))
                    Require(result.get('ok') is True and isinstance(result.get('result'), dict),
                            name + ': worker action failed')
                    values[name] = result['result']
                except (ValueError, OSError, TypeError) as error:
                    errors[name] = str(error)
            Save(output / 'summary.json', dict(action=action, returncode=completed.returncode,
                 nodes=list(values), errors=errors, diagnostics=diagnostics))
            if completed.returncode or errors or len(values) != 20:
                raise RuntimeError('Ansible %s failed (exit=%s); inspect %s' %
                                   (action, completed.returncode, output))
            return values


def Discover(nodes):
    """Fill the existing seed graph through the native IPv4 discovery message."""
    for index, node in enumerate(nodes[:-1]):
        peers = nodes[index + 1:]
        Require(all(ipaddress.ip_address(peer['host']).version == 4 for peer in peers),
                'This pinned comparison uses the original IPv4 discovery wire format')
        payload = b''.join(socket.inet_aton(peer['host']) + struct.pack('<H', peer['port']) for peer in peers)
        with socket.create_connection((node['host'], node['port']), timeout=1) as connection:
            connection.sendall(b'\x02' + struct.pack('<BI', 9, len(payload)) + payload)
            connection.shutdown(socket.SHUT_WR)
            while connection.recv(1024):
                pass


def ClusterType(C):
    class AnsibleCluster(C.Cluster):
        def __init__(self, workers, release, nodes, run, genesis, settings, output):
            super().__init__(nodes, run, genesis, settings, output)
            self.workers, self.release = workers, release
            self.stop_lock, self.stopped = threading.RLock(), False
            self.stop_results, self.audit_collect, self.exports = {}, None, None
            self.audit_requests, self.audit_results = {}, None
            self.audit_barrier, self.audit_event = threading.Barrier(len(nodes)), threading.Event()
            self.audit_error = None
            self.touched = False

        def Start(self):
            arguments = {}
            for index, node in enumerate(self.nodes):
                settings = {**self.settings, 'clock-offset-ms': 0, 'clock-drift-ppm': 0,
                            'port': node['port'], 'bind': node['host'], 'max-peers': 64,
                            'max-connections': 128}
                argv = [part for key, value in settings.items() for part in ('--' + key, str(int(value) if isinstance(value, bool) else value))]
                for peer in self.nodes[index + 1:]:
                    endpoint = '[%s]:%s' % (peer['host'], peer['port']) if ':' in peer['host'] else '%s:%s' % (peer['host'], peer['port'])
                    argv += ['--seed', endpoint]
                arguments[node['id']] = argv
                self.starts[index] = dict(action='start', run=self.run, binary=node['binary'], genesis=self.genesis, arguments=argv)
            self.touched = True  # Partial playbook success must still be cleaned.
            self.metadata = self.workers.Call('start', self.run, self.release, self.genesis, arguments)
            self.started.update(range(len(self.nodes)))
            Require({row.get('binary_sha256') for row in self.metadata.values()} == {self.release['sha256']},
                    'Started binary differs from pinned release SHA256')
            self.WaitReady(range(len(self.nodes)))
            Discover(self.nodes)
            deadline = time.monotonic() + 30
            while True:
                stats = self.Stats()
                if all(not row['failed'] and row['peers'] >= 19 for row in stats):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('All twenty nodes did not form the full peer graph')
                time.sleep(.1)
            Save(self.output / 'readiness.json', dict(zip([node['id'] for node in self.nodes], stats)))
            Save(self.output / 'machines.json', self.metadata)
            Save(self.output / 'load-starts.json', self.starts)

        def StopAll(self):
            with self.stop_lock:
                if not self.stopped:
                    for client in self.clients:
                        client.Close()
                    self.stop_results = self.workers.Call('stop', self.run, self.release)
                    self.stopped = True
                    self.offline.update(range(len(self.nodes)))
                return self.stop_results

        def StopOne(self, index):
            return self.StopAll()[self.nodes[index]['id']]

        def ExportHeaders(self, node, run):
            Require(run == self.run and self.stopped, 'Export requires this case to be stopped')
            with self.stop_lock:
                if self.exports is None:
                    self.exports = self.workers.Call('export-headers', self.run, self.release)
            return self.exports[node['id']]

        def PrepareAudit(self):
            Require(self.stopped, 'The load must stop before persisted auditing')
            self.audit_collect = self.workers.Call('collect', self.run, self.release)

        def AuditWorker(self, node, request):
            Require(request.get('run') == self.run, 'Audit worker run mismatch')
            if request['action'] == 'collect':
                return self.audit_collect[node['id']]
            Require(request['action'] == 'start' and request.get('restart') is True,
                    'Only a stopped audit restart is permitted')
            arguments = [str(value) for value in request['arguments']]
            # Archived state audit did not query sample positions after reopen.
            # The final RPC31 checks require the persisted index to stay enabled.
            if '--experiment-index' not in arguments:
                arguments += ['--experiment-index', '1']
            if 'block-tx-step' in self.settings and '--block-tx-step' not in arguments:
                arguments += ['--block-tx-step', str(self.settings['block-tx-step'])]
            self.audit_requests[node['id']] = arguments
            leader = self.audit_barrier.wait(timeout=60) == 0
            if leader:
                try:
                    self.stopped = False
                    self.offline.clear()
                    self.audit_results = self.workers.Call('start', self.run, self.release,
                        self.genesis, self.audit_requests, restart=True)
                    Require({row.get('binary_sha256') for row in self.audit_results.values()} == {self.release['sha256']},
                            'Audit restart binary SHA256 differs')
                except BaseException as error:
                    self.audit_error = error
                finally:
                    self.audit_event.set()
            Require(self.audit_event.wait(450), 'Audit restart did not finish')
            if self.audit_error:
                raise RuntimeError('Audit restart failed') from self.audit_error
            return self.audit_results[node['id']]

        def Close(self):
            if self.touched:
                try:
                    self.StopAll()
                except Exception as error:
                    self.cleanup_errors.append('stop: ' + str(error))
                try:
                    values = self.workers.Call('collect', self.run, self.release)
                    for name, value in values.items():
                        Save(self.output / (name + '.json'), value)
                except Exception as error:
                    self.cleanup_errors.append('collect: ' + str(error))
                try:
                    values = self.workers.Call('delete-db', self.run, self.release)
                    for name, value in values.items():
                        Save(self.output / (name + '-database-cleanup.json'), value)
                        Require(value.get('removed') is True, name + ': database removal unproved')
                except Exception as error:
                    self.cleanup_errors.append('delete-db: ' + str(error))
            for client in self.clients:
                client.Close()
            self.pool.shutdown(wait=True)
            Save(self.output / 'cleanup-errors.json', self.cleanup_errors)
    return AnsibleCluster


class LatencyObserver:
    def __init__(self, C, load):
        self.load, self.stop, self.rounds = load, threading.Event(), []
        self.clients = [C.NodeClient(node) for node in load.cluster.nodes]
        self.pool = concurrent.futures.ThreadPoolExecutor(len(self.clients))
        self.thread = threading.Thread(target=self.Run, daemon=True)

    def Query(self, index, payload):
        client = self.clients[index]
        started = time.monotonic()
        try:
            if client.socket is None:
                client.socket = socket.create_connection((client.node['host'], client.node['port']), timeout=.4)
                client.socket.sendall(b'\x02')
            client.socket.settimeout(.4)
            value, error = client.Query(31, payload), None
        except Exception as exc:
            value, error = None, type(exc).__name__
        return dict(node=client.node['id'], start_monotonic=started,
                    end_monotonic=time.monotonic(), value=value, error=error)

    def Run(self):
        while not self.stop.wait(.02):
            if self.load.started and time.monotonic() >= self.load.started + 17:
                return
            with self.load.lock:
                pending = [sample for sample in self.load.samples if sample['included_at'] is None and
                           self.load.started + 2 <= sample['scheduled'] < self.load.started + 12][:128]
            if not pending:
                continue
            payload = b''.join(bytes.fromhex(sample['hash']) for sample in pending)
            rows = list(self.pool.map(lambda index: self.Query(index, payload), range(len(self.clients))))
            self.rounds.append(dict(sample_indices=[sample['index'] for sample in pending], nodes=rows))
            if not all(isinstance(row['value'], list) and len(row['value']) == len(pending) for row in rows):
                continue
            confirmed_at = max(row['end_monotonic'] for row in rows)
            if confirmed_at > self.load.started + 17:
                continue
            with self.load.lock:
                for column, sample in enumerate(pending):
                    values = [row['value'][column] for row in rows]
                    position = values[0]
                    if position is not None and all(value == position for value in values):
                        sample['included_at'] = confirmed_at
                        sample['observed_position'] = position
                        sample['confirmation_rpc_intervals'] = [
                            dict(node=row['node'], start_monotonic=row['start_monotonic'], end_monotonic=row['end_monotonic']) for row in rows]

    def Close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        Require(not self.thread.is_alive(), 'Latency observer did not stop')
        for client in self.clients:
            client.Close()
        self.pool.shutdown(wait=True)


def LatencySummary(samples, expected, final_positions, canonical, names):
    """Keep missing observations in the complete, predeclared sample population."""
    by_index = {sample['index']: sample for sample in samples}
    output = []
    for item in expected:
        actual = by_index.get(item['index'], {})
        sample = {**item, **actual}
        position = sample.get('observed_position')
        persisted = bool(position and set(final_positions) == set(names) and set(canonical) == set(names))
        if persisted:
            for name in names:
                ledger = canonical[name]
                at = final_positions[name].get(sample['hash'])
                height = position.get('height')
                persisted = bool(persisted and at == position and type(height) is int and 1 <= height <= len(ledger)
                                 and ledger[height - 1] and ledger[height - 1].get('hash') == position.get('block'))
        observed = sample.get('included_at')
        good = bool(persisted and actual.get('hash') == item['hash'] and
                    type(observed) in (int, float) and math.isfinite(observed) and
                    type(sample.get('send_begin')) in (int, float) and
                    sample['scheduled'] <= sample['send_begin'] <= observed)
        sample['persisted_on_all_canonical_chains'] = persisted
        sample['observation_complete'] = good
        sample['scheduled_to_all_confirmation_upper_ms'] = (observed - sample['scheduled']) * 1000 if good else None
        sample['batch_start_to_all_confirmation_upper_ms'] = (observed - sample['send_begin']) * 1000 if good else None
        sample['within_one_second'] = bool(good and sample['scheduled_to_all_confirmation_upper_ms'] <= 1000)
        output.append(sample)
    population = sorted(sample['scheduled_to_all_confirmation_upper_ms'] if sample['observation_complete'] else math.inf for sample in output)
    def Percentile(fraction):
        value = population[max(0, math.ceil(len(population) * fraction) - 1)] if population else math.inf
        return value if math.isfinite(value) else None
    complete = sum(sample['observation_complete'] for sample in output)
    late = sum(sample['observation_complete'] and not sample['within_one_second'] for sample in output)
    return dict(expected_samples=len(output), observed_and_persisted_samples=complete,
                missing_samples=len(output) - complete, missing_fraction=(len(output) - complete) / len(output) if output else 1,
                p50_upper_ms=Percentile(.5), p95_upper_ms=Percentile(.95), p99_upper_ms=Percentile(.99),
                unbounded_samples_in_percentile_population=len(output) - complete,
                observed_above_one_second_samples=late,
                unresolved_sample_note='Missing observations are unresolved confirmation times and conservatively fail the deadline; they do not prove absence of a commit.',
                within_one_second_fraction=sum(sample['within_one_second'] for sample in output) / len(output) if output else 0,
                all_samples_within_one_second=bool(output) and all(sample['within_one_second'] for sample in output),
                passed=len(output) == 100 and complete == 100, samples=output,
                definition='controller monotonic scheduled arrival or batch-send begin to completion of the first RPC31 round reporting the same canonical block on all 20 nodes; includes batching, transmission, polling and RPC delay; positions rechecked against final persisted canonical ledgers')


def MakeExpected(dataset, rate):
    result = []
    with Path(dataset).open('rb') as source:
        for index in range(rate * 2, rate * 12, rate // 10):
            source.seek(index * 176 + 80)
            result.append(dict(index=index, hash=source.read(32).hex(), scheduled_offset_seconds=index / rate))
    return result


def DrainWithinBudget(observation, accepted, names):
    rows = (observation or {}).get('nodes', [])
    heads = [value.get('head') for value in rows]
    return bool(len(rows) == 20 and {row.get('node') for row in rows} == set(names) and all(heads) and
        len({head['hash'] for head in heads}) == 1 and
        all(head['transactions'] in accepted for head in heads) and
        all(not value.get('error') and 12 <= value['start_seconds'] <= value['end_seconds'] <= 17 for value in rows))


def InstallMeasurement(E, C, release, latency):
    """Wrap only the existing sender's constructor; its wire/scheduling stays intact."""
    import LightObserve
    original_load = E.Load
    registered, observers = [], []
    class SampledLoad(original_load):
        def __init__(self, cluster, dataset, rate, total_seconds, pattern, ingress, sample_hz, batch=128, parallel_ingress=False):
            super().__init__(cluster, dataset, rate, total_seconds, pattern, ingress,
                             10 if latency else sample_hz, batch, parallel_ingress)
            registered.append(self)
            if latency:
                observer = LatencyObserver(C, self)
                observers.append(observer)
                observer.thread.start()
    E.Load = SampledLoad
    try:
        LightObserve.Install(SimpleNamespace(source=REPO, expected_binary_sha256=release['sha256']))
    finally:
        E.Load = original_load
    return registered, observers


def RunCase(context, genesis, workers, release, kind, rate, ordinal, modules, block_tx_step=None):
    C, E, R = modules
    if block_tx_step is not None:
        Require(type(block_tx_step) is int and 0 <= block_tx_step <= 10000, 'Invalid case block_tx_step')
    suffix = '' if block_tx_step is None else '-step%s' % block_tx_step
    name = '%02d-%s-%s-%s' % (ordinal, release['id'], kind, rate) + suffix
    run = context['case_prefix'] + '-%02d-%s-%s' % (ordinal, kind, rate) + suffix
    Require(len(run) <= 100 and ID.fullmatch(context['run_id']), 'Case run identifier exceeds worker limit')
    output = Path(context['result_dir']) / name
    output.mkdir()
    nodes = [dict(id=node['node_id'], host=node['p2p_address'], port=PORT, bind=node['p2p_address'],
                  binary=str(PurePosixPath(release['directory']) / 'bin/boost'), root=context['test_root'])
             for node in context['nodes']]
    settings = {'io-threads': 4, 'verify-threads': 8, 'sync': 1, 'produce': 1,
                'experiment-index': 1, 'clock-sync': 1, 'sync-period-ms': 100,
                'sync-gain': .25, 'round-ms': 50, 'candidate-ms': 12,
                'relay-transactions': 0, 'block-ms': 50, 'max-block-txs': 6000, 'run-seconds': 300}
    if block_tx_step is not None:
        settings['block-tx-step'] = block_tx_step
    cluster = ClusterType(C)(workers, release, nodes, run, genesis, settings, output)
    case = dict(suite='throughput', mode='throughput', rate=rate, nodes=20, repeat=1,
                pattern='steady', group=kind, revision=release['id'])
    step_metadata = {} if block_tx_step is None else dict(block_tx_step=block_tx_step)
    case.update(step_metadata)
    plan = json.loads((REPO / 'Experiments/plan.json').read_text(encoding='utf-8'))
    plan.update(nodes=20, node_counts=[20], throughput_rates=list(RATES), throughput_modes=['throughput'],
                warmup=2, duration=10, drain_seconds=5, repeats=1, parallel_ingress=False,
                wallets=1024, batch=128, ingress='broadcast')
    if block_tx_step is not None:
        plan.setdefault('node_settings', {})['block-tx-step'] = block_tx_step
    E.ValidatePlan(plan)
    Save(output / 'protocol.json', dict(case=case, plan=plan, release=release,
         node_settings=settings, **step_metadata,
         dataset_sha256=context['dataset_sha256'], genesis_sha256=context['genesis_sha256'], port=PORT))
    row = dict(case=case, run=run, status='failed', success=False, **step_metadata)
    registered, observers = InstallMeasurement(E, C, release, kind == 'latency')
    try:
        cluster.Start()
        row['metrics'] = E.LoadCase(cluster, case, plan, Path(context['dataset']), genesis, output)
        for observer in observers:
            observer.Close()
        cluster.PrepareAudit()
        facade = SimpleNamespace(ExportHeaders=cluster.ExportHeaders, Worker=cluster.AuditWorker, NodeClient=C.NodeClient)
        row['state_audit'], row['audited_measurement'] = R.OfflineAudit(facade, cluster, run, output,
            genesis, context['dataset'], row['metrics']['sent_unique_attempted_upper_bound'])
        accepted = {row['metrics']['sent_unique_completed_all_ingress'], row['metrics']['sent_unique_attempted_upper_bound']}
        row['final_state_matches_accepted'] = row['state_audit']['passed'] and all(
            node['confirmed'] in accepted for node in row['state_audit']['nodes'])
        # Ansible dispatch has nonzero latency. Stopped DB equality alone cannot
        # prove that the fixed five-second drain budget was met.
        row['drained_within_budget'] = DrainWithinBudget(row['metrics'].get('head_drain_observation'),
                                                       accepted, [node['id'] for node in nodes])
        row['drained'] = bool(row['final_state_matches_accepted'] and row['drained_within_budget'])
        if kind == 'latency':
            Require(len(registered) == 1, 'Expected exactly one original sender')
            load = registered[0]
            expected = MakeExpected(context['dataset'], rate)
            for sample in expected:
                sample['scheduled'] = load.started + sample['scheduled_offset_seconds']
            payload = b''.join(bytes.fromhex(sample['hash']) for sample in expected)
            def Final(index):
                client = C.NodeClient(nodes[index])
                try:
                    positions = client.Query(31, payload)
                    Require(isinstance(positions, list) and len(positions) == len(expected), 'Final RPC31 sample set incomplete')
                    return dict(zip([sample['hash'] for sample in expected], positions))
                finally:
                    client.Close()
            final = dict(zip([node['id'] for node in nodes], cluster.All(Final)))
            canonical = {node['id']: json.loads((output / (node['id'] + '-canonical-index.json')).read_text()) for node in nodes}
            row['latency'] = LatencySummary(load.samples, expected, final, canonical, [node['id'] for node in nodes])
            Save(output / 'latency-final-positions.json', final)
            Save(output / 'latency-rpc.json', observers[0].rounds)
            Save(output / 'latency-samples.json', row['latency']['samples'])
        row['status'] = 'complete'
        row['tps_valid'] = bool(row['metrics']['load_success'] and row['state_audit']['passed'] and
                                row['audited_measurement']['passed'])
        row['success'] = bool(row['tps_valid'] and (kind != 'latency' or row['latency']['passed']))
    except BaseException as error:
        row['error'] = type(error).__name__ + ': ' + str(error)
        row['interrupted'] = isinstance(error, (KeyboardInterrupt, SystemExit))
    finally:
        previous_signals = {signum: signal.signal(signum, signal.SIG_IGN) for signum in (signal.SIGINT, signal.SIGTERM)}
        try:
            for observer in observers:
                if observer.thread.is_alive():
                    try:
                        observer.Close()
                    except Exception as error:
                        row['observer_cleanup_error'] = str(error)
                        row['success'] = False
            cluster.Close()
            row['cleanup_errors'] = cluster.cleanup_errors
            row['success'] = bool(row['success'] and not cluster.cleanup_errors)
            Save(output / 'result.json', row)
        finally:
            for signum, handler in previous_signals.items():
                signal.signal(signum, handler)
    return row


def Run(path):
    context, genesis = ReadContext(path)
    output = Path(context['result_dir'])
    Require(not output.exists(), 'Use a new result_dir')
    output.mkdir(parents=True)
    context['case_prefix'] = context['run_id'][:40] + '-' + uuid.uuid4().hex[:10]
    sys.path.insert(0, str(REPO / 'Test'))
    sys.path.insert(0, str(METHODS))
    import Cluster as C
    import Experiments as E
    import RetestCases as R
    workers = AnsibleWorkers(context, output / 'ansible')
    summary = dict(passed=False, cases=[], run_id=context['run_id'], run_owner=context['run_owner'],
        fixed_protocol=dict(nodes=20, port=PORT,
        rates=list(RATES), warmup=2, duration=10, drain_seconds=5, repeats=1,
        dataset_transactions=DATASET_COUNT, latency_rate=20000, latency_sample_hz=10),
        dataset_sha256=context['dataset_sha256'], genesis_sha256=context['genesis_sha256'])
    if 'block_tx_steps' in context:
        summary['fixed_protocol']['block_tx_steps'] = context['block_tx_steps']
    Save(output / 'protocol.json', dict(**summary['fixed_protocol'], releases=context['releases'],
         case_prefix=context['case_prefix'],
         run_id=context['run_id'], run_owner=context['run_owner'],
         methods_directory=str(METHODS), method_hashes={name: Digest(METHODS / name) for name in
         ('RetestCases.py', 'RetestObserve.py', 'LightObserve.py', 'HeaderReader.py')}))
    schedule = ComparisonSchedule(context)
    def Interrupted(signum, frame):
        raise KeyboardInterrupt('Controller interrupted by signal ' + str(signum))
    previous_signals = {signum: signal.signal(signum, Interrupted) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        for ordinal, (release, kind, rate, step) in enumerate(schedule, 1):
            progress = dict(starting=ordinal, release=release['id'], kind=kind, rate=rate)
            if step is not None:
                progress['block_tx_step'] = step
            print(json.dumps(progress), flush=True)
            row = RunCase(context, genesis, workers, release, kind, rate, ordinal, (C, E, R), step)
            summary['cases'].append(row)
            Save(output / 'results.json', summary)
            if row.get('interrupted'):
                raise KeyboardInterrupt('Comparison interrupted; owned case cleanup finished')
            if row.get('cleanup_errors') or row.get('observer_cleanup_error'):
                raise RuntimeError('Cleanup was not confirmed; refusing further cases')
        Require(Digest(context['dataset']) == context['dataset_sha256'] and
                Digest(context['genesis_file']) == context['genesis_sha256'], 'Dataset/genesis changed during comparison')
        summary['passed'] = len(summary['cases']) == len(schedule) and all(row['success'] for row in summary['cases'])
        summary['latency_one_second_target_passed'] = all(row['latency']['all_samples_within_one_second']
             for row in summary['cases'] if row['case']['group'] == 'latency') and len(summary['cases']) == len(schedule)
    except BaseException as error:
        summary['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        try:
            Save(output / 'results.json', summary)
        finally:
            for signum, handler in previous_signals.items():
                signal.signal(signum, handler)
    print(json.dumps(dict(passed=summary['passed'], cases=len(summary['cases']), result_dir=str(output))), flush=True)
    return 0 if summary['passed'] else 1


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ansible-context', type=Path, required=True)
    args = parser.parse_args()
    return Run(args.ansible_context)


if __name__ == '__main__':
    try:
        sys.exit(Main())
    except Exception as error:
        print(json.dumps(dict(passed=False, error=type(error).__name__ + ': ' + str(error))), flush=True)
        sys.exit(1)
