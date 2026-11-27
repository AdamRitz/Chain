"""Five reproducible cluster experiments. Run --help; see EXPERIMENTS.md."""
import argparse
import collections
import concurrent.futures
import csv
import hashlib
import json
import math
import mmap
from pathlib import Path
import platform
import random
import re
import socket
import statistics
import struct
import threading
import time
import psutil
from Cluster import Cluster, LocalInventory, ReadInventory
from Netem import NetemCommands
from NetworkTest import ExeName, Frame, Run

MODES = {'latency': {'block-ms': 10, 'max-block-txs': 512},
         'throughput': {'block-ms': 50, 'max-block-txs': 6000}}


def Percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered)*fraction)-1)]


def Save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def ArrivalTime(index, rate, pattern):
    if pattern == 'steady':
        return index/rate
    cycle, within = divmod(index, rate)
    return cycle + within/(rate*4)


class Load:
    def __init__(self, cluster, dataset, rate, total_seconds, pattern, ingress, sample_hz, batch=128, parallel_ingress=False):
        self.cluster, self.dataset = cluster, Path(dataset)
        self.rate, self.count = rate, int(rate*total_seconds)
        self.pattern, self.ingress, self.batch = pattern, ingress, batch
        self.every = max(1, int(rate/sample_hz))
        self.samples, self.sent, self.wire_transactions = [], 0, 0
        self.sample_cursor = 0
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.error, self.late_ms = None, []
        self.started = 0
        self.parallel_ingress = parallel_ingress

    def Run(self):
        sockets = {}
        workers = concurrent.futures.ThreadPoolExecutor(len(self.cluster.nodes)) if self.parallel_ingress else None
        def Send(index, payload):
            if index in self.cluster.offline:
                if index in sockets: sockets.pop(index).close()
                return 0
            node = self.cluster.nodes[index]
            try:
                if index not in sockets:
                    connection = socket.create_connection((node['host'], node['port']), timeout=5)
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    connection.sendall(b'\x02'); sockets[index] = connection
                sockets[index].sendall(Frame(10, payload))
                return len(payload)//176
            except OSError:
                if index in sockets: sockets.pop(index).close()
                if index not in self.cluster.offline: raise
                return 0
        try:
            with self.dataset.open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
                if len(data) < self.count*176:
                    raise ValueError('Dataset is smaller than offered load')
                self.started = time.monotonic()
                for begin in range(0, self.count, self.batch):
                    if self.stop.is_set():
                        break
                    end = min(begin+self.batch, self.count)
                    # The last transaction in the batch determines send time; each sample keeps its own arrival time.
                    scheduled = self.started+ArrivalTime(end-1, self.rate, self.pattern)
                    if self.stop.wait(max(0, scheduled-time.monotonic())):
                        break
                    for index in list(sockets):
                        if index in self.cluster.offline:
                            sockets.pop(index).close()
                    active = [i for i in range(len(self.cluster.nodes)) if i not in self.cluster.offline]
                    if not active:
                        raise RuntimeError('No live ingress nodes')
                    package = data[begin*176:end*176]
                    if self.ingress == 'broadcast':
                        packages = {i: package for i in active}
                    else:
                        packages = collections.defaultdict(bytearray)
                        for offset in range(0, len(package), 176):
                            owner = int.from_bytes(package[offset:offset+8], 'little') % len(self.cluster.nodes)
                            if owner not in active:
                                owner = active[owner % len(active)]
                            packages[owner].extend(package[offset:offset+176])
                    if workers:
                        futures = [workers.submit(Send, index, payload) for index, payload in packages.items()]
                        self.wire_transactions += sum(future.result() for future in futures)
                    else:
                        self.wire_transactions += sum(Send(index, payload) for index, payload in packages.items())
                    sent_at = time.monotonic()
                    with self.lock:
                        self.sent = end
                        self.late_ms.append(max(0, sent_at-scheduled)*1000)
                        for index in range(begin, end):
                            if index % self.every == 0:
                                self.samples.append({'index': index, 'hash': data[index*176+80:index*176+112].hex(),
                                                     'scheduled': self.started+ArrivalTime(index, self.rate, self.pattern),
                                                     'sent_at': sent_at, 'included_at': None})
        except Exception as error:
            self.error = str(error)
        finally:
            if workers: workers.shutdown(wait=True)
            self.finished = time.monotonic()
            for connection in sockets.values():
                connection.close()


