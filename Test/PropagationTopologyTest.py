"""Eight real nodes on a fixed graph, including a legacy compact-block peer."""
import argparse
import json
from pathlib import Path
import shutil
import socket
import struct

from NetworkTest import (ExeName, Frame, GetStats, ReadAll, Run, SendFrame,
                         StartNode, StopNode, VerifyAccounts, WaitStats)


def State(port):
    with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
        connection.sendall(b'\x02' + Frame(35))
        kind, length = struct.unpack('<BI', ReadAll(connection, 5))
        assert kind == 36 and length <= 16384
        return json.loads(ReadAll(connection, length))


def WriteSeeds(path, values):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(values)+'\n', encoding='utf-8')
    temporary.replace(path)


def Cleanup(nodes, work):
    errors, stopped, removed = [], [], []
    for node in reversed(nodes):
        try:
            StopNode(node)
            assert node['process'].poll() is not None, 'Node process is still running'
            stopped.append(node['process'].pid)
        except Exception as error:
            errors.append(f"Stop {node['directory'].name}: {error}")
    for node in nodes:
        if node['process'].poll() is None:
            continue
        directory = node['directory'].resolve()
        database = (directory/'db').resolve()
        try:
            assert directory.is_relative_to(work) and database.parent == directory
            assert database.name == 'db', 'Unexpected database path'
            if database.exists():
                shutil.rmtree(database)
                removed.append(str(database))
        except Exception as error:
            errors.append(f"Clean {directory.name}: {error}")
    return {'stopped_processes': stopped, 'removed_databases': removed, 'errors': errors}


def TestTopology(binary, work):
    work.mkdir(parents=True, exist_ok=False)
    dataset, config = work/'transactions.bin', work/'node.yaml'
    Run([binary/ExeName('sender'), '--prepare', dataset, '--count', 240, '--wallets', 240])
    data = dataset.read_bytes()
    assert len(data) == 240*176
    config.write_text('node: {}\n', encoding='utf-8')
    edges = sorted({tuple(sorted((i, (i+offset) % 8))) for i in range(8) for offset in (1, 3)})
    neighbors = [{right if left == i else left for left, right in edges if i in (left, right)} for i in range(8)]
    assert len(edges) == 16 and all(len(values) == 4 for values in neighbors)
    nodes, seeds = [], []
    report = {'nodes': 8, 'degree': 4, 'edges': edges, 'legacy_node': 7, 'status': 'failed'}
    failed = False
    try:
        for i in range(8):
            path = work/f'seeds-{i}.json'
            WriteSeeds(path, [])
            seeds.append(path)
            nodes.append(StartNode(binary, work, f'node-{i}', **{
                'config': config, 'seeds-file': path, 'peer-discovery': 0,
                'produce': int(i == 0), 'relay-transactions': 1,
                'short-ids': int(i != 7), 'block-relay-peers': 0, 'tx-relay-peers': 0,
                'max-peers': 4, 'max-connections': 24, 'max-block-txs': 6000,
                'block-ms': 100, 'io-threads': 1, 'verify-threads': 1,
                'candidate-threads': 1, 'block-verify-threads': 1,
                'compact-repair-ms': 100, 'run-seconds': 110,
            }))
        for i in range(8):
            WriteSeeds(seeds[i], [f"127.0.0.1:{nodes[j]['port']}" for j in sorted(neighbors[i]) if j > i])
        for i, node in enumerate(nodes):
            expected = len(neighbors[i]-{7}) if i != 7 else 0
            WaitStats(node['port'], lambda row, expected=expected:
                      row['peers'] == 4 and row['short_compact_peers'] == expected, timeout=20)

        # The producer has four neighbors. Some transactions enter through its
        # other three nodes, so both transaction and block forwarding are used.
        entries = (1, 2, 4, 6)
        for part, index in enumerate(entries):
            SendFrame(nodes[index]['port'], 10, data[part*60*176:(part+1)*60*176])
        producer = WaitStats(nodes[0]['port'], lambda row: row['chain_transactions'] == 240, timeout=30)
        head = producer['head']
        final = [WaitStats(node['port'], lambda row: row['chain_transactions'] == 240 and row['head'] == head and row['pending'] == 0,
                           timeout=20) for node in nodes]
        states = [State(node['port']) for node in nodes]
        assert all(state == states[0] for state in states), 'Chain heads or account state hashes differ'
        VerifyAccounts(nodes[0]['port'], dataset, 240, str(dataset)+'.genesis.json')
        assert all(row['peers'] == 4 and row['valid'] <= 240 for row in final)
        assert final[7]['relay_protocol'] == 1 and final[7]['compact_short_announcements'] == 0
        indirect = sorted(set(range(1, 8))-neighbors[0])
        assert any(final[i]['compact_reconstructed'] > 0 for i in indirect), 'No compact block crossed two connections'
        assert sum(row['compact_short_announcements'] for row in final) > 0

        before = [row['relayed_transactions'] for row in final]
        for node in nodes:
            skipped = GetStats(node['port'])['skipped_relay_transactions']
            SendFrame(node['port'], 10, data[:60*176])
            WaitStats(node['port'], lambda row, skipped=skipped: row['skipped_relay_transactions'] >= skipped+60)
        final = [GetStats(node['port']) for node in nodes]
        assert [row['relayed_transactions'] for row in final] == before, 'Confirmed duplicate transactions were forwarded again'
        assert all(row['head'] == head and row['chain_transactions'] == 240 and row['peers'] == 4 for row in final)
        report.update(status='passed', transactions=240, head=head, state=states[0],
                      producer_non_neighbors=indirect, metrics=final,
                      checks=['fixed degree and one TCP connection per edge',
                              'transactions reach one producer through multiple entry nodes',
                              'short IDs and legacy compact blocks coexist',
                              'compact blocks cross at least two connections',
                              'all chain heads and account states match',
                              'confirmed duplicate transactions are not forwarded'])
    except BaseException as error:
        failed = True
        report['error'] = str(error)
        raise
    finally:
        report['cleanup'] = Cleanup(nodes, work)
        if report['cleanup']['errors']:
            report['status'] = 'failed'
        (work/'result.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        if report['cleanup']['errors'] and not failed:
            raise RuntimeError('; '.join(report['cleanup']['errors']))
    print(json.dumps({key: report[key] for key in ('status', 'nodes', 'degree', 'transactions', 'head', 'checks')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    TestTopology(args.bin.resolve(), args.work.resolve())
