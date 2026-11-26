"""Compare five local producers with one-block or consecutive-block turns.

Both runs reuse --dataset and use fresh work directories. Examples:
  python Test/VdfContinuousBenchmark.py --bin build --work run-old --dataset tx.bin --consecutive-blocks 0
  python Test/VdfContinuousBenchmark.py --bin build --work run-new --dataset tx.bin --consecutive-blocks 32

The dataset is prepared once when absent. Logs, configuration and measurements
are retained; Cluster validates stopped process identities before deleting DBs.
"""
import argparse
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import struct
import subprocess
import threading
import time
import traceback

import psutil

import Cluster as Lifecycle
import Experiments
from NetworkTest import ExeName, Run
from VdfContinuousNetworkTest import Audit as AuditContinuous, Block as ContinuousBlock
from VdfNetworkTest import Block as RotationBlock, Candidate, Query, Require
from VdfRotationNetworkTest import Owner


def Save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def Digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


class EarlyStop:
    def __init__(self, target, window, seconds, ratio):
        self.target, self.window, self.seconds, self.ratio = target, window, seconds, ratio
        self.points, self.bad_since = deque(), None

    def Add(self, seconds, transactions):
        self.points.append((seconds, transactions))
        while len(self.points) > 1 and self.points[1][0] <= seconds - self.window:
            self.points.popleft()
        before, count = self.points[0]
        if seconds - before < self.window:
            return None
        throughput = (transactions - count) / (seconds - before)
        if throughput >= self.target * self.ratio:
            self.bad_since = None
            return None
        if self.bad_since is None:
            self.bad_since = seconds
        if seconds - self.bad_since < self.seconds:
            return None
        return {'reason': 'TPS remained below the configured fraction of offered TPS',
                'window_seconds': seconds - before, 'window_tps': throughput,
                'threshold_tps': self.target * self.ratio,
                'below_threshold_seconds': seconds - self.bad_since}