def CheckSamples(cluster, load, flush=False):
    if cluster.offline:
        return 0
    with load.lock:
        size = len(load.samples)
        cursor = load.sample_cursor % size if size else 0
        pending = []
        for offset in range(size):
            index = (cursor + offset) % size
            sample = load.samples[index]
            if sample['included_at'] is None:
                pending.append(sample)
                load.sample_cursor = (index + 1) % size
                if not flush and len(pending) == 128:
                    break
    if not pending:
        return 0
    confirmed = 0
    # A final pass checks every pending sample once, including later samples
    # behind an unresolved first page. RPC 31 accepts at most 128 hashes.
    for begin in range(0, len(pending), 128):
        batch = pending[begin:begin + 128]
        payload = b''.join(bytes.fromhex(sample['hash']) for sample in batch)
        rows = cluster.All(lambda index: cluster.clients[index].Query(31, payload))
        now = time.monotonic()
        for column, sample in enumerate(batch):
            values = [row[column] for row in rows]
            if values[0] is not None and all(value == values[0] for value in values):
                sample['included_at'] = now
                sample['block'] = values[0]
                confirmed += 1
    return confirmed


RESOURCE_COUNTERS = ('process_cpu_seconds', 'network_bytes', 'network_sent_bytes',
                     'process_write_bytes', 'db_write_ms')


class NodeResources:
    """Accumulate counter differences without retaining complete node stats."""
    def __init__(self):
        self.previous = None
        self.seconds = {key: 0.0 for key in RESOURCE_COUNTERS}
        self.deltas = {key: 0.0 for key in RESOURCE_COUNTERS}
        self.peak_resident_bytes = None
        self.reorganizations = 0
        self.intervals = 0
        self.resets = 0
        self.clock_sources = set()

    def Add(self, controller_seconds, row):
        resident = row.get('resident_bytes')
        if resident is not None:
            self.peak_resident_bytes = max(self.peak_resident_bytes or 0, resident)
        raw_us = row.get('clock', {}).get('raw_us')
        source = 'node_raw_us' if isinstance(raw_us, (int, float)) and math.isfinite(raw_us) else 'controller'
        timestamp = raw_us / 1e6 if source == 'node_raw_us' else controller_seconds
        current = {key: row.get(key) for key in RESOURCE_COUNTERS}
        current.update(timestamp=timestamp, source=source, reorganizations=row.get('reorganizations', 0))
        previous = self.previous
        if previous is not None:
            seconds = timestamp - previous['timestamp']
            cpu, before_cpu = current['process_cpu_seconds'], previous['process_cpu_seconds']
            restarted = cpu is not None and before_cpu is not None and cpu < before_cpu
            if restarted or current['reorganizations'] < previous['reorganizations']:
                self.resets += 1
            if not restarted and source == previous['source'] and seconds > 0:
                self.clock_sources.add(source)
                self.intervals += 1
                self.reorganizations += max(0, current['reorganizations'] - previous['reorganizations'])
                for key in RESOURCE_COUNTERS:
                    value, before = current[key], previous[key]
                    if value is not None and before is not None and value >= before:
                        self.seconds[key] += seconds
                        self.deltas[key] += value - before
            elif not restarted and source == previous['source'] and seconds == 0:
                # Cluster.Stats reuses a snapshot between refreshes. Keep the
                # original counters and timestamp, not the later poll time.
                return
        self.previous = current

    def Summary(self, name):
        def Rate(key):
            return self.deltas[key] / self.seconds[key] if self.seconds[key] else None
        return {'node': name, 'cpu_core_equivalents': Rate('process_cpu_seconds'),
                'peak_resident_bytes': self.peak_resident_bytes,
                'received_bytes_per_second': Rate('network_bytes'),
                'sent_bytes_per_second': Rate('network_sent_bytes'),
                'process_write_bytes_per_second': Rate('process_write_bytes'),
                'db_write_ms_per_second': Rate('db_write_ms'),
                'measurement_reorganizations': self.reorganizations,
                'statistics_seconds': self.seconds['process_cpu_seconds'],
                'statistics_intervals': self.intervals, 'statistics_resets': self.resets,
                'statistics_time_sources': sorted(self.clock_sources)}


