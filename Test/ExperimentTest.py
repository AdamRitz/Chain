"""Experiment regression: directory races, impairment scope, canonical indexes and logical rounds."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import struct
import time
from types import SimpleNamespace
from unittest.mock import patch
from Cluster import Cluster, LocalInventory, NodeClient, ReadInventory
from ClusterWorker import CaseDirectory
from Experiments import LoadCase, MakeCases, ValidatePlan
from ForkNetworkTest import MakeBlock
from Netem import NetemCommands, ClearNetem
from NetworkTest import ExeName, Run, StartNode, StopNode, SendFrame, WaitStats, GetStats


def TestConfiguration(work):
    root = work/'directory-race'
    def Create(index):
        path = CaseDirectory({'root': str(root), 'run': 'run'+str(index//3), 'node': 'node'+str(index%3)})
        path.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        list(pool.map(Create, range(90)))
    try:
        CaseDirectory({'root': str(root), 'run': '../outside', 'node': 'node'})
        raise AssertionError('Traversal accepted')
    except ValueError:
        pass
    device, preferences, commands = NetemCommands('eth0', 8089, [{'host': '10.0.0.2', 'port': 8090}],
                                                 {'delay_ms': 20, 'jitter_ms': 5, 'loss_percent': .1}, 'case')
    filters = [command for command in commands if command[:3] == ['tc', 'filter', 'add']]
    assert len(filters) == 2 and len(set(preferences)) == 2
    assert all('10.0.0.2' in command and 'tcp' in command and 'ingress' in command for command in filters)
    assert any('src_port' in command and '8090' in command for command in filters)
    assert any('dst_port' in command and '8089' in command for command in filters)
    assert all(command[command.index('dev')+1] == device for command in commands if command[:3] == ['tc', 'qdisc', 'add'])
    for settings in [{'loss_percent': 101}, {'jitter_ms': 5}, {'queue_packets': 0}]:
        try:
            NetemCommands('eth0', 8089, [{'host': '10.0.0.2', 'port': 8090}], settings, 'case')
            raise AssertionError('Invalid impairment accepted')
        except ValueError:
            pass
    lease = work/'netem-lease.json'
    lease.write_text(json.dumps({'interface': 'eth0', 'device': device, 'sudo': False,
                                 'installed_filters': preferences, 'device_created': True}))
    commands_run = []
    def FailedCleanup(command, sudo=False, check=True):
        commands_run.append(command)
        failed = 'pref' in command and command[command.index('pref')+1] == str(preferences[-1])
        return SimpleNamespace(returncode=int(failed), stderr='test cleanup failure' if failed else '')
    with patch('Netem.Execute', side_effect=FailedCleanup):
        try:
            ClearNetem(lease)
            raise AssertionError('Incomplete cleanup lost its lease')
        except RuntimeError:
            pass
    saved = json.loads(lease.read_text())
    assert saved['installed_filters'] == [preferences[-1]] and saved['device_created']
    assert not any(command[:3] == ['ip', 'link', 'del'] for command in commands_run)
    with patch('Netem.Execute', return_value=SimpleNamespace(returncode=0, stderr='')):
        assert ClearNetem(lease)['cleared'] and not lease.exists()
    inventory = work/'inventory.json'
    inventory.write_text(json.dumps({'nodes': [{'id': f'n{i}', 'host': '127.0.0.1', 'port': 9000+i,
                                                'transport': 'local'} for i in range(20)]}))
    _, nodes = ReadInventory(inventory)
    assert len(nodes) == 20
    nodes[1]['port'] = nodes[0]['port']
    inventory.write_text(json.dumps({'nodes': nodes}))
    try:
        ReadInventory(inventory)
        raise AssertionError('Duplicate endpoint accepted')
    except ValueError:
        pass


def TestIndex(binary, work):
    work.mkdir()
    dataset = work/'transactions.bin'
    Run([binary/ExeName('sender'), '--prepare', dataset, '--count', 16, '--wallets', 16])
    raw = dataset.read_bytes()
    txs = [raw[i:i+176] for i in range(0, len(raw), 176)]
    node = None
    client = None
    try:
        node = StartNode(binary, work, 'index', produce=0, **{'experiment-index': 1})
        client = NodeClient({'host': '127.0.0.1', 'port': node['port']})
        genesis = bytes.fromhex(GetStats(node['port'])['head'])
        first = MakeBlock(genesis, 2, [txs[0]])
        other = MakeBlock(genesis, 2, [txs[1], txs[2]])
        SendFrame(node['port'], 7, first)
        WaitStats(node['port'], lambda s: s['head'] == first[80:112].hex())
        assert client.Query(31, txs[0][80:112])[0]['height'] == 2
        SendFrame(node['port'], 7, other)
        WaitStats(node['port'], lambda s: s['head'] == other[80:112].hex())
        assert client.Query(31, txs[0][80:112]+txs[1][80:112]) == [None, {'height': 2, 'block': other[80:112].hex()}]
        extended = MakeBlock(first[80:112], 3, txs[3:6])
        SendFrame(node['port'], 7, extended)
        WaitStats(node['port'], lambda s: s['head'] == extended[80:112].hex())
        before = client.Query(35)
        assert client.Query(31, txs[0][80:112]+txs[1][80:112]) == [{'height': 2, 'block': first[80:112].hex()}, None]
        assert client.Query(29, struct.pack('<Q', 3))['transactions'] == 4
        client.Close(); StopNode(node); node = None
        node = StartNode(binary, work, 'index', produce=0, **{'experiment-index': 1})
        client = NodeClient({'host': '127.0.0.1', 'port': node['port']})
        assert client.Query(35) == before
        assert client.Query(31, txs[0][80:112])[0]['block'] == first[80:112].hex()
    finally:
        if client:
            client.Close()
        if node:
            StopNode(node)


def TestRounds(binary, work):
    work.mkdir()
    plan = json.loads((Path(__file__).resolve().parents[1]/'Experiments/smoke.json').read_text())
    ValidatePlan(plan)
    case = {'suite': 'throughput', 'rate': 100, 'mode': 'latency', 'nodes': 2}
    plan.update({'warmup': 1, 'duration': 2, 'drain_seconds': 10, 'batch': 8})
    dataset = work/'transactions.bin'
    Run([binary/ExeName('sender'), '--prepare', dataset, '--count', 300, '--wallets', plan['wallets']])
    genesis = json.loads(Path(str(dataset)+'.genesis.json').read_text())
    nodes = LocalInventory(binary, work/'nodes', 2)
    cluster = Cluster(nodes, 'rounds', genesis, {'produce': 1, 'sync': 1, 'experiment-index': 1,
                      'clock-sync': 1, 'round-ms': 50, 'candidate-ms': 10, 'sync-period-ms': 50,
                      'offsets_ms': [-40, 40], 'io-threads': 2, 'verify-threads': 2, 'run-seconds': 60}, work)
    try:
        cluster.Start()
        time.sleep(1)
        result = LoadCase(cluster, case, plan, dataset, genesis, work)
        assert result['drained'] and result['sent_unique'] == 300 and result['unresolved_samples'] == 0, result
        assert all(stats['clock']['samples'] > 0 and stats['local_proposals'] > 0 for stats in cluster.Stats())
    finally:
        cluster.Close()
    assert not cluster.cleanup_errors, cluster.cleanup_errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    directory = args.work.resolve()/str(time.time_ns()); directory.mkdir(parents=True)
    TestConfiguration(directory)
    TestIndex(args.bin.resolve(), directory/'index')
    TestRounds(args.bin.resolve(), directory/'rounds')
    print(json.dumps({'checks': ['parallel directories', 'network impairment scope/cleanup retry', 'dynamic inventory',
                                 'canonical transaction index rollback/restart', 'synchronized round execution']}))
