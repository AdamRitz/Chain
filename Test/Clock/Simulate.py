"""Controlled reference data, not a network performance benchmark or a real-node model."""
import argparse
import collections
import hashlib
import json
import math
from pathlib import Path
import random

FEATURES = ['sources', 'offset_abs_ms', 'offset_mad_ms', 'offset_range_ms', 'rtt_p50_ms', 'rtt_p95_ms',
            'jitter_ms', 'age_max_ms', 'offset_change_ms', 'rejected_ratio', 'cpu_cores', 'pending_tx',
            'pending_block', 'ready_txs', 'prepare_p95_ms', 'commit_p95_ms']
for window in [1, 5, 30]:
    FEATURES += [f'mad_{window}s_ms', f'rtt_{window}s_ms', f'prepare_{window}s_ms',
                 f'commit_{window}s_ms', f'late_{window}s_fraction']
FEATURES += ['sample_rate_hz']


def Quantile(values, q=.95):
    return sorted(values)[math.ceil(q*(len(values)-1))] if values else 0.


def Generate(seed, runs, steps):
    rng = random.Random(seed)
    for run in range(runs):
        history, works, commits = collections.deque(), collections.deque(), collections.deque()
        previous_center, previous_targets = 0., {}
        offset, drift = rng.uniform(-8, 8), rng.uniform(-.05, .05)
        # Entire runs stay in one split, including adjacent windows and fault phases.
        scenario = ['quiet', 'jitter', 'congestion', 'false_time', 'mixed', 'asymmetric'][run % 6]
        for step in range(steps):
            now = step*.5
            busy = max(0., rng.gauss(1.5 if scenario in ['congestion', 'mixed'] else .25, .2))
            jitter = rng.uniform(.1, 1.5) if scenario in ['jitter', 'mixed'] else .04
            target = -offset-drift*now
            targets, rtts, changes = [], [], []
            sources = 6 if step % 23 else 2
            for source in range(sources):
                forward = rng.expovariate(1/max(.01, jitter))+.04
                backward = rng.expovariate(1/max(.01, jitter))+.04
                if scenario == 'asymmetric':
                    forward += 1.5
                measured = target+(forward-backward)/2
                if source == 0 and scenario in ['false_time', 'mixed'] and step % 12 < 6:
                    measured += rng.choice([-1, 1])*rng.uniform(5, 100)
                targets.append(measured); rtts.append(forward+backward)
                changes.append(abs(measured-previous_targets.get(source, measured)))
                previous_targets[source] = measured
            center = Quantile(targets, .5)
            mad = Quantile([abs(value-center) for value in targets], .5)
            # A separate true reference exists in simulation. No chrony/neighbor median labels.
            logical_correction = target*(1-math.exp(-now/4))
            time_error = abs(logical_correction-target)
            prepare = max(.05, rng.gauss(.25+busy*.6, .05+busy*.1))
            commit = max(.1, rng.gauss(.5+busy*4, .1+busy))
            late = prepare+abs(center-logical_correction)+.5 >= 12
            # Extract only past work. The future work label never leaks into current features.
            past_prepare, past_commit = Quantile([w[1] for w in works]), Quantile([w[1] for w in commits])
            history.append((now, mad, Quantile(rtts), past_prepare, past_commit,
                            sum(w[2] for w in works)/len(works) if works else 0.))
            features = [float(sources), abs(center-logical_correction), mad, max(targets)-min(targets),
                        Quantile(rtts, .5), Quantile(rtts), Quantile(changes, .5), rng.uniform(0, 500),
                        abs(center-previous_center), .01 if scenario in ['false_time', 'mixed'] else 0.,
                        busy*4, busy*1000, busy*300, busy*1500, past_prepare, past_commit]
            for window in [1, 5, 30]:
                frames = [row for row in history if now-row[0] <= window]
                features += [Quantile([row[column] for row in frames]) for column in range(1, 5)]
                features += [sum(row[5] for row in frames)/len(frames)]
            features += [sources*4.]
            censored = scenario == 'mixed' and step % 31 == 0
            yield {'schema': 1, 'feature_schema': 'chain-clock-v1-32', 'run': f'sim-{run:04}',
                   'node': f'node-{run % 10}', 'scenario': scenario, 'step': step, 'features': features,
                   'time_error_ms': time_error, 'prepare_ms': prepare, 'prepare_censored': censored,
                   'late': late, 'label_source': 'simulation'}
            previous_center = center
            works.append((now, prepare, late)); commits.append((now, commit, False))
            for queue in [history, works, commits]:
                while queue and (now-queue[0][0] > 30 or len(queue) > 256):
                    queue.popleft()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=20261021)
    parser.add_argument('--runs', type=int, default=96)
    parser.add_argument('--steps', type=int, default=96)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', encoding='utf-8', newline='\n') as output:
        for row in Generate(args.seed, args.runs, args.steps):
            output.write(json.dumps(row, separators=(',', ':'), allow_nan=False)+'\n')
    print(json.dumps({'rows': args.runs*args.steps, 'seed': args.seed,
                      'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest()}))