class LoadMetrics:
    """Keep per-poll totals and per-node accumulators; full records stay on disk."""
    def __init__(self, warmup, duration, nodes):
        self.warmup, self.end = warmup, warmup + duration
        self.measured = []
        self.nodes = [NodeResources() for _ in nodes]
        self.reorganizations_sum = self.invalid_or_rejected_sum = self.max_reorg_depth = 0

    def Add(self, point):
        rows = [node for node in point['nodes'] if node]
        self.reorganizations_sum = max(self.reorganizations_sum, sum(node['reorganizations'] for node in rows))
        self.invalid_or_rejected_sum = max(self.invalid_or_rejected_sum, sum(node['invalid'] + node['rejected'] for node in rows))
        self.max_reorg_depth = max([self.max_reorg_depth] + [node['max_reorg_depth'] for node in rows])
        if not self.warmup <= point['seconds'] <= self.end or not point['common']:
            return
        self.measured.append({'seconds': point['seconds'], 'sent': point['sent'], 'common': point['common'],
                              'backlog': sum(node['pool'] + node['pending'] for node in rows),
                              'tx_backlog': sum(node['pool'] + node.get('pending_tx_verify', 0) for node in rows),
                              'pending_block_verify': sum(node.get('pending_block_verify', 0) for node in rows),
                              'pending_metadata': sum(node.get('pending_metadata', 0) for node in rows)})
        for resources, row in zip(self.nodes, point['nodes']):
            if row:
                resources.Add(point['seconds'], row)


def VerifyNativeState(cluster, dataset, genesis, count, wallets):
    vdf_enabled = 'vdf' in genesis and genesis['vdf'].get('enabled', True)
    common = cluster.Common() if vdf_enabled else None
    if vdf_enabled and (not common or common['transactions'] != count):
        raise AssertionError('Common chain does not contain all sent transactions')
    expected = {row['public_key']: {'balance': row['balance'], 'nonce': 0} for row in genesis['accounts']}
    fee = genesis['base_fee']
    # The prepared dataset repeats one sender/receiver cycle with increasing account nonces.
    with Path(dataset).open('rb') as source:
        for index in range(min(wallets, count)):
            tx = source.read(176)
            sender, receiver = tx[:32].hex(), tx[32:64].hex()
            amount, nonce = struct.unpack_from('<QQ', tx, 64)
            if nonce != 1:
                raise ValueError('Experiment dataset must begin with nonce 1')
            repetitions = (count-1-index)//wallets+1
            expected[sender]['balance'] -= repetitions*(amount+fee)
            expected[sender]['nonce'] = repetitions
            expected[receiver]['balance'] += repetitions*amount
    keys = list(expected)
    for begin in range(0, len(keys), 128):
        batch = keys[begin:begin+128]
        rows = cluster.All(lambda index: cluster.clients[index].Query(37, b''.join(bytes.fromhex(k) for k in batch)))
        for index, row in enumerate(rows):
            if row != [expected[key] for key in batch]:
                raise AssertionError(f'Account state mismatch at {cluster.nodes[index]["id"]}')
    states = cluster.All(lambda index: cluster.clients[index].Query(35))
    # VDF empty blocks keep advancing the head during the queries. Account/EVM
    # state must still agree, and the fixed common block is checked again below.
    compared = ([{key: value for key, value in state.items() if key != 'head'} for state in states]
                if vdf_enabled else states)
    if not all(state == compared[0] for state in compared):
        raise AssertionError('Account/contract state differs between nodes' if vdf_enabled else
                             'Chain head or account/contract state differs between nodes')
    if states[0]['transactions'] != count or states[0]['burned'] != count*fee:
        raise AssertionError('Unique transaction count or burned fees mismatch')
    checks = {'accounts_checked_per_node': len(keys), 'state': compared[0]}
    if vdf_enabled:
        anchors = cluster.All(lambda index: cluster.clients[index].Query(29, struct.pack('<Q', common['height'])))
        if not all(anchor == common for anchor in anchors):
            raise AssertionError('Common transaction-containing block changed during state verification')
        checks.update(common_block=common, node_states={node['id']: state for node, state in zip(cluster.nodes, states)})
    return checks


