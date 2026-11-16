"""Reproduce the documented R2 per-node lifetime counter summary.

Input is the extracted stage2 directory from the retained retest evidence.
This is read-only for source evidence and does not connect to any node.
"""
import argparse
import csv
import json
import statistics
from pathlib import Path

SOURCE_COMMIT = '9d34b55f1d5bf8ebab1f3fe44a86df31e18b1899'
METRICS = [
    'reorganizations', 'max_reorg_depth', 'fork_reexecute_transactions',
    'branch_append_transactions', 'replayed_transactions', 'committed',
    'local_proposals', 'block_tasks', 'block_queue_wait_ms', 'metadata_tasks',
    'metadata_queue_wait_ms', 'missing_transactions', 'network_bytes',
    'network_sent_bytes', 'db_write_ms', 'db_writes', 'db_batch_bytes',
    'process_write_bytes', 'process_cpu_seconds', 'elapsed_seconds',
    'signature_checks', 'signature_cache_hits', 'execution_cache_hits',
    'execution_cache_misses', 'compact_reconstructed', 'peak_working_set_bytes',
    'cold_signature_ms', 'block_verify_ms', 'verify_worker_ms',
    'candidate_execute_ms', 'commit_execute_ms', 'peak_pending',
]


def Extract(evidence):
    cases = []
    for count in [10, 20]:
        for index, rate in enumerate([20000, 30000], 1):
            relative = f'r2-n{count}-20261116/{index:04}-throughput-r1'
            nodes = []
            for path in sorted((evidence/relative).glob('*-load-stop.json')):
                s = json.loads(path.read_text(encoding='utf-8'))['final.json']
                row = {key: s[key] for key in METRICS}
                row['db_ms_per_write'] = s['db_write_ms']/s['db_writes'] if s['db_writes'] else None
                row['block_wait_ms_per_task'] = s['block_queue_wait_ms']/s['block_tasks'] if s['block_tasks'] else None
                row['metadata_wait_ms_per_task'] = s['metadata_queue_wait_ms']/s['metadata_tasks'] if s['metadata_tasks'] else None
                row['fork_reexecute_per_committed'] = s['fork_reexecute_transactions']/s['committed'] if s['committed'] else None
                cache_lookups = s['execution_cache_hits']+s['execution_cache_misses']
                row['execution_cache_hit_fraction'] = s['execution_cache_hits']/cache_lookups if cache_lookups else None
                nodes.append(row)
            if len(nodes) != count:
                raise ValueError(f'{relative}: expected {count} load-process snapshots, found {len(nodes)}')
            aggregate = {}
            for key in nodes[0]:
                values = [n[key] for n in nodes if isinstance(n[key], (float, int))]
                if len(values) != count:
                    raise ValueError(f'{relative}: missing numeric {key}')
                aggregate[key] = {'median': statistics.median(values), 'min': min(values),
                                  'max': max(values), 'sum': sum(values)}
            cases.append({'nodes': count, 'offered_tps': rate,
                          'source_pattern': relative+'/*-load-stop.json:final.json',
                          'statistics': aggregate})
    return {'source_commit': SOURCE_COMMIT,
            'scope': 'Each load process full startup-to-stop cumulative counters; not the audited 10-second TPS window. Task waiting sums overlap across tasks and threads; no CPU percent derived. Each case has one run.',
            'cases': cases}


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-root', type=Path, required=True,
                        help='Extracted stage2 directory from the retained evidence archive')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = Extract(args.evidence_root)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'diagnostics.json').write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    fields = ['nodes', 'offered_tps']+list(output['cases'][0]['statistics'])
    with (args.output/'diagnostics.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for case in output['cases']:
            writer.writerow({'nodes': case['nodes'], 'offered_tps': case['offered_tps'],
                             **{key: value['median'] for key, value in case['statistics'].items()}})


if __name__ == '__main__':
    Main()