def LoadCase(cluster, plan, dataset, genesis, output, args):
    """Use the experiment sender, sampling and state checks with early stopping."""
    warmup, duration = plan['warmup'], plan['duration']
    load = Experiments.Load(cluster, dataset, args.rate, warmup + duration, 'steady',
                            plan['ingress'], plan['sample_hz'], plan['batch'], False)
    thread = threading.Thread(target=load.Run, daemon=True)
    metrics = Experiments.LoadMetrics(warmup, duration, cluster.nodes)
    guard = EarlyStop(args.rate, args.early_stop_window, args.early_stop_seconds, args.early_stop_ratio)
    stop = None
    controller, started = psutil.Process(), time.monotonic()
    cpu_start = controller.cpu_times()
    thread.start()
    while not load.started and thread.is_alive():
        time.sleep(.001)
    try:
        end = load.started + warmup + duration
        deadline = end + plan['drain_seconds']
        with (output / 'timeline.jsonl').open('w', encoding='utf-8') as timeline:
            while time.monotonic() < deadline:
                stats = cluster.Stats()
                Require(not any(row['failed'] for row in stats), 'Node execution failed')
                common = cluster.Common(stats)
                elapsed = time.monotonic() - load.started
                point = {'seconds': elapsed, 'sent': load.sent, 'common': common, 'nodes': stats}
                metrics.Add(point)
                timeline.write(json.dumps(point) + '\n')
                timeline.flush()
                Experiments.CheckSamples(cluster, load)
                if load.error:
                    raise RuntimeError('Load generator: ' + load.error)
                if stop is None and warmup <= elapsed < warmup + duration and common:
                    stop = guard.Add(elapsed - warmup, common['transactions'])
                    if stop:
                        stop.update(measurement_seconds=elapsed - warmup, sent=load.sent)
                        Save(output / 'early-stop.json', stop)
                        print(json.dumps({'event': 'early_stop', **stop}), flush=True)
                        load.stop.set()
                        # Keep the partial measurement and give already-sent transactions time to finish.
                        metrics.end = elapsed
                        end, deadline = time.monotonic(), time.monotonic() + plan['drain_seconds']
                if time.monotonic() >= end and not thread.is_alive() and common and common['transactions'] == load.sent:
                    break
                time.sleep(plan['poll_seconds'])
        load.stop.set()
        thread.join(timeout=10)
        Require(not thread.is_alive(), 'Load generator did not stop')
        common = cluster.Common()
        Experiments.CheckSamples(cluster, load, flush=True)
        checks = Experiments.VerifyNativeState(cluster, dataset, genesis, load.sent, plan['wallets']) if (
            common and common['transactions'] == load.sent) else None
        measured = metrics.measured
        first, last = (measured[0], measured[-1]) if len(measured) >= 2 else (None, None)
        measured_seconds = last['seconds'] - first['seconds'] if first else 0
        samples = [row for row in load.samples if warmup <= row['scheduled'] - load.started < metrics.end]
        completed = [row for row in samples if row['included_at'] is not None]
        latency = [(row['included_at'] - row['scheduled']) * 1000 for row in completed]
        sent_latency = [(row['included_at'] - row['sent_at']) * 1000 for row in completed]
        throughput = (last['common']['transactions'] - first['common']['transactions']) / measured_seconds if first else None
        achieved = (last['sent'] - first['sent']) / measured_seconds if first else None
        def QueueGrowth(key):
            return statistics.linear_regression([row['seconds'] for row in measured],
                [row[key] for row in measured]).slope if first else None
        backlog_growth = QueueGrowth('backlog')
        tx_backlog_growth = QueueGrowth('tx_backlog')
        logical_growth = statistics.linear_regression([row['seconds'] for row in measured],
            [row['sent'] - row['common']['transactions'] for row in measured]).slope if first else None
        cpu_end = controller.cpu_times()
        summary = {'offered_tps': args.rate, 'achieved_send_tps': achieved, 'common_commit_tps': throughput,
                   'sent_unique': load.sent, 'sent_wire_copies': load.wire_transactions,
                   'p50_ms': Experiments.Percentile(latency, .5), 'p95_ms': Experiments.Percentile(latency, .95),
                   'p99_ms': Experiments.Percentile(latency, .99),
                   'p99_after_send_ms': Experiments.Percentile(sent_latency, .99),
                   'samples': len(samples), 'unresolved_samples': len(samples) - len(completed),
                   'deadline_ms': plan['deadline_ms'],
                   'deadline_success_fraction': sum(value <= plan['deadline_ms'] for value in latency) / len(samples) if samples else None,
                   'send_lateness_p99_ms': Experiments.Percentile(load.late_ms, .99),
                   'backlog_growth_per_second': backlog_growth,
                   'tx_backlog_growth_per_second': tx_backlog_growth,
                   'candidate_backlog_growth_per_second': QueueGrowth('pending_block_verify'),
                   'metadata_queue_growth_per_second': QueueGrowth('pending_metadata'),
                   'logical_backlog_growth_per_second': logical_growth,
                   'logical_backlog_end': last['sent'] - last['common']['transactions'] if first else None,
                   'max_backlog_sum': max((row['backlog'] for row in measured), default=0),
                   'reorganizations_sum': metrics.reorganizations_sum,
                   'measurement_reorganizations_sum': sum(row.reorganizations for row in metrics.nodes),
                   'max_reorg_depth': metrics.max_reorg_depth,
                   'invalid_or_rejected_sum': metrics.invalid_or_rejected_sum,
                   'measurement_seconds': measured_seconds, 'measurement_samples': len(measured),
                   'drained': checks is not None, 'checks': checks, 'early_stop': stop,
                   'sample_poll_seconds': plan['poll_seconds'], 'parallel_ingress': False,
                   'controller_cpu_core_equivalents': (cpu_end.user + cpu_end.system - cpu_start.user - cpu_start.system) / (time.monotonic() - started),
                   'resources': [row.Summary(node['id']) for row, node in zip(metrics.nodes, cluster.nodes)]}
        summary['sustainable'] = bool(not stop and checks and throughput is not None and achieved is not None and
            achieved >= .95 * args.rate and throughput >= .95 * args.rate and
            not summary['unresolved_samples'] and not summary['invalid_or_rejected_sum'] and
            backlog_growth is not None and backlog_growth <= plan['max_backlog_growth'])
        summary['tx_sustainable'] = bool(not stop and checks and throughput is not None and achieved is not None and
            achieved >= .95 * args.rate and throughput >= .95 * args.rate and
            not summary['unresolved_samples'] and not summary['invalid_or_rejected_sum'] and
            tx_backlog_growth is not None and tx_backlog_growth <= plan['max_backlog_growth'])
        Experiments.Save(output / 'latency-samples.json', load.samples)
        Save(output / 'summary.json', summary)
        return summary
    finally:
        load.stop.set()
        thread.join(timeout=10)
        Require(not thread.is_alive(), 'Load generator still running')


