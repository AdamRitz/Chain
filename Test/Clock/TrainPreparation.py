"""Train a bounded preparation-lead model from complete local monotonic work labels.

Time uncertainty retains its original independently labelled simulation head.
Cancelled candidates completed preparation before waiting; their work labels are
observed, although their eventual publication outcome is censored. No peer clock
or transaction confirmation time is used as a time-error training label.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform

import numpy as np
import sklearn
from sklearn.ensemble import GradientBoostingRegressor

FEATURES = [10, 11, 12, 13, 14, 15, 18, 19, 20, 23, 24, 25, 28, 29, 30]


def Train(root, base, output, report, quantile=.75):
    rows, manifest = [], []
    for group in 'ABCD':
        for rate in [20000, 30000, 40000, 50000, 60000]:
            directory = root / f'{group}-{rate}'
            result = json.loads((directory / 'result.json').read_text())
            if result['status'] != 'complete' or not result.get('state_audit', {}).get('passed'):
                raise ValueError('Only completed state-audited runs may supply work labels')
            for path in sorted(directory.glob('node*-clock.jsonl')):
                content = path.read_bytes()
                manifest.append({'file': str(path.relative_to(root)), 'sha256': hashlib.sha256(content).hexdigest()})
                for line in content.splitlines():
                    row = json.loads(line)
                    if row['kind'] != 'candidate' or row['outcome'] not in ['offered', 'not_selected', 'late', 'cancelled']:
                        continue
                    if row.get('label_source') != 'local_monotonic' or row.get('prepare_completed') is False:
                        raise ValueError('Preparation needs a completed local monotonic label')
                    if len(row['features']) != 32 or not 0 <= row['prepare_us'] <= 1000000:
                        raise ValueError('Invalid work record')
                    rows.append((group, rate, row['features'], row['prepare_us'] / 1000))
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    x = np.asarray([row[2] for row in rows]); y = np.asarray([row[3] for row in rows])
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError('Finite causal features and work labels required')
    masks = {name: np.array([row[0] in groups for row in rows])
             for name, groups in [('train', 'A'), ('calibration', 'B'), ('exploratory_validation', 'CD')]}
    if any(mask.sum() < 100 for mask in masks.values()):
        raise ValueError('Need whole independent experiments in each split')
    model = GradientBoostingRegressor(loss='quantile', alpha=quantile, n_estimators=24,
        max_depth=2, min_samples_leaf=80, learning_rate=.12, random_state=20261022)
    model.fit(x[masks['train']][:, FEATURES], y[masks['train']])
    raw = model.predict(x[:, FEATURES])
    correction = max(0., float(np.quantile(y[masks['calibration']] - raw[masks['calibration']], quantile, method='higher')))
    trees = []
    for estimator in model.estimators_[:, 0]:
        tree = estimator.tree_; nodes = []
        for index in range(tree.node_count):
            leaf = tree.children_left[index] == -1
            nodes.append({'feature': -1 if leaf else FEATURES[int(tree.feature[index])],
                'left': int(tree.children_left[index]), 'right': int(tree.children_right[index]),
                'threshold': float(tree.threshold[index]), 'value': float(tree.value[index, 0, 0])})
        trees.append(nodes)
    # Fit-domain bounds use training runs only. Unseen work regimes still fall back.
    minimum = np.minimum(0, x[masks['train']].min(0))
    maximum = np.maximum(1, x[masks['train']].max(0)) * 1.5 + 1
    preparation = {'kind': 'gradient-boosted-quantile', 'label_source': 'local-monotonic',
        'split': 'whole-experiment', 'dataset_sha256': digest, 'quantile': quantile,
        'features': FEATURES, 'bias_ms': float(model.init_.constant_[0, 0]),
        'learning_rate': float(model.learning_rate), 'calibration_ms': correction,
        'minimum': minimum.tolist(), 'maximum': maximum.tolist(), 'trees': trees,
        'training_groups': ['A'], 'calibration_groups': ['B'],
        'exploratory_validation_groups': ['C', 'D'],
        'runtime_scope': 'soft preparation lead only; frozen deadline and parent checks mandatory',
        'python': platform.python_version(), 'numpy': np.__version__, 'sklearn': sklearn.__version__}
    pack = json.loads(base.read_text()); pack['preparation'] = preparation
    pack['model_id'] = 'clock-preparation-' + digest[:12]
    predictions = np.maximum(1, raw + correction)
    # Check the exact exported tree representation used by C++.
    exported = []
    for features in x:
        value = preparation['bias_ms'] + correction
        for tree in trees:
            index = 0
            while tree[index]['feature'] >= 0:
                node = tree[index]; index = node['left'] if features[node['feature']] <= node['threshold'] else node['right']
            value += preparation['learning_rate'] * tree[index]['value']
        exported.append(max(1, value))
    if not np.allclose(exported, predictions, rtol=0, atol=1e-9):
        raise AssertionError('Exported predictor differs from training implementation')
    preparation['parity_cases'] = [{'features': x[index].tolist(), 'prepare_us': predictions[index] * 1000}
        for index in np.linspace(0, len(rows) - 1, 12, dtype=int)]
    metrics = {'dataset_sha256': digest, 'manifest': manifest, 'quantile': quantile,
        'counts': {key: int(mask.sum()) for key, mask in masks.items()},
        'split': 'all nodes and adjacent windows of each experiment stay together',
        'scope': 'offline exploratory validation; prospective cloud runs are not training data',
        'time_head': 'unchanged simulation reference; no real clock truth is claimed', 'by_rate': []}
    for rate in [20000, 30000, 40000, 50000, 60000]:
        mask = masks['exploratory_validation'] & np.array([row[1] == rate for row in rows])
        domain = np.all((x[:, FEATURES] >= minimum[FEATURES]) & (x[:, FEATURES] <= maximum[FEATURES]), axis=1)
        metrics['by_rate'].append({'rate': rate, 'rows': int(mask.sum()),
            'domain_valid_fraction': float(domain[mask].mean()),
            'empirical_coverage': float(np.mean(y[mask] <= predictions[mask])),
            'median_prediction_ms': float(np.median(predictions[mask])),
            'median_previous_p95_ms': float(np.median(x[mask, 14]))})
    output.parent.mkdir(parents=True, exist_ok=True); report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(pack, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    metrics['model_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
    report.write_text(json.dumps(metrics, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in metrics.items() if key != 'manifest'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ['raw', 'base', 'output', 'report']:
        parser.add_argument('--' + field, type=Path, required=True)
    args = parser.parse_args(); Train(args.raw, args.base, args.output, args.report)
