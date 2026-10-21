"""Join a node trace with an independently measured reference on that host's steady axis.

Reference rows: {steady_us, reference_logical_us, reference_uncertainty_us}.
The reference must be independently calibrated; peer medians are not ground truth.
"""
import argparse
import bisect
import json
from pathlib import Path


def Label(trace, reference, output, run, node, scenario, max_gap_us=2000):
    rows = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines() if line.strip()]
    truth = sorted([json.loads(line) for line in reference.read_text(encoding='utf-8').splitlines() if line.strip()], key=lambda row: row['steady_us'])
    stamps = [row['steady_us'] for row in truth]
    observations = {row['feature_version']: row for row in rows if row.get('kind') == 'observation'}
    count = 0
    with output.open('w', encoding='utf-8', newline='\n') as destination:
        for row in rows:
            if row.get('kind') != 'candidate' or row.get('outcome') in ['empty', 'invalid']:
                continue
            observation = observations.get(row['feature_version'])
            if not observation or not stamps:
                continue
            at = observation['steady_us']; index = bisect.bisect_left(stamps, at)
            nearest = min([i for i in [index-1, index] if 0 <= i < len(truth)], key=lambda i: abs(stamps[i]-at))
            sample = truth[nearest]; gap = abs(stamps[nearest]-at)
            if gap > max_gap_us or sample['reference_uncertainty_us'] < 0:
                continue
            # Propagate a calibrated reference at nominal rate; include its uncertainty and alignment gap.
            target = sample['reference_logical_us']+at-sample['steady_us']
            error = abs(observation['logical_us']-target)+sample['reference_uncertainty_us']+gap
            labeled = {'schema': 1, 'feature_schema': 'chain-clock-v1-32', 'run': run, 'node': node,
                       'scenario': scenario, 'features': row['features'], 'time_error_ms': error/1000,
                       'prepare_ms': row['prepare_us']/1000, 'prepare_censored': row['censored'],
                       'late': row['outcome'] == 'late', 'label_source': 'controlled-reference'}
            destination.write(json.dumps(labeled, allow_nan=False)+'\n'); count += 1
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['trace', 'reference', 'output']:
        parser.add_argument('--'+name, type=Path, required=True)
    for name in ['run', 'node', 'scenario']:
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    print(json.dumps({'labeled': Label(**vars(args))}))