def ClockCase(cluster, plan, output):
    points, started = [], time.monotonic()
    while time.monotonic()-started < plan.get('sync_duration', plan['duration']):
        def Probe(index):
            wall, monotonic = time.time_ns()/1000, time.monotonic_ns()
            raw = cluster.clients[index].Query(27, struct.pack('<Q', monotonic), raw=True)
            elapsed = (time.monotonic_ns()-monotonic)/1000
            token, t2, t3 = struct.unpack('<QQQ', raw)
            if token != monotonic:
                raise ValueError('Clock probe token mismatch')
            return {'offset_us': (t2+t3)/2-(wall+elapsed/2), 'round_trip_us': elapsed-(t3-t2)}
        probes = cluster.All(Probe)
        offsets = [row['offset_us'] for row in probes]
        point = {'seconds': time.monotonic()-started, 'spread_ms': (max(offsets)-min(offsets))/1000,
                 'probe_uncertainty_ms': max(row['round_trip_us'] for row in probes)/2000,
                 'nodes': probes, 'stats': cluster.Stats()}
        points.append(point)
        time.sleep(plan['poll_seconds'])
    Save(output/'clock.json', points)
    # Require five consecutive observations below the threshold, not a single lucky sample.
    convergence = None
    for index in range(4, len(points)):
        if all(point['spread_ms'] <= plan['sync_threshold_ms'] for point in points[index-4:index+1]):
            convergence = points[index-4]['seconds']; break
    return {'final_spread_ms': statistics.median(row['spread_ms'] for row in points[-5:]),
            'convergence_seconds': convergence, 'threshold_ms': plan['sync_threshold_ms'],
            'max_probe_uncertainty_ms': max(row['probe_uncertainty_ms'] for row in points)}


def ApplyFault(cluster, fault):
    if fault['kind'] == 'offline':
        cluster.StopOne(len(cluster.nodes)-1)
        return
    split = len(cluster.nodes)//2
    for index, node in enumerate(cluster.nodes):
        peers = [peer for other, peer in enumerate(cluster.nodes) if other != index and
                 (fault['kind'] != 'partition' or (index < split) != (other < split))]
        settings = {key: value for key, value in fault.items() if key in ['delay_ms', 'jitter_ms', 'loss_percent', 'queue_packets']}
        if fault['kind'] == 'partition':
            settings['loss_percent'] = 100
        cluster.Fault(index, peers, settings)


def RecoverFault(cluster, fault):
    if fault['kind'] == 'offline':
        cluster.Restart(len(cluster.nodes)-1)
    else:
        cluster.ClearFaults()


