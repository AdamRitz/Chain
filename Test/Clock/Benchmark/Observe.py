"""Bounded common-chain observer reused for the clock-control ablation.

The original dataset generator, workload, node startup and guarded database
cleanup are reused. Only sending cutoffs and measurement are replaced.
No repository file is modified. --dry-run never contacts a server.
"""
import argparse
import concurrent.futures
import hashlib
import json
import mmap
import os
from pathlib import Path
import shlex
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time


def Save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


class Stopped(Exception):
    pass


def Main(args):
    sys.path.insert(0, str(args.source / 'Test'))
    import Experiments as E
    import Cluster as C
    from NetworkTest import Frame, ReadAll, FLAGS

    # The original Worker does not accept a dedicated known-hosts file.
    def PinnedWorker(node, request):
        if node.get('transport') == 'local':
            return original_worker(node, request)
        request = {**request, 'root': node['root'], 'node': node['id']}
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(args.known_hosts),
                   '-p', str(node.get('ssh_port', 22))]
        if node.get('ssh_key'):
            command += ['-i', str(Path(node['ssh_key']).expanduser())]
        command += [node['ssh'], shlex.join([node.get('python', 'python3'), node['worker']])]
        r = subprocess.run(command, input=json.dumps(request), capture_output=True, text=True,
                           encoding='utf-8', timeout=90, creationflags=FLAGS)
        try:
            value = json.loads(r.stdout)
        except ValueError as error:
            raise RuntimeError(node['id'] + ' worker: ' + (r.stderr or r.stdout)) from error
        if r.returncode or not value.get('ok'):
            raise RuntimeError(node['id'] + ': ' + value.get('error', r.stderr))
        return value['result']

    original_worker = C.Worker
    C.Worker = PinnedWorker

    class BoundedClient(C.NodeClient):
        def Query(self, kind, payload=b'', raw=False):
            with self.lock:
                if getattr(self, 'cancel', None) is not None and self.cancel.is_set():
                    raise Stopped('Measurement cutoff reached')
                try:
                    if self.socket is None:
                        self.socket = socket.create_connection((self.node['host'], self.node['port']), timeout=1)
                        self.socket.settimeout(1)
                        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        self.socket.sendall(b'\x02')
                    self.socket.sendall(Frame(kind, payload))
                    response, size = struct.unpack('<BI', ReadAll(self.socket, 5))
                    if response != kind + 1 or size > 16384:
                        raise ValueError('Invalid measurement response')
                    data = ReadAll(self.socket, size)
                    return data if raw else json.loads(data)
                except BaseException:
                    self.Close()
                    raise

    class CutoffLoad(E.Load):
        """Original broadcast/batch workload, with observable socket cutoffs."""
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.sockets = {}
            self.socket_lock = threading.Lock()
            self.attempted = 0
            self.completed_per_ingress = [0] * len(self.cluster.nodes)
            self.cutoff_at = None
            self.partial_batch = None
            self.batch_intervals = []

        def HardStop(self):
            self.stop.set()
            with self.socket_lock:
                if self.cutoff_at is None:
                    self.cutoff_at = time.monotonic()
                for connection in self.sockets.values():
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    connection.close()
                self.sockets.clear()

        def Run(self):
            try:
                with self.dataset.open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
                    if len(data) < self.count * 176:
                        raise ValueError('Dataset smaller than offered load')
                    self.started = time.monotonic()
                    for begin in range(0, self.count, self.batch):
                        end = min(begin + self.batch, self.count)
                        scheduled = self.started + E.ArrivalTime(end - 1, self.rate, self.pattern)
                        if self.stop.wait(max(0, scheduled - time.monotonic())):
                            break
                        batch_start = time.monotonic()
                        completed = []
                        self.attempted = end
                        package = data[begin * 176:end * 176]
                        # This adapter deliberately keeps the original sequential,
                        # broadcast-to-all-ten ingress and identical frame bytes.
                        for index, node in enumerate(self.cluster.nodes):
                            with self.socket_lock:
                                if self.stop.is_set():
                                    raise Stopped()
                                connection = self.sockets.get(index)
                            if connection is None:
                                connection = socket.create_connection((node['host'], node['port']), timeout=1)
                                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                                connection.settimeout(1)
                                with self.socket_lock:
                                    if self.stop.is_set():
                                        connection.close()
                                        raise Stopped()
                                    self.sockets[index] = connection
                                connection.sendall(b'\x02')
                            connection.sendall(Frame(10, package))
                            sent_end = time.monotonic()
                            completed.append(index)
                            self.completed_per_ingress[index] = end
                            self.wire_transactions += end - begin
                        with self.lock:
                            self.sent = end
                            self.late_ms.append(max(0, sent_end - scheduled) * 1000)
                            self.batch_intervals.append({'begin': begin, 'end': end,
                                'start_seconds': batch_start - self.started, 'end_seconds': sent_end - self.started})
                            for index in range(begin, end):
                                if index % self.every == 0:
                                    self.samples.append({'index': index,
                                        'hash': data[index * 176 + 80:index * 176 + 112].hex(),
                                        'scheduled': self.started + E.ArrivalTime(index, self.rate, self.pattern),
                                        'send_begin': batch_start, 'sent_at': sent_end,
                                        'included_at': None, 'last_negative_at': None})
            except (Stopped, OSError) as error:
                self.partial_batch = {'begin': locals().get('begin'), 'end': locals().get('end'),
                    'completed_ingress_indices': locals().get('completed', []),
                    'cutoff_expected': self.stop.is_set(), 'reason': type(error).__name__ + ': ' + str(error)}
                if not self.stop.is_set():
                    self.error = str(error)
            except Exception as error:
                self.error = str(error)
            finally:
                self.finished = time.monotonic()
                with self.socket_lock:
                    for connection in self.sockets.values():
                        connection.close()
                    self.sockets.clear()

    class Observe:
        def __init__(self, cluster, load, output):
            self.cluster, self.load, self.output = cluster, load, output
            self.stats_clients = [BoundedClient(n) for n in cluster.nodes]
            self.prefix_clients = [BoundedClient(n) for n in cluster.nodes]
            self.latency_clients = [BoundedClient(n) for n in cluster.nodes]
            self.stats_pool = concurrent.futures.ThreadPoolExecutor(10)
            self.latency_pool = concurrent.futures.ThreadPoolExecutor(10)
            self.stop = threading.Event()
            self.errors = []
            self.points = []
            self.skipped_targets = []
            self.latency_rpc = []
            self.thread = threading.Thread(target=self.Latency, daemon=True)

        def Rpc(self, clients, index, kind, payload=b''):
            before = time.monotonic()
            sent_before = self.load.sent
            try:
                value = clients[index].Query(kind, payload)
                error = None
            except Exception as exc:
                value, error = None, str(exc)
            after = time.monotonic()
            row = {'node': self.cluster.nodes[index]['id'], 'kind': kind,
                'start_seconds': before - self.load.started, 'end_seconds': after - self.load.started,
                'seconds': (before + after) / 2 - self.load.started,
                'rtt_ms': (after - before) * 1000,
                'sent_before': sent_before, 'sent_after': self.load.sent, 'value': value}
            if error:
                row['error'] = error
            return row

        def Parallel(self, pool, clients, kind, payload=b''):
            return list(pool.map(lambda i: self.Rpc(clients, i, kind, payload), range(10)))

        def Point(self, target):
            rows = self.Parallel(self.stats_pool, self.stats_clients, 13)
            point = {'target_seconds': target, 'stats_rpc': rows, 'nodes': [r['value'] for r in rows]}
            stats = point['nodes']
            if all(stats):
                if any(r['failed'] for r in stats):
                    raise RuntimeError('Node execution failure')
                upper = min(r['height'] for r in stats)
                limit = min(range(10), key=lambda i: stats[i]['height'])
                anchor = rows[limit]
                point.update({'limiting_node': limit, 'snapshot_seconds': anchor['seconds'],
                    'snapshot_start_seconds': anchor['start_seconds'],
                    'snapshot_end_seconds': anchor['end_seconds'], 'min_height': upper,
                    'min_chain_transactions': min(r['chain_transactions'] for r in stats),
                    'sent': (anchor['sent_before'] + anchor['sent_after']) / 2})
                # Intermediate binary searches can occupy the observer beyond
                # the 12s endpoint under congestion. Keep one-second stats, but
                # reserve full common-prefix probes for endpoints and drain.
                if upper and target in (2,12,16):
                    probes = []
                    def At(height):
                        round_rows = self.Parallel(self.stats_pool, self.prefix_clients, 29, struct.pack('<Q', height))
                        for r in round_rows:
                            r['requested_height'] = height
                        probes.extend(round_rows)
                        values = [r['value'] for r in round_rows]
                        return values[0] if values[0] and all(v == values[0] for v in values) else None
                    common = At(upper)
                    if not common:
                        lower, best = 1, At(1)
                        high = upper
                        while lower < high and time.monotonic() < self.load.started + target + .4:
                            middle = (lower + high + 1) // 2
                            value = At(middle)
                            if value:
                                lower, best = middle, value
                            else:
                                high = middle - 1
                        common = best if lower == high else None
                    point['prefix_rpc'] = probes
                    point['common'] = common
                    point['common_observation_interval_seconds'] = [
                        min(r['start_seconds'] for r in rows), max(r['end_seconds'] for r in probes)]
                    point['limiting_head_matches_prefix'] = bool(point['common'] and
                        point['common']['hash'] == stats[limit]['head'] and
                        point['common']['transactions'] == stats[limit]['chain_transactions'])
            self.points.append(point)
            with (self.output / 'timeline.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(point) + '\n')
            return point

        def Latency(self):
            while not self.stop.is_set():
                with self.load.lock:
                    pending = [s for s in self.load.samples if s['included_at'] is None][:8]
                if pending:
                    payload = b''.join(bytes.fromhex(s['hash']) for s in pending)
                    rows = self.Parallel(self.latency_pool, self.latency_clients, 31, payload)
                    self.latency_rpc.append({'sample_indices': [s['index'] for s in pending], 'rpc': rows})
                    if all(r['value'] is not None and len(r['value']) == len(pending) for r in rows):
                        observed = self.load.started + max(r['end_seconds'] for r in rows)
                        negative = self.load.started + min(r['start_seconds'] for r in rows)
                        with self.load.lock:
                            for column, sample in enumerate(pending):
                                values = [r['value'][column] for r in rows]
                                if values[0] is not None and all(v == values[0] for v in values):
                                    sample['included_at'] = observed
                                    sample['observed_position'] = values[0]
                                    sample['positive_rpc_start'] = self.load.started + min(r['start_seconds'] for r in rows)
                                    sample['positive_rpc_end'] = observed
                                else:
                                    # This is an observation bracket, not a claim
                                    # that tx inclusion was globally atomic.
                                    sample['last_negative_at'] = negative
                self.stop.wait(.05)

        def Close(self):
            self.stop.set()
            for clients in (self.stats_clients, self.prefix_clients, self.latency_clients):
                for client in clients:
                    client.Close()
            if self.thread.ident is not None:
                self.thread.join(timeout=3)
            self.stats_pool.shutdown(wait=True, cancel_futures=True)
            self.latency_pool.shutdown(wait=True, cancel_futures=True)
            if self.thread.is_alive():
                raise RuntimeError('Latency observer did not stop')

    def LoadCase(cluster, case, plan, dataset, genesis, output):
        actual = {m.get('binary_sha256') for m in cluster.metadata.values()}
        if actual != {args.expected_binary_sha256}:
            raise RuntimeError('Unexpected experiment binary SHA256: ' + str(actual))
        Save(output / 'measurement-adapter.json', {
            'adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'application_source_commit': args.source_revision,
            'expected_binary_sha256': args.expected_binary_sha256,
            'workload': 'native transfer, 1024 accounts, batch128, same signed dataset, sequential broadcast10',
            'stats_cadence_seconds': 1, 'latency_samples_per_second_total': 2,
            'application_settings_changed': True, 'group': case.get('group'), 'clock_control': case.get('clock_control'), 'join_current': case.get('join_current'),
            'send_cutoff_seconds': 12, 'node_stop_request_deadline_seconds': 17})
        abort = threading.Event()
        for client in cluster.clients:
            client.Close()
        cluster.clients = [BoundedClient(n) for n in cluster.nodes]
        for client in cluster.clients:
            client.cancel = abort
        load = CutoffLoad(cluster, dataset, case['rate'], 12, 'steady', 'broadcast', 2, 128, False)
        observer = Observe(cluster, load, output)
        for clients in (observer.stats_clients, observer.prefix_clients, observer.latency_clients):
            for client in clients:
                client.cancel = abort
        thread = threading.Thread(target=load.Run, daemon=True)
        guard_done = threading.Event()
        stop_lock = threading.Lock()
        stop_errors, stop_records = [], []
        nodes_stopped = False
        case_start = time.monotonic()

        def StopNodes():
            nonlocal nodes_stopped
            with stop_lock:
                if nodes_stopped:
                    return
                nodes_stopped = True
                requested = time.monotonic() - (load.started or case_start)
                # Independent workers; never signal a service or any other run.
                with concurrent.futures.ThreadPoolExecutor(10) as pool:
                    futures = [pool.submit(cluster.StopOne, i) for i in range(10)]
                    for i, future in enumerate(futures):
                        try:
                            result = future.result()
                            stop_records.append({'node': cluster.nodes[i]['id'],
                                'requested_seconds': requested, 'result': result})
                        except Exception as exc:
                            stop_errors.append(cluster.nodes[i]['id'] + ': ' + str(exc))

        def Guard():
            if not guard_done.wait(max(0, load.started + 12 - time.monotonic())):
                load.HardStop()
            if not guard_done.wait(max(0, load.started + 17 - time.monotonic())):
                abort.set()
                observer.stop.set()
                for clients in (observer.stats_clients, observer.prefix_clients, observer.latency_clients, cluster.clients):
                    for client in clients:
                        client.Close()
                StopNodes()

        guard = None
        summary = {}
        try:
            thread.start()
            while not load.started and thread.is_alive():
                time.sleep(.001)
            if not load.started:
                raise RuntimeError('Load generator never started')
            guard = threading.Thread(target=Guard, daemon=True)
            guard.start()
            observer.thread.start()
            final_point = None
            for target in range(18):
                if time.monotonic() >= load.started + 17:
                    break
                time.sleep(max(0, load.started + target - time.monotonic()))
                if time.monotonic() >= load.started + 17:
                    break
                if time.monotonic() > load.started + target + .5:
                    observer.skipped_targets.append(target)
                    continue
                point = observer.Point(target)
                final_point = point
                if target >= 12 and not thread.is_alive() and point.get('common') and \
                        point['common']['transactions'] in {load.sent, load.attempted}:
                    break
            load.HardStop()
            thread.join(timeout=max(0, load.started + 17 - time.monotonic()))
            if thread.is_alive():
                raise RuntimeError('Load generator still alive at hard cutoff')
            observer.stop.set()
            observer.thread.join(timeout=1.1)
            checks, final_common = None, final_point.get('common') if final_point else None
            accepted_counts = {load.sent, load.attempted}
            enough_time = time.monotonic() < load.started + 16
            if enough_time and final_common and final_common['transactions'] in accepted_counts:
                # State verification and canonical anchor checks happen only after
                # every sender has stopped, and are bounded by the 17s guard.
                try:
                    checks = E.VerifyNativeState(cluster, dataset, genesis, final_common['transactions'], 1024)
                except Exception as exc:
                    summary['verification_error'] = str(exc)

            boundaries = [p for p in observer.points if 2 <= p['target_seconds'] <= 12 and all(p.get('nodes', []))]
            boundary_pair = [boundaries[0], boundaries[-1]] if len(boundaries) >= 2 else []
            proof = {'method': 'captured stats heads checked against one final common canonical chain',
                     'passed': False, 'boundary_targets': [p['target_seconds'] for p in boundary_pair], 'checks': []}
            cache = {}
            if final_common and len(boundary_pair) == 2 and time.monotonic() < load.started + 16:
                try:
                    for p in boundary_pair:
                        for row in p['stats_rpc']:
                            stat = row['value']; height = stat['height']
                            if height > final_common['height']:
                                raise RuntimeError('Boundary exceeds the final shared canonical anchor')
                            if height not in cache:
                                cache[height] = observer.prefix_clients[0].Query(29, struct.pack('<Q', height))
                            value = cache[height]
                            match = bool(value and value['hash'] == stat['head'] and
                                         value['transactions'] == stat['chain_transactions'])
                            proof['checks'].append({'target': p['target_seconds'], 'node': row['node'],
                                                    'height': height, 'matches_final_chain': match})
                    proof['passed'] = len(proof['checks']) == 20 and all(c['matches_final_chain'] for c in proof['checks'])
                except Exception as exc:
                    proof['error'] = str(exc)
            Save(output / 'boundary-canonical-proof.json', proof)

            def EndpointRate(points, count_key):
                if len(points) != 2:
                    return None, None
                a, b = points
                dt = b['snapshot_seconds'] - a['snapshot_seconds']
                count = b[count_key] - a[count_key]
                low_dt = b['snapshot_start_seconds'] - a['snapshot_end_seconds']
                high_dt = b['snapshot_end_seconds'] - a['snapshot_start_seconds']
                return (count / dt if dt > 0 else None,
                        [count / high_dt, count / low_dt] if count >= 0 and low_dt > 0 else None)

            throughput, bounds = EndpointRate(boundary_pair, 'min_chain_transactions') if proof['passed'] else (None, None)
            observed_pairs = [p for p in boundaries if p.get('common') and p.get('limiting_head_matches_prefix')]
            diagnostic = [observed_pairs[0], observed_pairs[-1]] if len(observed_pairs) >= 2 else []
            for p in diagnostic:
                p['observed_common_transactions'] = p['common']['transactions']
            diagnostic_tps, diagnostic_bounds = EndpointRate(diagnostic, 'observed_common_transactions')
            common_points = [p for p in boundaries if p.get('common')]
            common_pair = [common_points[0], common_points[-1]] if len(common_points) >= 2 else []
            common_observed_tps, common_observed_bounds = None, None
            if common_pair:
                pa, pb = common_pair
                a0, a1 = pa['common_observation_interval_seconds']
                b0, b1 = pb['common_observation_interval_seconds']
                count = pb['common']['transactions'] - pa['common']['transactions']
                elapsed = (b0 + b1 - a0 - a1) / 2
                common_observed_tps = count / elapsed if elapsed > 0 else None
                if b0 > a1 and count >= 0:
                    common_observed_bounds = [count / (b1 - a0), count / (b0 - a1)]
            # Prove the actual common-prefix anchors, independently of transient
            # per-node heads. Reject missing/reorganized anchors; never fill them.
            common_proof = {'passed': False, 'checks': [], 'boundary_targets': [p['target_seconds'] for p in common_pair]}
            if common_pair and [p['target_seconds'] for p in common_pair] == [2, 12] and final_common and time.monotonic() < load.started + 16.5:
                try:
                    for point in common_pair:
                        anchor = point['common']
                        values = observer.Parallel(observer.stats_pool, observer.prefix_clients, 29, struct.pack('<Q', anchor['height']))
                        match = anchor['height'] <= final_common['height'] and all(row['value'] == anchor for row in values)
                        common_proof['checks'].append({'target': point['target_seconds'], 'anchor': anchor, 'matches_final_chain': match, 'rpc': values})
                    common_proof['passed'] = len(common_proof['checks']) == 2 and all(row['matches_final_chain'] for row in common_proof['checks'])
                except Exception as exc:
                    common_proof['error'] = str(exc)
            Save(output / 'common-boundary-proof.json', common_proof)
            with load.lock:
                samples = [dict(s) for s in load.samples if 2 <= s['scheduled'] - load.started < 12]
            completed = [s for s in samples if s['included_at'] is not None]
            scheduled_ms = [(s['included_at'] - s['scheduled']) * 1000 for s in completed]
            after_send_ms = [max(0, s['included_at'] - s['sent_at']) * 1000 for s in completed]
            resources = []
            if len(boundary_pair) == 2:
                for i, node in enumerate(cluster.nodes):
                    a, b = [p['nodes'][i] for p in boundary_pair]
                    dt = (b['clock']['raw_us'] - a['clock']['raw_us']) / 1e6
                    def Rate(key):
                        return (b.get(key, 0) - a.get(key, 0)) / dt if dt > 0 else None
                    resources.append({'node': node['id'], 'node_clock_seconds': dt,
                        'cpu_core_equivalents': Rate('process_cpu_seconds'),
                        'network_received_bytes_per_second': Rate('network_bytes'),
                        'network_sent_bytes_per_second': Rate('network_sent_bytes'),
                        'db_write_ms_per_second': Rate('db_write_ms'),
                        'peak_resident_bytes': max(p['nodes'][i].get('resident_bytes', 0)
                            for p in observer.points if p.get('nodes') and p['nodes'][i])})
            def SentAt(seconds):
                return max((b['end'] for b in load.batch_intervals if b['end_seconds'] <= seconds), default=0)
            summary.update({'offered_tps': case['rate'], 'common_commit_tps': throughput,
                'achieved_send_tps': (SentAt(12) - SentAt(2)) / 10,
                'common_commit_tps_rpc_interval_bounds': bounds,
                'common_observation_tps': common_observed_tps,
                'proven_common_tps': common_observed_tps if common_proof['passed'] else None,
                'common_boundary_proof_passed': common_proof['passed'],
                'common_observation_tps_acquisition_interval_bounds': common_observed_bounds,
                'common_observation_note': 'net common-prefix observation rate; interval spans stats acquisition through prefix checks, no atomic global snapshot or finality claimed',
                'common_observation_boundary_targets': [p['target_seconds'] for p in common_pair],
                'measurement_seconds': boundary_pair[-1]['snapshot_seconds'] - boundary_pair[0]['snapshot_seconds'] if boundary_pair else None,
                'measurement_samples': len(boundaries), 'boundary_canonical_proof_passed': proof['passed'],
                'skipped_stats_targets': observer.skipped_targets,
                'diagnostic_prefix_observed_tps': diagnostic_tps,
                'diagnostic_prefix_rpc_interval_bounds': diagnostic_bounds,
                'diagnostic_prefix_note': 'snapshot time corrected, but boundaries may be reorganized; not used as proven TPS',
                'sent_unique_completed_all_ingress': load.sent, 'sent_unique_attempted_upper_bound': load.attempted,
                'sent_unique': load.sent, 'sent_wire_copies_completed': load.wire_transactions,
                'completed_per_ingress': load.completed_per_ingress, 'partial_batch': load.partial_batch,
                'load_error': load.error, 'cutoff_seconds': load.cutoff_at - load.started if load.cutoff_at else None,
                'generator_finished_seconds': load.finished - load.started,
                'p50_ms': E.Percentile(scheduled_ms, .5), 'p95_ms': E.Percentile(scheduled_ms, .95),
                'p99_ms': E.Percentile(scheduled_ms, .99), 'p99_after_send_ms': E.Percentile(after_send_ms, .99),
                'latency_label': 'first observed identical transaction position on all ten nodes; no finality guarantee',
                'latency_poll_seconds': .05, 'latency_sample_hz_total': 2,
                'latency_note': 'upper observation latency includes query/round-trip/polling delay; not node commit timestamp',
                'samples': len(samples), 'unresolved_samples': len(samples) - len(completed),
                'drained': bool(checks), 'drained_observed_seconds': max((r['end_seconds'] for r in final_point.get('prefix_rpc', [])), default=None) if checks and final_point else None,
                'checks': checks, 'resources': resources,
                'timing': 'RPC intervals bound each snapshot; TPS uses limiting stats snapshot, never later prefix query completion',
                'measurement_quality': 'canonical boundary proof passed' if proof['passed'] else 'TPS unresolved: canonical boundary proof unavailable/failed'})
            return summary
        finally:
            load.HardStop()
            if thread.ident is not None:
                thread.join(timeout=3)
            abort.set()
            guard_done.set()
            try:
                StopNodes()
            finally:
                observer.Close()
                if guard:
                    guard.join(timeout=95)
            Save(output / 'sender-batches.json', load.batch_intervals)
            Save(output / 'sender-state.json', {
                'completed_all_ingress': load.sent, 'attempted_upper_bound': load.attempted,
                'completed_per_ingress': load.completed_per_ingress,
                'wire_transactions_completed': load.wire_transactions,
                'partial_batch': load.partial_batch, 'error': load.error,
                'started_monotonic': load.started, 'finished_monotonic': getattr(load, 'finished', None),
                'cutoff_monotonic': load.cutoff_at})
            Save(output / 'latency-samples.json', load.samples)
            Save(output / 'latency-rpc.json', observer.latency_rpc)
            Save(output / 'stop-records.json', {'nodes': stop_records, 'errors': stop_errors})
            if thread.is_alive() or (guard and guard.is_alive()):
                raise RuntimeError('Sender/guard did not stop; do not clean databases')
            if stop_errors:
                raise RuntimeError('Experiment stop failed: ' + '; '.join(stop_errors))

    E.LoadCase = LoadCase
    args.suite, args.local, args.keep_going = 'throughput', None, True
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    expected = {'nodes': 10, 'repeats': 1, 'warmup': 2, 'duration': 10,
                'drain_seconds': 5, 'wallets': 1024, 'batch': 128, 'ingress': 'broadcast'}
    for key, value in expected.items():
        if plan.get(key) != value:
            raise ValueError(f'Fixed comparison requires {key}={value!r}')
    if plan['throughput_rates'] != [20000, 30000, 40000, 50000, 60000] or plan.get('parallel_ingress', False):
        raise ValueError('Fixed rates and sequential original ingress required')
    if plan['throughput_modes'] != ['throughput']:
        raise ValueError('Only throughput mode is authorized')
    _, nodes = C.ReadInventory(args.inventory)
    if len(nodes) != 10 or any(n['port'] != 8090 or n.get('root') != '/var/tmp/chain-experiments' for n in nodes):
        raise ValueError('Use exactly ten dedicated8090 experiment nodes rooted at /var/tmp/chain-experiments')
    if args.dry_run:
        E.Main(args)
        return
    args.source = args.source.resolve()
    E.Main(args)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, default=Path('/opt/chain/src'))
    p.add_argument('--bin', type=Path, default=Path('/opt/chain/build'))
    p.add_argument('--inventory', type=Path, required=True)
    p.add_argument('--known-hosts', type=Path, default=Path('/opt/chain-control/known_hosts'))
    p.add_argument('--expected-binary-sha256', default='cacece0b3806b334d8966aedbae5182a63654eb726189688eafe362558c06ebb')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    a.source, a.bin, a.work = a.source.resolve(), a.bin.resolve(), a.work.resolve()
    Main(a)
