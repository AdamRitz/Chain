"""Compare duplicate transaction ingress through real TCP, then verify ledger state."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import struct
import sys
import threading
import time


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for part in iter(lambda: source.read(1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def cpu_seconds(process):
    if os.name == 'nt':
        values = [ctypes.c_uint64() for _ in range(4)]
        function = ctypes.windll.kernel32.GetProcessTimes
        function.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
        if not function(int(process._handle), *(ctypes.byref(x) for x in values)):
            raise ctypes.WinError()
        return (values[2].value + values[3].value) / 10000000
    fields = Path('/proc/{}/stat'.format(process.pid)).read_text().rsplit(')', 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')


def remove_database(root, database):
    # Only delete the exact database created by this run, after its process exits.
    if not (root / 'benchmark-owner.json').is_file():
        raise ValueError('Missing benchmark ownership record')
    if database.name != 'db' or database.parent.parent != root:
        raise ValueError('Unexpected database path')
    if database.is_symlink() or database.parent.is_symlink():
        raise ValueError('Database path is a symbolic link')
    if not database.resolve().is_relative_to(root.resolve()):
        raise ValueError('Database is outside the benchmark directory')
    if database.exists():
        shutil.rmtree(database)


def run_case(label, binary, args, work, dataset, transactions, helpers):
    StartNode, StopNode, GetStats, WaitStats, VerifyAccounts, MakeBlock, Peer, ReadAll = helpers
    case = work / label
    case.mkdir()
    save(case / 'benchmark-owner.json', {'kind': 'duplicate-ingress', 'label': label})
    config = case / 'node.yaml'
    config.write_text('node: {}\n', encoding='utf-8')
    count = len(transactions)
    settings = {'config': config, 'genesis': str(dataset) + '.genesis.json',
                'produce': 0, 'peer-discovery': 0, 'target-peers': 0,
                'io-threads': 2, 'verify-threads': 2, 'candidate-threads': 2,
                'block-verify-threads': 2, 'query-threads': 1, 'max-pending': 32768,
                'max-pool': max(200000, count + 6000), 'block-ms': 200,
                'round-ms': 0, 'max-block-txs': 6000,
                'clock-sync': 0, 'tx-push-peers': 0,
                'run-seconds': int(args.seconds + args.drain_seconds + 180)}
    node = StartNode(binary, case, 'node', **settings)
    peers, senders, errors = [], [], []
    stop = threading.Event()
    report = {'label': label, 'binary_sha256': digest(binary / ('boost.exe' if os.name == 'nt' else 'boost')),
              'settings': {k: str(v) if isinstance(v, Path) else v for k, v in settings.items()},
              'unique_count': count, 'copies': args.copies, 'body_count': count * args.copies,
              'passed': False}

    class DrainingPeer(Peer):
        def __init__(self, port):
            self.send_lock = threading.Lock()
            super().__init__(port, tx_inventory=True)
            self.socket.settimeout(None)
            self.barriers = queue.Queue()
            self.reader = threading.Thread(target=self.drain, daemon=True)
            self.reader.start()

        def Send(self, kind, body=b''):
            with self.send_lock:
                return super().Send(kind, body)

        def drain(self):
            try:
                while not stop.is_set():
                    kind, length = struct.unpack('<BI', ReadAll(self.socket, 5))
                    if length > 2 * 1024 * 1024:
                        raise ValueError('Oversized peer response')
                    body = ReadAll(self.socket, length)
                    if kind == 13:
                        self.Send(14, json.dumps(self.capabilities).encode())
                    elif kind == 20:
                        self.barriers.put(body)
            except Exception as error:
                if not stop.is_set():
                    errors.append(str(error))

        def close_and_join(self):
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.Close()
            self.reader.join(timeout=3)

    try:
        for _ in range(args.copies):
            peers.append(DrainingPeer(node['port']))
        batches = [(offset, b''.join(transactions[offset:offset + args.batch]))
                   for offset in range(0, count, args.batch)]
        before = GetStats(node['port'])
        cpu_start = cpu_seconds(node['process'])
        started = time.monotonic() + .25
        sent = [0] * args.copies
        finishes = [None] * args.copies
        maximum_lag = [0.0] * args.copies

        def send(index, peer):
            try:
                for offset, payload in batches:
                    deadline = started + offset / args.rate
                    if stop.wait(max(0, deadline - time.monotonic())):
                        return
                    maximum_lag[index] = max(maximum_lag[index], time.monotonic() - deadline)
                    peer.Send(10, payload)
                    sent[index] += len(payload) // 176
                peer.Send(19)
                peer.barriers.get(timeout=args.drain_seconds)
                finishes[index] = time.monotonic()
            except Exception as error:
                errors.append('sender {}: {}'.format(index, error))

        for index, peer in enumerate(peers):
            thread = threading.Thread(target=send, args=(index, peer), daemon=True)
            senders.append(thread)
            thread.start()
        peak = 0
        deadline = started + args.seconds + args.drain_seconds
        with (case / 'timeline.jsonl').open('w', encoding='utf-8') as trace:
            while True:
                stats = GetStats(node['port'])
                now = time.monotonic()
                row = {'seconds': now - started, 'cpu_seconds': cpu_seconds(node['process']) - cpu_start,
                       'sent_by_copy': list(sent), 'stats': stats}
                trace.write(json.dumps(row) + '\n')
                peak = max(peak, stats['pending_tx_verify'])
                if errors:
                    raise RuntimeError('; '.join(errors))
                if (now >= started + args.seconds and all(not thread.is_alive() for thread in senders) and
                        stats['valid'] == count and stats['pending_tx_verify'] == 0):
                    break
                if now >= deadline:
                    raise TimeoutError('Transaction ingress did not drain: ' + json.dumps(stats))
                time.sleep(.05)
        ingress_seconds = now - started
        if sent != [count] * args.copies or stats['pool'] != count or stats['failed']:
            raise AssertionError('Unique transaction count or pool state differs')
        if stats['received'] - before['received'] != count * args.copies:
            raise AssertionError('Received body count differs from all transmitted copies')
        if stats['duplicate'] - before['duplicate'] != count * (args.copies - 1):
            raise AssertionError('Duplicate counter must include merged and already-known copies')
        if stats['invalid'] != before['invalid'] or stats['rejected'] != before['rejected']:
            raise AssertionError('A valid benchmark transaction was rejected')
        counters = ['received', 'valid', 'duplicate', 'invalid', 'rejected', 'signature_checks',
                    'signature_cache_hits', 'verify_worker_ms', 'pool_worker_ms',
                    'tx_queue_wait_ms', 'tx_work_merged', 'tx_work_pool_known', 'tx_work_capacity']
        report.update(ingress_seconds=ingress_seconds, admitted_unique_tps=count / ingress_seconds,
                      load_seconds=args.seconds, unique_rate=args.rate,
                      latest_sender_finish_seconds=max(finishes) - started,
                      maximum_sender_lag_ms=max(maximum_lag) * 1000,
                      peak_pending_tx_verify=peak,
                      cpu_seconds=cpu_seconds(node['process']) - cpu_start,
                      counters={key: stats[key] - before.get(key, 0) for key in counters if key in stats},
                      ingress_final=stats)
        # Identical blocks make the final hash and every account directly comparable.
        parent = bytes.fromhex(stats['head'])
        committed = 0
        for height, offset in enumerate(range(0, count, 6000), 2):
            block_transactions = transactions[offset:offset + 6000]
            block = MakeBlock(parent, height, block_transactions)
            peers[0].Send(7, block)
            parent = block[80:112]
            committed += len(block_transactions)
            WaitStats(node['port'], lambda s: s['head'] == parent.hex() and
                      s['chain_transactions'] == committed, timeout=30)
        final = WaitStats(node['port'], lambda s: s['pool'] == 0 and s['pending_tx_verify'] == 0)
        accounts = VerifyAccounts(node['port'], dataset, count, str(dataset) + '.genesis.json')
        report.update(passed=True, final_head=final['head'], chain_transactions=final['chain_transactions'],
                      accounts=accounts, final=final)
        return report
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        stop.set()
        for peer in peers:
            peer.close_and_join()
        for sender in senders:
            sender.join(timeout=3)
        StopNode(node)
        if not args.keep_data and node['process'].poll() is not None:
            remove_database(case, node['directory'] / 'db')
            report['database_removed'] = True
        save(case / 'result.json', report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-bin', type=Path, required=True)
    parser.add_argument('--candidate-bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--rate', type=int, default=10000)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--copies', type=int, default=6)
    parser.add_argument('--batch', type=int, default=256)
    parser.add_argument('--wallets', type=int, default=1024)
    parser.add_argument('--drain-seconds', type=float, default=30)
    parser.add_argument('--keep-data', action='store_true')
    args = parser.parse_args()
    count = int(args.rate * args.seconds)
    if not (1 <= args.rate <= 100000 and 0 < args.seconds <= 60 and 1 <= count <= 1000000 and
            1 <= args.copies <= 16 and 1 <= args.batch <= 10000 and 1 <= args.wallets <= count and
            1 <= args.drain_seconds <= 120):
        parser.error('Benchmark parameters are outside supported limits')
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    binary_a, binary_b = args.baseline_bin.resolve(), args.candidate_bin.resolve()
    sys.path.insert(0, str(args.repo.resolve() / 'Test'))
    from NetworkTest import StartNode, StopNode, GetStats, WaitStats, VerifyAccounts, ReadAll, Run, ExeName
    from ForkNetworkTest import MakeBlock
    from RelayOptimizationNetworkTest import Peer
    dataset = work / 'transactions.bin'
    Run([binary_a / ExeName('sender'), '--prepare', dataset, '--count', count, '--wallets', args.wallets], timeout=120)
    raw = dataset.read_bytes()
    if len(raw) != count * 176:
        raise ValueError('Prepared dataset has unexpected length')
    transactions = [raw[offset:offset + 176] for offset in range(0, len(raw), 176)]
    report = {'dataset_sha256': digest(dataset), 'unique_count': count, 'rate': args.rate,
              'seconds': args.seconds, 'copies': args.copies, 'batch': args.batch, 'runs': [], 'passed': False}
    helpers = StartNode, StopNode, GetStats, WaitStats, VerifyAccounts, MakeBlock, Peer, ReadAll
    try:
        for label, binary in [('baseline', binary_a), ('candidate', binary_b)]:
            report['runs'].append(run_case(label, binary, args, work, dataset, transactions, helpers))
            save(work / 'result.json', report)
        left, right = report['runs']
        if left['final_head'] != right['final_head'] or left['accounts'] != right['accounts']:
            raise AssertionError('A/B final ledger states differ')
        report['passed'] = True
    finally:
        save(work / 'result.json', report)
    print(json.dumps({'passed': report['passed'], 'result': str(work / 'result.json'),
                      'runs': [{key: row[key] for key in ('label', 'admitted_unique_tps', 'cpu_seconds',
                                                        'peak_pending_tx_verify', 'maximum_sender_lag_ms')}
                               for row in report['runs']]}))


if __name__ == '__main__':
    main()