def LoadCase(cluster, case, plan, dataset, genesis, output):
    warmup, duration = plan['warmup'], plan['duration']
    requested_duration, early_stop = duration, None
    load = Load(cluster, dataset, case['rate'], warmup+duration, case.get('pattern', 'steady'),
                'shard' if case.get('fault', {}).get('kind') == 'partition' else plan['ingress'],
                plan['sample_hz'], plan.get('latency_batch', plan['batch']) if case['suite'] in ['latency', 'fault'] else plan['batch'], plan.get('parallel_ingress', False))
    thread = threading.Thread(target=load.Run, daemon=True)
    metrics, events = LoadMetrics(warmup, duration, cluster.nodes), []
    applied = recovered = False
    fault = case.get('fault')
    recovery_started, recovery_seconds = None, None
    recovery_observations = []
    controller = psutil.Process()
    cpu_start = controller.cpu_times()
    controller_start = time.monotonic()
    thread.start()
    while not load.started and thread.is_alive():
        time.sleep(.001)
    try:
        deadline = load.started+warmup+duration+plan['drain_seconds']
        with (output/'timeline.jsonl').open('w', encoding='utf-8') as timeline:
            while time.monotonic() < deadline:
                elapsed = time.monotonic()-load.started
                if fault and not applied and elapsed >= warmup+fault['start']:
                    applied = True
                    ApplyFault(cluster, fault)
                    events.append({'event': 'fault_applied', 'seconds': time.monotonic()-load.started})
                if fault and applied and not recovered and elapsed >= warmup+fault['start']+fault['duration']:
                    recovery_started = time.monotonic()
                    RecoverFault(cluster, fault); recovered = True
                    events.append({'event': 'fault_removed', 'seconds': time.monotonic()-load.started,
                                   'recovery_started_seconds': recovery_started-load.started})
                stats = cluster.Stats()
                if any(row and row['failed'] for row in stats):
                    raise RuntimeError('A node reported an execution failure')
                common = cluster.Common(stats)
                point = {'seconds': time.monotonic()-load.started, 'sent': load.sent,
                         'common': common, 'nodes': stats}
                metrics.Add(point); timeline.write(json.dumps(point)+'\n'); timeline.flush()
                if early_stop is None and plan.get('stop_when'):
                    early_stop = plan['stop_when'](point)
                    if early_stop:
                        early_stop = {**early_stop, 'seconds': point['seconds'], 'sent': load.sent}
                        Save(output/'early-stop.json', early_stop)
                        events.append({'event': 'early_stop', **early_stop})
                        load.stop.set()
                        duration = max(0, point['seconds']-warmup)
                        metrics.end = warmup+duration
                        deadline = time.monotonic()+plan['drain_seconds']
                CheckSamples(cluster, load)
                if recovery_started and recovery_seconds is None:
                    if all(stats) and len({row['head'] for row in stats}) == 1:
                        recovery_observations.append(time.monotonic()-recovery_started)
                        if len(recovery_observations) >= 3:
                            recovery_seconds = recovery_observations[-3]
                    else:
                        recovery_observations.clear()
                if load.error:
                    raise RuntimeError('Load generator: '+load.error)
                if elapsed >= warmup+duration and not thread.is_alive() and common and common['transactions'] == load.sent:
                    break
                time.sleep(plan['poll_seconds'])
        load.stop.set(); thread.join(timeout=10)
        if thread.is_alive():
            raise TimeoutError('Load generator did not stop')
        if fault and applied and not recovered:
            RecoverFault(cluster, fault); recovered = True
        common = cluster.Common()
        CheckSamples(cluster, load, flush=True)
        checks = VerifyNativeState(cluster, dataset, genesis, load.sent, plan['wallets']) if common and common['transactions'] == load.sent else None
        samples = [row for row in load.samples if warmup <= row['scheduled']-load.started < warmup+duration]
        completed = [row for row in samples if row['included_at'] is not None]
        latency = [(row['included_at']-row['scheduled'])*1000 for row in completed]
        sent_latency = [(row['included_at']-row['sent_at'])*1000 for row in completed]
        measured = metrics.measured
        first, last = (measured[0], measured[-1]) if (len(measured) >= 2 and
            measured[-1]['seconds'] > measured[0]['seconds']) else (None, None)
        throughput = (last['common']['transactions']-first['common']['transactions'])/(last['seconds']-first['seconds']) if first else None
        achieved = (last['sent']-first['sent'])/(last['seconds']-first['seconds']) if first else None
        backlogs = [row['backlog'] for row in measured]
        slope = statistics.linear_regression([row['seconds'] for row in measured], backlogs).slope if first else None
        def QueueGrowth(key):
            return statistics.linear_regression([row['seconds'] for row in measured],
                [row[key] for row in measured]).slope if first else None
        tx_backlogs = [row['tx_backlog'] for row in measured]
        tx_slope = statistics.linear_regression([row['seconds'] for row in measured], tx_backlogs).slope if first else None
        logical_backlogs = [row['sent']-row['common']['transactions'] for row in measured]
        logical_slope = statistics.linear_regression([row['seconds'] for row in measured], logical_backlogs).slope if first else None
        reorgs = metrics.reorganizations_sum
        invalid = metrics.invalid_or_rejected_sum
        within = sum(value <= plan['deadline_ms'] for value in latency)
        summary = {'offered_tps': case['rate'], 'achieved_send_tps': achieved, 'common_commit_tps': throughput,
                   'sent_unique': load.sent, 'sent_wire_copies': load.wire_transactions,
                   'p50_ms': Percentile(latency, .5), 'p95_ms': Percentile(latency, .95), 'p99_ms': Percentile(latency, .99),
                   'p99_after_send_ms': Percentile(sent_latency, .99), 'samples': len(samples),
                   'unresolved_samples': len(samples)-len(completed), 'deadline_ms': plan['deadline_ms'],
                   'deadline_success_fraction': within/len(samples) if samples else None,
                   'send_lateness_p99_ms': Percentile(load.late_ms, .99), 'backlog_growth_per_second': slope,
                   'tx_backlog_growth_per_second': tx_slope, 'logical_backlog_growth_per_second': logical_slope,
                   'candidate_backlog_growth_per_second': QueueGrowth('pending_block_verify'),
                   'metadata_queue_growth_per_second': QueueGrowth('pending_metadata'),
                   'logical_backlog_end': logical_backlogs[-1] if first else None,
                   'max_backlog_sum': max(backlogs, default=0), 'reorganizations_sum': reorgs,
                   'measurement_reorganizations_sum': sum(node.reorganizations for node in metrics.nodes),
                   'max_reorg_depth': metrics.max_reorg_depth,
                   'invalid_or_rejected_sum': invalid, 'drained': checks is not None,
                   'recovery_seconds': recovery_seconds, 'checks': checks, 'events': events,
                   'sample_poll_seconds': plan['poll_seconds'], 'measurement_seconds': last['seconds']-first['seconds'] if first else 0,
                   'measurement_samples': len(measured), 'parallel_ingress': load.parallel_ingress}
        if plan.get('stop_when'):
            summary.update(early_stop=early_stop, requested_duration_seconds=requested_duration)
        summary['resources'] = [resource.Summary(node['id']) for resource, node in zip(metrics.nodes, cluster.nodes)]
        cpu_end = controller.cpu_times()
        controller_seconds = time.monotonic()-controller_start
        summary['controller_cpu_core_equivalents'] = ((cpu_end.user+cpu_end.system-cpu_start.user-cpu_start.system)/controller_seconds
                                                    if controller_seconds > 0 else None)
        summary['sustainable'] = bool(not fault and not early_stop and checks and throughput is not None and achieved is not None and
                                      achieved >= .95*case['rate'] and throughput >= .95*case['rate'] and invalid == 0 and
                                      slope <= plan['max_backlog_growth'] and not summary['unresolved_samples'])
        summary['tx_sustainable'] = bool(not fault and not early_stop and checks and throughput is not None and achieved is not None and
                                      achieved >= .95*case['rate'] and throughput >= .95*case['rate'] and invalid == 0 and
                                      tx_slope is not None and tx_slope <= plan['max_backlog_growth'] and not summary['unresolved_samples'])
        Save(output/'latency-samples.json', load.samples)
        Save(output/'events.json', events)
        return summary
    finally:
        load.stop.set(); thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError('Load generator still running; check its sockets')