def AuditChain(cluster, genesis, output, expected_count, require_all_producers=True):
    config = genesis['vdf']
    continuous = bool(config.get('consecutive_blocks'))
    decode = ContinuousBlock if continuous else RotationBlock
    anchor = cluster.Common()
    Require(anchor and anchor['transactions'] == expected_count, 'Final common block contains every sent transaction')
    blocks, producers, normal = [], {}, 0
    with (output / 'canonical-blocks.jsonl').open('w', encoding='utf-8') as descriptions:
        for height in range(1, anchor['height'] + 1):
            data = Query(cluster.nodes[0]['port'], 6, struct.pack('<Q', height), raw=True)
            block = decode(data)
            Require(block['height'] == height and (not blocks or block['parent'] == blocks[-1]['hash']),
                    'Canonical block height and parent continuity')
            entry = {key: value for key, value in block.items()
                     if key not in {'raw', 'transactions', 'root', 'candidates', 'proof'}}
            entry.update(transactions=len(block['transactions']), bytes=len(data),
                         has_producer_proof=block.get('proof') is not None)
            if height >= 2:
                epoch = (height - 2) // config['epoch_blocks']
                entry['epoch'] = epoch
                if epoch >= 2:
                    active = [Candidate(record)['public_key'] for record in blocks[epoch * config['epoch_blocks']]['candidates']]
                    Require(block['producer'] in active, 'Healthy five-node test has no outsider recovery block')
                    if not continuous:
                        Require(block['producer'] == Owner(active, block['slot'], config), 'Scheduled one-block producer')
                    normal += 1
                    producers[block['producer']] = producers.get(block['producer'], 0) + 1
            descriptions.write(json.dumps(entry) + '\n')
            # Keep headers and small VDF records for the independent schedule audit.
            # Transaction bytes have been hashed and verified by the decoder.
            block.pop('raw')
            block['transactions'] = ()
            blocks.append(block)
    Require(blocks[-1]['hash'] == anchor['hash'], 'Audited chain ends at the fixed common block')
    anchors = cluster.All(lambda index: cluster.clients[index].Query(29, struct.pack('<Q', anchor['height'])))
    Require(all(value == anchor for value in anchors), 'Common block stays unchanged during the audit')
    Require(normal >= 5 and (not require_all_producers or len(producers) == 5),
            'Canonical production covers the required producer turns')
    result = {'normal_blocks_checked': normal, 'blocks_by_producer': producers, 'anchor': anchor}
    if continuous:
        result['continuous_turns'] = AuditContinuous(blocks, config)
    return result


