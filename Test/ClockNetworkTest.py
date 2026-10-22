"""Local correctness only: A/B/C/D wiring, model fallback, deadlines, and ledger agreement."""
import argparse
import json
from pathlib import Path
import time
from Cluster import Cluster, LocalInventory
from NetworkTest import ExeName, Run, VerifyAccounts


def Wait(predicate, timeout=12):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('Local clock test condition timed out')


def Case(binary, root, control, scheduler, model, dataset, genesis, count, join_current=1, timing=0, risk=0, adaptive=0, value=0):
    root.mkdir()
    nodes = LocalInventory(binary, root/'nodes', 2)
    for node in nodes:
        node['options'] = {'clock-trace': str(root/(node['id']+'.jsonl'))}
    settings = {'produce': 1, 'sync': 1, 'experiment-index': 1, 'clock-sync': 1,
                'clock-control': control, 'round-scheduler': scheduler, 'clock-model': str(model),
                'join-current-round': join_current,
                'candidate-timing': timing, 'candidate-risk': risk,
                'candidate-adaptive': adaptive, 'candidate-value': value, 'candidate-lifecycle': 1,
                'clock-min-peers': 1, 'sync-period-ms': 50, 'clock-model-period-ms': 100,
                'round-ms': 50, 'candidate-ms': 12, 'io-threads': 2, 'verify-threads': 2,
                'run-seconds': 60, 'max-block-txs': 64}
    cluster = Cluster(nodes, 'clock-local', genesis, settings, root)
    try:
        cluster.Start()
        Wait(lambda: all(row['clock']['samples'] > 0 and row['clock']['model_runs'] > 0 for row in cluster.Stats()))
        target = nodes[0]
        Run([binary/ExeName('sender'), '--file', dataset, '--count', count, '--host', target['host'],
             '--port', target['port'], '--batch', 4, '--connections', 1])
        stats = Wait(lambda: (rows if all(row['chain_transactions'] == count and row['pool'] == 0 for row in rows) else None)
                     if (rows := cluster.Stats()) else None)
        assert len({row['head'] for row in stats}) == 1, stats
        assert sum(row['local_proposals'] for row in stats) > 0, stats
        for node, row in zip(nodes, stats):
            VerifyAccounts(node['port'], dataset, count, Path(str(dataset)+'.genesis.json'))
            assert row['clock']['control'] == control and row['clock']['scheduler'] == scheduler
            assert row['clock']['join_current_round'] == bool(join_current)
            assert row['clock']['candidate_timing'] == bool(timing) and row['clock']['candidate_risk'] == bool(risk)
            assert row['clock']['candidate_adaptive'] == bool(adaptive) and row['clock']['candidate_value'] == value
            assert row['canonical_tail'][0] == {'height':row['height'],'hash':row['head'],'transactions':row['chain_transactions']}
            assert abs(row['clock']['slew_ppm']) <= row['clock']['max_slew_ppm']
        if not model.exists():
            assert all(not row['clock']['model_loaded'] and row['clock']['model_fallbacks'] > 0 for row in stats)
        else:
            assert all(row['clock']['model_loaded'] and row['clock']['model_sha256'] for row in stats)
            assert len({row['clock']['model_sha256'] for row in stats}) == 1
        time.sleep(.25)  # Observer flush, not a performance measurement.
        attempts = 0
        for node in nodes:
            trace = [json.loads(line) for line in (root/(node['id']+'.jsonl')).read_text().splitlines()]
            proposals = [row for row in trace if row['kind'] == 'candidate']
            rounds = [row['round'] for row in proposals]
            assert len(rounds) == len(set(rounds)), rounds
            for proposal in proposals:
                if not join_current:
                    assert not proposal['joined_current']
                assert proposal['proposal_deadline_us'] < proposal['collection_deadline_us']
                if proposal['outcome'] == 'offered':
                    assert proposal['start_us'] <= proposal['offered_us'] < proposal['proposal_deadline_us']
            attempts += len(proposals)
        if scheduler == 'bounded':
            assert attempts > 0
        return {'control': control, 'scheduler': scheduler, 'missing_model': not model.exists(),
                'join_current': join_current, 'transactions': count, 'state_checked_nodes': 2, 'bounded_attempts': attempts}
    finally:
        cluster.Close()
        assert not cluster.cleanup_errors, cluster.cleanup_errors


def NoQuorum(binary, root, dataset, genesis, count):
    root.mkdir(); nodes = LocalInventory(binary, root/'nodes', 2)
    settings = {'clock-control': 'robust', 'clock-sync': 1, 'clock-min-peers': 2,
                'round-scheduler': 'bounded', 'round-ms': 50, 'candidate-ms': 12,
                'sync-period-ms': 50, 'clock-model-period-ms': 100,
                'io-threads': 2, 'verify-threads': 2, 'run-seconds': 60}
    cluster = Cluster(nodes, 'no-quorum', genesis, settings, root)
    try:
        cluster.Start()
        Wait(lambda: all(row['clock']['samples'] > 0 and row['clock']['model_runs'] > 0 for row in cluster.Stats()))
        Run([binary/ExeName('sender'), '--file', dataset, '--count', count, '--port', nodes[0]['port'], '--connections', 1])
        stats = Wait(lambda: (rows if rows[0]['pool'] == count and rows[0]['clock']['budget_skips'] > 0 else None)
                     if (rows := cluster.Stats()) else None)
        assert all(row['local_proposals'] == 0 and row['chain_transactions'] == 0 for row in stats)
        assert all(row['clock']['fallback'] == 'insufficient_fresh_sources' for row in stats)
        return {'control': 'robust', 'scheduler': 'bounded', 'quorum_gate': 'no local proposals without sufficient fresh sources'}
    finally:
        cluster.Close()
        assert not cluster.cleanup_errors, cluster.cleanup_errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    binary = args.bin.resolve(); work = args.work.resolve()/str(time.time_ns()); work.mkdir(parents=True)
    dataset = work/'transactions.bin'; count = 80
    Run([binary/ExeName('sender'), '--prepare', dataset, '--count', count, '--wallets', 8])
    genesis = json.loads(Path(str(dataset)+'.genesis.json').read_text())
    model = Path(__file__).resolve().parents[1]/'Time/Models/simulation-v1.json'
    results = []
    for index, (control, pack, join_current) in enumerate([
            ('robust', model, 0), ('robust', model, 1),
            ('learned', model, 0), ('learned', model, 1),
            ('shadow', model, 1), ('learned', work/'missing.json', 1)]):
        results.append(Case(binary, work/str(index), control, 'bounded', pack, dataset, genesis, count, join_current))
    results.append(NoQuorum(binary, work/'no-quorum', dataset, genesis, count))
    preparation = model.parent/'preparation-v2.json'
    for timing, risk in [(1,0),(0,1),(1,1)]:
        results.append(Case(binary, work/f'policy-{timing}-{risk}', 'learned' if timing else 'robust',
                            'bounded', preparation, dataset, genesis, count, 1, timing, risk))
    for adaptive, value in [(1,1),(0,2),(1,2)]:
        results.append(Case(binary, work/f'adaptive-{adaptive}-{value}', 'learned' if adaptive else 'robust',
                            'bounded', preparation, dataset, genesis, count, adaptive=adaptive, value=value))
    print(json.dumps({'checks': results, 'scope': 'local correctness; no TPS comparison'}))