def MakeCases(plan, suite, count):
    cases = []
    for repeat in range(1, plan['repeats']+1):
        if suite in ['all', 'sync']:
            for enabled in [False, True]:
                cases.append({'suite': 'sync', 'sync': enabled, 'repeat': repeat, 'nodes': count})
        for name in ['latency', 'throughput']:
            if suite in ['all', name]:
                for rate in plan[name+'_rates']:
                    for mode in plan.get(name+'_modes', list(MODES)):
                        for pattern in (plan['patterns'] if name == 'latency' else ['steady']):
                            cases.append({'suite': name, 'mode': mode, 'rate': rate, 'pattern': pattern,
                                          'repeat': repeat, 'nodes': count})
        if suite in ['all', 'scale']:
            for size in plan['node_counts']:
                if size > count:
                    raise ValueError(f'Node count {size} exceeds inventory size {count}')
                for rate in plan['scale_rates']:
                    cases.append({'suite': 'scale', 'mode': 'throughput', 'rate': rate, 'repeat': repeat, 'nodes': size})
        if suite in ['all', 'fault']:
            for fault in plan['faults']:
                cases.append({'suite': 'fault', 'mode': 'latency', 'rate': plan['fault_rate'],
                              'repeat': repeat, 'nodes': count, 'fault': fault})
    if suite == 'throughput' and plan['repeats'] == 1:
        cases.sort(key=lambda case: case['rate'])
    else:
        random.Random(plan['seed']).shuffle(cases)
    return cases


