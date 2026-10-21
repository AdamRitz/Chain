"""Offline 0.95 linear quantiles. NumPy is only needed for training, never by a node."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import numpy as np
from Simulate import FEATURES


def Split(run):
    value = int(hashlib.sha256(run.encode()).hexdigest()[:8], 16) % 10
    return 'train' if value < 6 else 'calibration' if value < 8 else 'test'


def Fit(x, y, steps=1600):
    weights = np.zeros(x.shape[1]); bias = float(np.quantile(y, .95, method='higher'))
    first = np.zeros(x.shape[1]+1); second = first.copy()
    for step in range(1, steps+1):
        residual = y-(x@weights+bias)
        gradient = np.where(residual > 0, -.95, .05)
        derivatives = np.r_[x.T@gradient/len(y)+1e-4*weights, gradient.mean()]
        first = .9*first+.1*derivatives; second = .999*second+.001*derivatives**2
        change = .02*(first/(1-.9**step))/(np.sqrt(second/(1-.999**step))+1e-8)
        weights -= change[:-1]; bias -= change[-1]
    return weights, bias


def Train(path, output, report):
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    kinds = {row.get('label_source') for row in rows}
    if len(kinds) != 1 or not kinds <= {'simulation', 'controlled-reference'}:
        raise ValueError('Use one controlled reference label source; raw node traces are unlabeled')
    for row in rows:
        if row.get('feature_schema') != 'chain-clock-v1-32' or not row.get('run'):
            raise ValueError('Feature schema and whole-run identity are required')
    x = np.asarray([row['features'] for row in rows], dtype=float)
    y = np.asarray([[row['time_error_ms'], row['prepare_ms']] for row in rows], dtype=float)
    if x.shape != (len(rows), 32) or not np.isfinite(x).all() or not np.isfinite(y).all() or (y < 0).any():
        raise ValueError('Invalid features or reference labels')
    # Hold out the combined fault scenario in its entirety. A mixed phase also holds
    # out the rest of that run, including all nodes and adjacent feature windows.
    heldout_runs = {row['run'] for row in rows if row['scenario'] == 'mixed'}
    assignments = {row['run']: ('test' if row['run'] in heldout_runs else Split(row['run'])) for row in rows}
    masks = {split: np.array([assignments[row['run']] == split for row in rows]) for split in ['train', 'calibration', 'test']}
    if any(mask.sum() < 32 for mask in masks.values()):
        raise ValueError('Need at least 32 observations in each whole-run split')
    mean, scale = x[masks['train']].mean(axis=0), x[masks['train']].std(axis=0)
    scale = np.maximum(scale, 1.)
    normalized = (x-mean)/scale
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pack = {'schema': 1, 'feature_schema': 'chain-clock-v1-32', 'features': FEATURES, 'quantile': .95,
            'model_id': f'{next(iter(kinds))}-{digest[:12]}', 'mean': mean.tolist(), 'scale': scale.tolist(),
            'minimum': np.minimum(0, x[masks['train']].min(axis=0)).tolist(),
            'maximum': (np.maximum(1., x[masks['train']].max(axis=0))*1.5+1).tolist(),
            'training': {'kind': next(iter(kinds)), 'split': 'run', 'dataset_sha256': digest,
                         'optimizer': 'batch-adam-pinball', 'steps': 1600,
                         'python': platform.python_version(), 'numpy': np.__version__,
                         'heldout_scenarios': ['mixed'], 'calibration': 'worst-known-scenario'}}
    metrics = {'dataset_sha256': digest, 'label_source': next(iter(kinds)), 'split': 'whole run',
               'counts': {key: int(mask.sum()) for key, mask in masks.items()},
               'runs': {key: sorted({row['run'] for row in rows if assignments[row['run']] == key}) for key in masks},
               'heldout_scenarios': ['mixed'],
               'censored_prepare_rows': sum(bool(row.get('prepare_censored')) for row in rows),
               'coverage_scope': 'fully observed labels only; censored rows retained and counted'}
    for column, name in enumerate(['time', 'prepare']):
        observed = np.ones(len(rows), dtype=bool) if name == 'time' else np.array([not row.get('prepare_censored') for row in rows])
        train = masks['train'] & observed; calibration = masks['calibration'] & observed; test = masks['test'] & observed
        weights, bias = Fit(normalized[train], y[train, column])
        raw = normalized@weights+bias
        correction = max(0., float(np.quantile(y[calibration, column]-raw[calibration], .95, method='higher')))
        # Conservative calibration across known environments; the test/mixed rows
        # remain untouched until evaluation. This is empirical, not a safety bound.
        for scenario in sorted({row['scenario'] for row in rows}):
            subset = calibration & np.array([row['scenario'] == scenario for row in rows])
            if subset.any():
                correction = max(correction, float(np.quantile(y[subset, column]-raw[subset], .95, method='higher')))
        predicted = np.maximum(.25, raw+correction)
        residual = y[test, column]-predicted[test]
        pack[name] = {'bias_ms': bias, 'calibration_ms': correction, 'weights': weights.tolist()}
        metrics[name] = {'test_rows': int(test.sum()), 'coverage': float(np.mean(residual <= 0)),
                         'pinball_ms': float(np.mean(np.maximum(.95*residual, -.05*residual))),
                         'predicted_p95_ms': float(np.quantile(predicted[test], .95))}
        metrics[name]['by_scenario'] = {}
        for scenario in sorted({row['scenario'] for row in rows}):
            subset = test & np.array([row['scenario'] == scenario for row in rows])
            if subset.any():
                metrics[name]['by_scenario'][scenario] = {'rows': int(subset.sum()),
                    'coverage': float(np.mean(y[subset, column] <= predicted[subset]))}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(pack, indent=2, allow_nan=False)+'\n', encoding='utf-8', newline='\n')
    metrics['model_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(metrics, indent=2, allow_nan=False)+'\n', encoding='utf-8', newline='\n')
    print(json.dumps({key: value for key, value in metrics.items() if key not in ['runs']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    Train(args.input, args.output, args.report)