def Main(args):
    binary, work, dataset = args.bin.resolve(strict=True), args.work.resolve(), args.dataset.resolve()
    Require(not work.exists(), 'Choose a fresh work directory')
    Require(math.isfinite(args.duration) and 1 <= args.duration <= 3600, 'Duration must be 1..3600 seconds')
    Require(1 <= args.rate <= 100000, 'Rate must be 1..100000 TPS')
    Require(1 <= args.wallets <= 100000, 'Wallet count must be 1..100000')
    Require(args.consecutive_blocks in (0, 32), 'Use consecutive-blocks 0 or 32')
    Require(3 <= args.producer_timeout_slots <= 65536, 'Producer timeout must be 3..65536 slots')
    Require(0 < args.early_stop_ratio < 1 and args.early_stop_window > 0 and args.early_stop_seconds > 0,
            'Use a ratio between zero and one and positive early-stop times')
    Require(math.isfinite(args.max_backlog_growth) and args.max_backlog_growth >= 0,
            'Maximum backlog growth must be finite and nonnegative')
    warmup = 5.0
    count = int(args.rate * (warmup + args.duration))
    Require(count <= 100000000, 'Dataset exceeds the sender preparation limit')
    sender = binary / ExeName('sender')
    Require(sender.is_file() and (binary / ExeName('boost')).is_file(), 'Node and sender binaries are required')
    work.mkdir(parents=True, exist_ok=False)
    evidence = work / 'measurement'
    evidence.mkdir()
    estimate = count * 176 * 5 * 3 + (0 if dataset.exists() else count * 176)
    free = shutil.disk_usage(work).free
    Save(work / 'space.json', {'free_bytes': free, 'estimated_database_and_dataset_bytes': estimate,
                               'reserve_bytes': 2 * 1024 ** 3})
    Require(free >= estimate + 2 * 1024 ** 3, 'Insufficient free disk space for the five-node run')
    if not dataset.exists():
        Require(not Path(str(dataset) + '.genesis.json').exists(), 'Dataset genesis exists without its signed transactions')
        dataset.parent.mkdir(parents=True, exist_ok=True)
        Run([sender, '--prepare', dataset, '--count', count, '--wallets', args.wallets], timeout=max(120, count / 5000 + 60))
    Require(dataset.stat().st_size % 176 == 0 and dataset.stat().st_size >= count * 176,
            'Dataset must contain enough complete signed native transactions')
    genesis_path = Path(str(dataset) + '.genesis.json')
    genesis = json.loads(genesis_path.read_text(encoding='utf-8'))
    Require(len(genesis['accounts']) == args.wallets, 'Dataset genesis wallet count must match --wallets')
    slot_ms = 200
    config = {'enabled': True, 'producer_rotation': True,
              'epoch_blocks': 160 if args.consecutive_blocks else 32,
              'iterations': args.iterations, 'slot_ms': slot_ms,
              'start_time_ms': (int(time.time() * 1000) // slot_ms + 5) * slot_ms,
              'fallback_slots': 20, 'future_slots': 2, 'producers': 5}
    if args.consecutive_blocks:
        config.update(consecutive_blocks=args.consecutive_blocks, producer_timeout_slots=args.producer_timeout_slots)
    genesis['vdf'] = config
    Save(work / 'genesis.json', genesis)
    settings = {'produce': 1, 'sync': 1, 'io-threads': 1, 'verify-threads': 1,
                'candidate-threads': 1, 'block-verify-threads': 1, 'query-threads': 1,
                'max-block-txs': 6000, 'block-tx-step': 2000, 'block-ms': 200,
                'relay-transactions': 1, 'clock-sync': 0, 'experiment-index': 1,
                'run-seconds': int(args.duration + warmup + 900)}
    nodes = Lifecycle.LocalInventory(binary, work / 'nodes', 5)
    for index, node in enumerate(nodes):
        node['options'] = {'vdf-bootstrap-produce': int(index == 0)}
    plan = {'warmup': warmup, 'duration': args.duration, 'drain_seconds': 30,
            'ingress': 'shard', 'sample_hz': 10, 'batch': 128, 'poll_seconds': .1,
            'wallets': args.wallets, 'deadline_ms': 1000, 'parallel_ingress': False,
            'max_backlog_growth': args.max_backlog_growth}
    Save(work / 'plan.json', {'measurement': plan, 'node_settings': settings, 'nodes': nodes,
        'early_stop': {'ratio': args.early_stop_ratio, 'window_seconds': args.early_stop_window,
                       'below_threshold_seconds': args.early_stop_seconds}})
    source = Path(__file__).resolve().parent.parent
    revision = subprocess.run(['git', '-c', 'safe.directory=' + str(source), '-C', str(source), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, timeout=10)
    Save(work / 'environment.json', {'platform': platform.platform(), 'python': platform.python_version(),
        'logical_cpus': os.cpu_count(), 'source': str(source), 'source_commit': revision.stdout.strip(),
        'node_sha256': Digest(binary / ExeName('boost')), 'sender_sha256': Digest(sender),
        'benchmark_sha256': Digest(Path(__file__)), 'dataset': str(dataset),
        'dataset_sha256': Digest(dataset), 'dataset_bytes': dataset.stat().st_size,
        'dataset_genesis_sha256': Digest(genesis_path), 'dataset_transactions': dataset.stat().st_size // 176,
        'wallets': args.wallets, 'physical_hosts': 1, 'process_count': 5})

    class ProgressCluster(Lifecycle.Cluster):
        last_progress = 0
        measurement_started = None

        def Stats(self):
            rows = super().Stats()
            now = time.monotonic()
            if now - self.last_progress >= 10:
                self.last_progress = now
                print(json.dumps({'event': 'progress',
                    'measurement_seconds': round(now - self.measurement_started, 2) if self.measurement_started else None,
                    'heights': [row['height'] for row in rows],
                    'transactions': [row['chain_transactions'] for row in rows],
                    'epochs': [row['vdf']['epoch'] for row in rows]}), flush=True)
            return rows

    cluster = ProgressCluster(nodes, 'continuous-benchmark', genesis, settings, evidence)
    result = {'status': 'failed', 'process_count': 5, 'physical_hosts': 1, 'config': config,
              'requested_measurement_seconds': args.duration, 'warmup_seconds': warmup,
              'offered_tps': args.rate, 'expected_transactions': count}
    failure = None
    try:
        cluster.Start()
        until = time.monotonic() + 240
        with (work / 'readiness.jsonl').open('w', encoding='utf-8') as output:
            while True:
                rows = cluster.Stats()
                output.write(json.dumps({'time': time.time(), 'nodes': rows}) + '\n')
                output.flush()
                Require(not any(row['failed'] for row in rows), 'Node failed before measurement')
                epochs, rosters = {row['vdf']['epoch'] for row in rows}, [row['vdf']['producers'] for row in rows]
                if (len(epochs) == 1 and min(epochs) >= 2 and len(rosters[0]) == 5 and
                        all(roster == rosters[0] for roster in rosters) and all(row['peers'] >= 4 for row in rows)):
                    keys = [row['vdf']['public_key'] for row in rows]
                    Require(len(set(keys)) == 5 and set(keys) == set(rosters[0]), 'Five distinct producing identities')
                    Save(work / 'ready.json', {'nodes': rows, 'public_keys': keys})
                    break
                Require(time.monotonic() < until, 'Five matching producers were not ready within 240 seconds')
                time.sleep(.25)
        print(json.dumps({'event': 'measurement_start', 'warmup': warmup, 'duration': args.duration}), flush=True)
        cluster.measurement_started = time.monotonic()
        summary = LoadCase(cluster, plan, dataset, genesis, evidence, args)
        result['metrics'] = summary
        result['state_check'] = summary['checks']
        Save(work / 'final-stats.json', cluster.Stats())
        Require(summary['drained'] and not summary['unresolved_samples'], 'Sent transactions and sampled confirmations must finish')
        if not summary['early_stop']:
            Require(summary['sent_unique'] == count and summary['measurement_seconds'] >= args.duration - max(1, args.duration * .01),
                    'Completed measurement has the requested duration and transaction count')
        result['rotation_check'] = AuditChain(cluster, genesis, work, summary['sent_unique'], not summary['early_stop'])
        result['status'] = 'stopped_early' if summary['early_stop'] else 'complete'
    except BaseException as error:
        failure = error
        result.update(error=repr(error), traceback=traceback.format_exc())
    finally:
        if cluster.started and not (work / 'final-stats.json').exists():
            try:
                Save(work / 'final-stats.json', cluster.Stats())
            except BaseException as error:
                result['final_snapshot_error'] = repr(error)
        try:
            cluster.Close()
        except BaseException as error:
            result.update(status='failed', cleanup_exception=repr(error))
            failure = failure or error
        if cluster.cleanup_errors:
            result.update(status='failed', cleanup_errors=cluster.cleanup_errors)
            failure = failure or RuntimeError('Node cleanup failed')
        Save(work / 'result.json', result)
        print(json.dumps({'event': 'finished', 'status': result['status'], 'result': str(work / 'result.json'),
                          'metrics': result.get('metrics'), 'error': result.get('error'),
                          'cleanup_errors': result.get('cleanup_errors', [])}), flush=True)
    if failure:
        raise failure


def SelfTest():
    guard = EarlyStop(10000, 10, 10, .5)
    for second in range(20):
        Require(guard.Add(second, second * 1000) is None, 'Low TPS must persist before stopping')
    Require(guard.Add(20, 20000)['window_tps'] == 1000, 'Low TPS stops after the configured interval')
    guard = EarlyStop(10000, 10, 10, .5)
    Require(all(guard.Add(second, second * 10000) is None for second in range(50)), 'Healthy throughput continues')
    guard = EarlyStop(10000, 10, 10, .5)
    for second in range(15):
        guard.Add(second, second * 1000)
    for second in range(15, 40):
        Require(guard.Add(second, 14000 + (second - 14) * 10000) is None, 'Recovered throughput clears the stop timer')
    print('{"status":"passed","checks":4,"scope":"benchmark early stopping"}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin', type=Path)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--consecutive-blocks', type=int, default=32, choices=(0, 32))
    parser.add_argument('--producer-timeout-slots', type=int, default=8)
    parser.add_argument('--duration', type=float, default=300)
    parser.add_argument('--rate', type=int, default=10000)
    parser.add_argument('--wallets', type=int, default=1024)
    parser.add_argument('--iterations', type=int, default=65536)
    parser.add_argument('--early-stop-ratio', type=float, default=.5)
    parser.add_argument('--early-stop-window', type=float, default=10)
    parser.add_argument('--early-stop-seconds', type=float, default=10)
    parser.add_argument('--max-backlog-growth', type=float, default=.1)
    parser.add_argument('--self-test', action='store_true')
    arguments = parser.parse_args()
    if arguments.self_test:
        SelfTest()
    else:
        if arguments.bin is None or arguments.work is None or arguments.dataset is None:
            parser.error('--bin, --work and --dataset are required')
        Main(arguments)
