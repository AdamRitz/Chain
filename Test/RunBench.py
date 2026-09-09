"""Reproducible local commit benchmarks; requires Python 3 and psutil.

Transactions are signed before timing. Every case uses a new database and checks
the final committed count and resulting account state. This measures local account
execution and persistence; consensus finality is a separate benchmark.
"""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import time
import psutil
from NetworkTest import FLAGS, Run, GetStats, StartNode, StopNode, SendFrame, VerifyAccounts


def RunNode(binary, work, dataset, count, threads, sync, batch, repeat, block_ms=50, genesis=None, sender_binary=None):
    name = f'node-v{threads}-s{sync}-b{batch}-m{block_ms}-r{repeat}'
    node = StartNode(binary, work, name, **{'io-threads': 4, 'verify-threads': threads,
        'block-ms': block_ms, 'max-block-txs': 6000, 'sync': sync, 'run-seconds': 120,
        **({'genesis': genesis} if genesis else {})})
    process = psutil.Process(node['process'].pid)
    cpu_start = sum(process.cpu_times()[:2])
    io_start = process.io_counters()
    rss = process.memory_info().rss
    started = time.perf_counter()
    sender = None
    try:
        sender = subprocess.Popen([str((sender_binary or binary)/'sender.exe'), '--file', str(dataset),
            '--count', str(count), '--batch', str(batch), '--connections', '8',
            '--port', str(node['port'])], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=FLAGS)
        while time.perf_counter()-started < 110:
            stats = GetStats(node['port'])
            rss = max(rss, process.memory_info().rss)
            if stats['failed'] or stats['rejected'] or stats['invalid']:
                raise RuntimeError(f'Node rejected data: {stats}')
            if stats['committed'] == count and stats['pending'] == 0 and stats['pool'] == 0:
                break
            if stats['committed'] > count:
                raise RuntimeError('More transactions committed than submitted')
            time.sleep(.02)
        else:
            raise TimeoutError(f'Commit timed out: {stats}')
        elapsed = time.perf_counter()-started
        cpu = sum(process.cpu_times()[:2])-cpu_start
        io_end = process.io_counters()
        stdout, stderr = sender.communicate(timeout=5)
        if sender.returncode:
            raise RuntimeError(f'Sender failed: {stdout} {stderr}')
        result = {'name': name, 'count': count, 'threads': threads, 'sync': sync,
            'batch': batch, 'block_ms': block_ms, 'repeat': repeat, 'stats': stats,
            'client_to_commit_seconds': elapsed, 'client_to_commit_tps': count/elapsed,
            'node_cpu_seconds': cpu, 'average_cpu_cores': cpu/elapsed,
            'total_cpu_percent': cpu/elapsed/psutil.cpu_count()*100,
            'peak_rss_bytes': rss, 'process_write_bytes': io_end.write_bytes-io_start.write_bytes,
            'process_read_bytes': io_end.read_bytes-io_start.read_bytes,
            'sender': json.loads(stdout.strip().splitlines()[-1])}
        if genesis:
            result['account_check'] = VerifyAccounts(node['port'], dataset, count, genesis)
        (node['directory']/'measured.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        return result
    finally:
        if sender is not None and sender.poll() is None:
            sender.terminate()
            sender.communicate(timeout=5)
        StopNode(node)


def RunLatency(binary, work, dataset, block_ms, count=50, genesis=None):
    node = StartNode(binary, work, f'latency-{block_ms}', **{'io-threads': 4,
        'verify-threads': 8, 'block-ms': block_ms, 'max-block-txs': 6000, 'sync': 1,
        **({'genesis': genesis} if genesis else {})})
    samples = []
    try:
        with dataset.open('rb') as source:
            for i in range(count):
                tx = source.read(176)
                started = time.perf_counter()
                SendFrame(node['port'], 1, tx)
                while time.perf_counter()-started < 5:
                    if GetStats(node['port'])['committed'] == i+1:
                        samples.append((time.perf_counter()-started)*1000)
                        break
                    time.sleep(.001)
                else:
                    raise TimeoutError('Latency sample timed out')
        ordered = sorted(samples)
        if genesis:
            VerifyAccounts(node['port'], dataset, count, genesis)
        return {'block_ms': block_ms, 'count': count, 'sync': 1, 'samples_ms': samples,
                'p50_ms': statistics.median(samples),
                'p95_ms': ordered[int(.95*(count-1))], 'p99_ms': ordered[int(.99*(count-1))]}
    finally:
        StopNode(node)


def RunReplica(binary, work, dataset, count, repeat, genesis=None):
    nodes = []
    try:
        settings = {'io-threads': 4, 'verify-threads': 8, 'block-ms': 50, 'max-block-txs': 6000, 'sync': 1}
        if genesis:
            settings['genesis'] = genesis
        leader = StartNode(binary, work, f'replica-leader-{repeat}', **settings)
        nodes.append(leader)
        follower = StartNode(binary, work, f'replica-follower-{repeat}', produce=0,
                             seed=f'127.0.0.1:{leader["port"]}', **settings)
        nodes.append(follower)
        processes = [psutil.Process(x['process'].pid) for x in nodes]
        cpu_start = [sum(p.cpu_times()[:2]) for p in processes]
        started = time.perf_counter()
        sent = Run([binary/'sender.exe', '--file', dataset, '--count', count,
                    '--batch', 128, '--connections', 8, '--port', leader['port']])
        while time.perf_counter()-started < 80:
            first, second = GetStats(leader['port']), GetStats(follower['port'])
            if first['failed'] or second['failed'] or first['rejected']:
                raise RuntimeError('Replica node failed or rejected transactions')
            if first['committed'] == count and second['committed'] == count and first['head'] == second['head']:
                elapsed = time.perf_counter()-started
                cpu_cores = [(sum(p.cpu_times()[:2])-start)/elapsed for p,start in zip(processes,cpu_start)]
                account_check = [VerifyAccounts(n['port'], dataset, count, genesis) for n in nodes] if genesis else []
                return {'repeat': repeat, 'count': count, 'sync': 1, 'seconds': elapsed,
                        'replicated_tps': count/elapsed, 'leader': first, 'follower': second,
                        'average_cpu_cores': cpu_cores, 'account_check': account_check,
                        'sender': sent}
            time.sleep(.02)
        raise TimeoutError(f'Replication timed out: {first} {second}')
    finally:
        for node in reversed(nodes):
            StopNode(node)


def Main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--genesis', type=Path)
    parser.add_argument('--count', type=int, default=500000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--suite', choices=['all', 'node', 'disk', 'replica'], default='all')
    parser.add_argument('--threads', type=int, nargs='+', default=[1, 4, 8, 16, 24])
    args = parser.parse_args()
    binary, work = args.bin.resolve(), args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    dataset = args.dataset.resolve() if args.dataset else work/'transactions.bin'
    if not args.dataset:
        Run([binary/'sender.exe', '--prepare', dataset, '--count', args.count], timeout=120)
    if args.count < 1000 or dataset.stat().st_size < args.count*176:
        raise ValueError('At least 1000 valid transactions are required')
    genesis = args.genesis.resolve() if args.genesis else Path(str(dataset)+'.genesis.json')
    if not genesis.is_file():
        raise ValueError('Provide --genesis containing the dataset accounts and initial balances')
    results = {'count': args.count, 'logical_cpus': psutil.cpu_count(), 'micro': [],
               'storage': [], 'node': [], 'latency': [], 'replica': [], 'work': str(work)}
    def Save(section, result):
        results[section].append(result)
        (work/'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        if section == 'node':
            print(json.dumps({'case': result['name'], 'tps': result['stats']['local_commit_tps'],
                              'cpu_cores': result['average_cpu_cores'], 'db_ms': result['stats']['db_write_ms']}), flush=True)
        else:
            print(json.dumps({'section': section, **{k: v for k, v in result.items() if k != 'samples_ms'}}), flush=True)
    if args.suite == 'replica':
        for repeat in range(args.repeats):
            Save('replica', RunReplica(binary, work, dataset, args.count, repeat, genesis=genesis))
        return
    if args.suite == 'all':
        for repeat in range(args.repeats):
            for threads in args.threads:
                result = Run([binary/'bench.exe', '--file', dataset, '--count', args.count,
                              '--mode', 'verify', '--threads', threads], timeout=120)
                result['repeat'] = repeat
                Save('micro', result)
        Save('micro', Run([binary/'bench.exe', '--file', dataset, '--count', args.count, '--mode', 'hash']))
    if args.suite in ('all', 'disk'):
        for sync in (0, 1):
            for mode in ('db-put', 'db-batch'):
                for repeat in range(args.repeats):
                    # Bound fsync-per-transaction test duration even on HDD.
                    count = min(args.count, 2000 if sync else 50000)
                    result = Run([binary/'bench.exe', '--file', dataset, '--count', count,
                        '--mode', mode, '--sync', sync, '--batch', 1000,
                        '--data', work/f'{mode}-s{sync}-r{repeat}'], timeout=180)
                    result['repeat'] = repeat
                    Save('storage', result)
    for repeat in range(args.repeats):
        for threads in args.threads:
            Save('node', RunNode(binary, work, dataset, args.count, threads, 1, 128, repeat, genesis=genesis))
        if 8 in args.threads:
            Save('node', RunNode(binary, work, dataset, args.count, 8, 0, 128, repeat, genesis=genesis))
            Save('node', RunNode(binary, work, dataset, args.count, 8, 1, 1, repeat, genesis=genesis))
    for block_ms in (10, 50):
        Save('latency', RunLatency(binary, work, dataset, block_ms, genesis=genesis))


if __name__ == '__main__':
    Main()