def ValidatePlan(plan):
    if 'nodes' in plan and (not isinstance(plan['nodes'], int) or isinstance(plan['nodes'], bool) or not 2 <= plan['nodes'] <= 512):
        raise ValueError('Plan nodes must be 2..512')
    for name in ['duration', 'sync_duration', 'poll_seconds', 'sample_hz', 'deadline_ms', 'drain_seconds', 'sync_threshold_ms']:
        value = plan.get(name, plan['duration'])
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be positive')
    if not math.isfinite(plan['warmup']) or plan['warmup'] < 0 or not isinstance(plan['repeats'], int) or not 1 <= plan['repeats'] <= 100 or not isinstance(plan['batch'], int) or not 1 <= plan['batch'] <= 10000:
        raise ValueError('Invalid warmup, repeat count or batch size')
    if not isinstance(plan.get('latency_batch', plan['batch']), int) or not 1 <= plan.get('latency_batch', plan['batch']) <= 10000:
        raise ValueError('latency_batch must be 1..10000')
    limits={'candidate-threads':(1,64),'candidate-ms':(0,5000),'block-verify-threads':(1,64),'query-threads':(1,4),'db-profile':(0,1),
            'signature-cache':(16,4194304),'recent-tx-cache':(16,2097152),
            'recent-tx-bytes':(176,512*1024*1024),'sync-window':(4,64)}
    for name,value in plan.get('node_settings',{}).items():
        if name not in limits or not isinstance(value,int) or isinstance(value,bool) or not limits[name][0]<=value<=limits[name][1]:
            raise ValueError(f'Invalid node setting: {name}')
    for mode,settings in plan.get('modes',MODES).items():
        if mode not in MODES or set(settings)!={'block-ms','max-block-txs'} or not 1<=settings['block-ms']<=5000 or not 1<=settings['max-block-txs']<=10000:
            raise ValueError('Invalid block mode settings')
    for name in ['io_threads', 'verify_threads']:
        if not isinstance(plan[name], int) or not 1 <= plan[name] <= 64:
            raise ValueError(f'{name} must be 1..64')
    if not 1 <= plan['wallets'] <= 100000 or plan['ingress'] not in ['broadcast', 'shard']:
        raise ValueError('Invalid wallets or ingress mode')
    if not isinstance(plan.get('parallel_ingress', False), bool):
        raise ValueError('parallel_ingress must be a boolean')
    for name in ['latency_rates', 'throughput_rates', 'scale_rates']:
        if not plan[name] or any(not isinstance(value, int) or value <= 0 for value in plan[name]):
            raise ValueError(f'{name} must contain positive integer rates')
    if not plan['node_counts'] or any(not isinstance(value, int) or not 2 <= value <= 512 for value in plan['node_counts']):
        raise ValueError('Node counts must be 2..512')
    if not plan['offsets_ms'] or not plan['drifts_ppm'] or not plan['patterns'] or any(p not in ['steady', 'burst'] for p in plan['patterns']):
        raise ValueError('Offsets, drifts and traffic patterns must be supplied')
    for name in ['latency_modes', 'throughput_modes']:
        modes = plan.get(name, list(MODES))
        if not isinstance(modes, list) or not modes or any(not isinstance(mode, str) or mode not in MODES for mode in modes) or len(set(modes)) != len(modes):
            raise ValueError('Block modes must be a nonempty list of supported modes')
    for fault in plan['faults']:
        if fault['kind'] not in ['offline', 'partition', 'delay', 'loss'] or fault['start'] < 0 or fault['duration'] <= 0:
            raise ValueError('Invalid fault plan')
        if fault['start']+fault['duration'] >= plan['duration']:
            raise ValueError('Leave a recovery interval inside the measured period')
        if not math.isfinite(fault['start']+fault['duration']):
            raise ValueError('Fault timing must be finite')
        if fault['kind'] != 'offline':
            NetemCommands('eth0', 8089, [{'host': '127.0.0.2', 'port': 8089}], fault, 'validation')
    if not isinstance(plan['fault_rate'], int) or plan['fault_rate'] <= 0:
        raise ValueError('fault_rate must be positive')


