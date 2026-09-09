"""Interleaved before/after comparison with identical signed data and sender binary."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import time
import psutil
from RunBench import RunNode, RunLatency


def Digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def Main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--before-bin', type=Path, required=True)
    parser.add_argument('--after-bin', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--genesis', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--count', type=int, default=500000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--threads', type=int, nargs='+', default=[8, 24])
    parser.add_argument('--latency-count', type=int, default=100)
    args = parser.parse_args()
    if args.count < 1000 or args.repeats < 1:
        raise ValueError('Use at least 1000 transactions and one repeat')
    binary = {'before': args.before_bin.resolve(), 'after': args.after_bin.resolve()}
    dataset, genesis, work = args.dataset.resolve(), args.genesis.resolve(), args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    for phase in binary:
        (work/phase).mkdir()
    result = {'count': args.count, 'repeats': args.repeats, 'threads': args.threads,
              'logical_cpus': psutil.cpu_count(), 'physical_cpus': psutil.cpu_count(logical=False),
              'memory_bytes': psutil.virtual_memory().total, 'platform': platform.platform(),
              'dataset_sha256': Digest(dataset), 'genesis_sha256': Digest(genesis),
              'sender_sha256': Digest(binary['after']/'sender.exe'),
              'binary_sha256': {phase: Digest(path/'boost.exe') for phase,path in binary.items()},
              'settings': {'io_threads': 4, 'connections': 8, 'batch': 128, 'max_block_txs': 6000,
                           'block_ms': 50, 'sync': 1, 'base_fee': json.loads(genesis.read_text())['base_fee']},
              'before': {'node': [], 'latency': []}, 'after': {'node': [], 'latency': []}}
    def Save():
        (work/'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    for phase in binary:
        RunNode(binary[phase], work/phase, dataset, min(args.count,50000), 8, 1, 128, -1,
                genesis=genesis if phase == 'after' else None, sender_binary=binary['after'])
    for repeat in range(args.repeats):
        for threads in args.threads:
            phases = ['before','after'] if repeat % 2 == 0 else ['after','before']
            for phase in phases:
                item = RunNode(binary[phase], work/phase, dataset, args.count, threads, 1, 128, repeat,
                               genesis=genesis if phase == 'after' else None, sender_binary=binary['after'])
                result[phase]['node'].append(item)
                Save()
                print(json.dumps({'phase': phase, 'threads': threads, 'repeat': repeat,
                                  'tps': item['stats']['local_commit_tps'], 'cpu_percent': item['total_cpu_percent'],
                                  'account_check': item.get('account_check')}), flush=True)
    for block_ms in [10,50]:
        for phase in binary:
            result[phase]['latency'].append(RunLatency(binary[phase], work/phase, dataset, block_ms,
                count=args.latency_count, genesis=genesis if phase == 'after' else None))
            Save()
    summary = []
    for threads in args.threads:
        medians = {}
        for phase in binary:
            cases = [case for case in result[phase]['node'] if case['threads'] == threads]
            medians[phase] = {'tps': statistics.median(case['stats']['local_commit_tps'] for case in cases),
                              'cpu_percent': statistics.median(case['total_cpu_percent'] for case in cases),
                              'peak_rss_bytes': statistics.median(case['peak_rss_bytes'] for case in cases)}
        summary.append({'threads': threads, **medians,
                        'change_percent': (medians['after']['tps']/medians['before']['tps']-1)*100})
    result['summary'] = summary
    Save()
    print(json.dumps({'complete': True, 'summary': summary}), flush=True)


if __name__ == '__main__':
    Main()
