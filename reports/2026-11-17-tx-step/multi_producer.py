"""Three concurrent producers: transaction-step correctness, not a TPS benchmark."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import sys
import threading
import time


def RunCase(binary, work, step):
    work.mkdir(parents=True, exist_ok=False)
    dataset = work/'transactions.bin'
    count = 2*step+7
    Run([binary/ExeName('sender'), '--prepare', dataset, '--count', count, '--wallets', 8])
    raw = dataset.read_bytes()
    nodes = []
    result = {'kind': 'multi_producer_correctness', 'step': step, 'input_transactions': count,
              'round_ms': 300, 'candidate_ms': 40, 'nodes': 3, 'passed': False}
    started = None
    try:
        for index in range(3):
            config = Configuration(work, f'node-{index}', {
                'block_tx_step': step,
                'peers': [f"127.0.0.1:{node['port']}" for node in nodes],
            })
            node = StartNode(binary, work, f'producer-{index}', config=config, produce=1,
                             **{'max-block-txs': 10000, 'round-ms': 300, 'candidate-ms': 40,
                                'block-ms': 200, 'experiment-index': 1, 'clock-sync': 1,
                                'sync-period-ms': 50, 'run-seconds': 90})
            nodes.append(node)
        for node in nodes:
            WaitStats(node['port'], lambda row: row['peers'] >= 2, timeout=10)
        RoundStart(nodes[0]['port'], 300)
        barrier = threading.Barrier(len(nodes))

        def Broadcast(node):
            barrier.wait(timeout=5)
            begin = time.monotonic()
            SendFrame(node['port'], 10, raw)
            return {'port': node['port'], 'send_seconds': time.monotonic()-begin}

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as pool:
            futures = [pool.submit(Broadcast, node) for node in nodes]
            result['broadcast'] = [future.result() for future in futures]

        deadline = started+15
        stable_head, stable_since, rows = None, None, []
        while time.monotonic() < deadline:
            rows = [GetStats(node['port']) for node in nodes]
            assert not any(row['failed'] for row in rows), rows
            common = len({row['head'] for row in rows}) == 1
            drained = all(row['chain_transactions'] == count and row['pool'] == 0 and
                          row['pending_tx_verify'] == 0 and row['pending_block_verify'] == 0
                          for row in rows)
            if common and drained:
                head = rows[0]['head']
                if stable_head != head:
                    stable_head, stable_since = head, time.monotonic()
                # Observe the same complete canonical chain for two additional rounds.
                if time.monotonic()-stable_since >= .65:
                    break
            else:
                stable_head, stable_since = None, None
            time.sleep(.02)
        else:
            raise TimeoutError(f'Producers did not converge and drain: {rows}')

        result['drain_and_stability_seconds'] = time.monotonic()-started
        counts = [BlockCounts(node['port'], row['height']) for node, row in zip(nodes, rows)]
        assert counts[0] == counts[1] == counts[2], counts
        assert sum(counts[0]) == count, counts
        assert all(n > 0 and (n % step == 0 or n < step) for n in counts[0]), counts
        # At this load, the feature must exercise at least one full step and one deadline tail.
        assert any(n >= step for n in counts[0]) and any(n < step for n in counts[0]), counts

        clients = [NodeClient({'host': '127.0.0.1', 'port': node['port']}) for node in nodes]
        try:
            states = [client.Query(35) for client in clients]
        finally:
            for client in clients:
                client.Close()
        assert states[0] == states[1] == states[2], states
        result['account_checks'] = [VerifyAccounts(node['port'], dataset, count,
                                                    str(dataset)+'.genesis.json') for node in nodes]
        result.update({'passed': True, 'canonical_block_counts': counts[0], 'state': states[0],
                       'node_stats': rows,
                       'observations': [{key: row[key] for key in (
                           'local_proposals', 'block_step_proposals', 'block_tail_proposals',
                           'reorganizations', 'max_reorg_depth', 'chain_transactions', 'head')}
                                        for row in rows]})
        return result
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        if started is not None:
            result['elapsed_seconds'] = time.monotonic()-started
        snapshots = []
        for node in nodes:
            try:
                snapshots.append(GetStats(node['port']))
            except Exception as snapshot_error:
                snapshots.append({'snapshot_error': str(snapshot_error)})
        result['node_stats'] = snapshots
        raise
    finally:
        cleanup_errors = []
        for node in reversed(nodes):
            try:
                StopNode(node)
            except Exception as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            result['cleanup_errors'] = cleanup_errors
            result['passed'] = False
        (work/'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        if cleanup_errors:
            raise RuntimeError('Test-node cleanup failed: '+'; '.join(cleanup_errors))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    source, binary = args.source.resolve(), args.bin.resolve()
    sys.path.insert(0, str(source/'Test'))
    from Cluster import NodeClient
    from NetworkTest import ExeName, GetStats, Run, SendFrame, StartNode, StopNode, VerifyAccounts, WaitStats
    from TxStepNetworkTest import BlockCounts, Configuration, RoundStart

    work = args.work.resolve()/str(time.time_ns())
    work.mkdir(parents=True, exist_ok=False)
    report = {'kind': 'multi_producer_correctness', 'source': str(source), 'binary': str(binary),
              'work': str(work), 'cases': []}
    try:
        for step in (500, 1000, 2000):
            case = RunCase(binary, work/f'step-{step}', step)
            report['cases'].append(case)
            print(json.dumps({'step': step, 'passed': case['passed'],
                              'block_counts': case['canonical_block_counts'],
                              'seconds': case['drain_and_stability_seconds']}), flush=True)
        report['passed'] = True
    except BaseException as error:
        report.update({'passed': False, 'error': f'{type(error).__name__}: {error}'})
        raise
    finally:
        (work/'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'cases': len(report['cases']), 'report': str(work/'result.json')}))