def Main(args):
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    ValidatePlan(plan)
    if args.local is not None and not 2 <= args.local <= 512:
        raise ValueError('Local node count must be 2..512')
    if args.local:
        nodes = LocalInventory(args.bin, args.work/'nodes', args.local)
    else:
        _, nodes = ReadInventory(args.inventory)
    if 'nodes' in plan:
        if len(nodes) < plan['nodes']:
            raise ValueError(f"Plan requires {plan['nodes']} nodes; inventory has {len(nodes)}")
        nodes = nodes[:plan['nodes']]
    cases = MakeCases(plan, args.suite, len(nodes))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', args.work.name):
        raise ValueError('Use a work directory name with 1..60 letters, numbers, _ or -')
    network_faults = any(case.get('fault', {}).get('kind') in ['delay', 'loss', 'partition'] for case in cases)
    if network_faults:
        for node in nodes:
            if not node.get('allow_netem') or not node.get('interface'):
                raise ValueError('Network fault cases require allow_netem=true and interface in every inventory node')
            if node.get('transport') == 'local' and platform.system() != 'Linux':
                raise ValueError('Network fault cases require Linux')
    if args.dry_run:
        print(json.dumps({'nodes': nodes, 'cases': cases}, indent=2)); return
    count = max([int(case.get('rate', 0)*(plan['warmup']+plan['duration'])) for case in cases]+[plan['wallets']])
    if count > 100000000:
        raise ValueError('Dataset exceeds 100 million transactions; reduce run duration or offered rate')
    args.work.mkdir(parents=True, exist_ok=False)
    Save(args.work/'plan.json', plan); Save(args.work/'inventory.json', nodes)
    Save(args.work/'cases.json', cases)
    # One signed dataset is shared by all cases; every case starts a new chain database.
    dataset = args.work/'transactions.bin'
    sender = args.bin/ExeName('sender')
    Run([sender, '--prepare', dataset, '--count', count, '--wallets', plan['wallets']], timeout=max(120, count/10000))
    genesis = json.loads(Path(str(dataset)+'.genesis.json').read_text())
    Save(args.work/'environment.json', {'controller': platform.platform(), 'python': platform.python_version(),
                                       'sender_sha256': hashlib.sha256(sender.read_bytes()).hexdigest(),
                                       'dataset_bytes': dataset.stat().st_size,
                                       'timing': 'controller monotonic clock; common canonical prefix; synchronous persistence'})
    results = []
    failed = False
    for index, case in enumerate(cases):
        name = f'{index+1:04}-{case["suite"]}-r{case["repeat"]}'
        run = args.work.name+'-'+name
        output = args.work/name; output.mkdir()
        settings = {'io-threads': plan['io_threads'], 'verify-threads': plan['verify_threads'],
                    'sync': 1, 'produce': 1, 'experiment-index': 1, 'clock-sync': int(plan['clock_sync']),
                    'sync-period-ms': plan['sync_period_ms'], 'sync-gain': plan['sync_gain'],
                    'round-ms': plan['round_ms'], 'relay-transactions': plan['ingress'] == 'shard',
                    'run-seconds': min(86400, int(plan['warmup']+max(plan['duration'], plan.get('sync_duration', 0))+plan['drain_seconds']+180))}
        settings.update({**MODES,**plan.get('modes',{})}[case.get('mode', 'latency')])
        settings.update(plan.get('node_settings',{}))
        if case['suite'] == 'sync':
            settings.update({'clock-sync': int(case['sync']), 'clock-observe': 1,
                             'offsets_ms': plan['offsets_ms'], 'drifts_ppm': plan['drifts_ppm']})
        if case.get('fault', {}).get('kind') == 'partition':
            settings['relay-transactions'] = True
        cluster = Cluster(nodes[:case['nodes']], run, genesis, settings, output)
        result = {'case': case, 'directory': name, 'status': 'failed'}
        print(json.dumps({'starting': name, **case}), flush=True)
        try:
            cluster.Start()
            ready_until = time.monotonic()+30
            while True:
                stats = cluster.Stats()
                if all(row['peers'] >= len(cluster.nodes)-1 for row in stats):
                    break
                if time.monotonic() >= ready_until:
                    raise TimeoutError('Configured peer graph did not connect')
                time.sleep(.1)
            result['metrics'] = ClockCase(cluster, plan, output) if case['suite'] == 'sync' else LoadCase(cluster, case, plan, dataset, genesis, output)
            result['status'] = 'complete'
        except Exception as error:
            result['error'] = str(error); failed = True
        finally:
            cluster.Close()
            if cluster.cleanup_errors:
                result['cleanup_errors'] = cluster.cleanup_errors; result['status'] = 'failed'; failed = True
        Save(output/'result.json', result); results.append(result)
        Save(args.work/'results.json', results)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result['status'] == 'failed' and not args.keep_going:
            break
    rows = [{**row['case'], **row.get('metrics', {}), 'status': row['status']} for row in results]
    fields = ['suite', 'mode', 'nodes', 'rate', 'repeat', 'status', 'common_commit_tps', 'p50_ms', 'p99_ms',
              'deadline_success_fraction', 'sustainable', 'tx_sustainable', 'drained', 'recovery_seconds', 'final_spread_ms',
              'measurement_seconds', 'measurement_samples', 'parallel_ingress', 'tx_backlog_growth_per_second',
              'logical_backlog_growth_per_second', 'logical_backlog_end', 'measurement_reorganizations_sum',
              'reorganizations_sum']
    with (args.work/'summary.csv').open('w', newline='', encoding='utf-8-sig') as output:
        writer = csv.DictWriter(output, fields, extrasaction='ignore'); writer.writeheader(); writer.writerows(rows)
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--inventory', type=Path)
    source.add_argument('--local', type=int, help='Number of local nodes for smoke tests')
    parser.add_argument('--bin', type=Path, required=True, help='Controller-side sender binary directory')
    parser.add_argument('--plan', type=Path, default=Path(__file__).resolve().parents[1]/'Experiments/plan.json')
    parser.add_argument('--work', type=Path, required=True, help='A NEW directory with a simple unique name')
    parser.add_argument('--suite', choices=['all', 'sync', 'latency', 'throughput', 'scale', 'fault'], default='throughput')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--keep-going', action='store_true')
    arguments = parser.parse_args(); arguments.bin = arguments.bin.resolve(); arguments.work = arguments.work.resolve()
    Main(arguments)
