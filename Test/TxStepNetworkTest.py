"""Transaction-count steps and deadline tails through real node processes and public queries."""
import argparse
import json
from pathlib import Path
import socket
import struct
import subprocess
import time

from Cluster import NodeClient
from ForkNetworkTest import MakeBlock
from NetworkTest import (FLAGS, ExeName, Frame, GetStats, Run, SendFrame, StartNode,
                         StopNode, VerifyAccounts, WaitStats)


def Configuration(work, name, values=None):
    path = work/(name+'.yaml')
    # Always pass an explicit configuration: never inherit a developer's ../config.yaml.
    path.write_text('node: '+json.dumps(values or {})+'\n', encoding='utf-8')
    return path


def TestConfiguration(binary, work):
    work.mkdir()
    cases = [({}, {}, 0), ({'block_tx_step': 500}, {}, 500),
             ({'block_tx_step': 500}, {'block-tx-step': 1000}, 1000),
             ({'block_tx_step': 2000}, {}, 2000), ({}, {'block-tx-step': 1}, 1)]
    for index, (yaml, cli, expected) in enumerate(cases):
        config = Configuration(work, f'valid-{index}', yaml)
        node = StartNode(binary, work, f'valid-{index}', config=config, produce=0,
                         **{'max-block-txs': 10000, **cli})
        try:
            assert GetStats(node['port'])['block_tx_step'] == expected
        finally:
            StopNode(node)
    for source in ('cli', 'yaml'):
        for index, value in enumerate(('-1', '10001', '500x', '1.5')):
            name = f'invalid-{source}-{index}'
            config = Configuration(work, name, {'block_tx_step': value} if source == 'yaml' else {})
            command = [binary/ExeName('boost'), '--config', config, '--data', work/(name+'-db'),
                       '--produce', '0', '--run-seconds', '1']
            if source == 'cli':
                command += ['--block-tx-step', value]
            result = subprocess.run([str(item) for item in command], cwd=work, capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=10,
                                    creationflags=FLAGS)
            assert result.returncode != 0, (source, value, result.stdout, result.stderr)
            assert not (work/(name+'-db')).exists(), 'Invalid step must fail before database creation'


def RoundStart(port, round_ms):
    if not round_ms:
        return
    end = time.monotonic()+2
    while time.monotonic() < end:
        phase = GetStats(port)['clock']['logical_us'] % (round_ms*1000)
        if 10000 <= phase <= 40000:
            return
        time.sleep(.004)
    raise TimeoutError('Could not align low-load input to the start of a logical round')


def BlockCounts(port, height):
    client = NodeClient({'host': '127.0.0.1', 'port': port})
    try:
        scores = [client.Query(29, struct.pack('<Q', h))['transactions'] for h in range(1, height+1)]
        return [right-left for left, right in zip(scores, scores[1:])]
    finally:
        client.Close()


def AssertReplica(producer, follower):
    expected = GetStats(producer['port'])
    actual = WaitStats(follower['port'], lambda row: row['head'] == expected['head'], timeout=5)
    assert actual['committed'] == expected['committed']
    clients = [NodeClient({'host': '127.0.0.1', 'port': node['port']}) for node in (producer, follower)]
    try:
        assert clients[0].Query(35) == clients[1].Query(35), 'Independent native/EVM state digests must agree'
    finally:
        for client in clients:
            client.Close()


def TestNative(binary, work, step, round_ms):
    work.mkdir()
    total = step+10
    dataset = work/'transactions.bin'
    Run([binary/ExeName('sender'), '--prepare', dataset, '--count', total, '--wallets', 2])
    raw = dataset.read_bytes()
    config = Configuration(work, 'node', {'block_tx_step': step})
    settings = {'config': config, 'max-block-txs': 10000, 'round-ms': round_ms,
                'candidate-ms': 40 if round_ms else 0, 'block-ms': 200,
                'experiment-index': 1}
    nodes = []
    try:
        producer = StartNode(binary, work, 'producer', **settings)
        nodes.append(producer)
        follower = StartNode(binary, work, 'follower', produce=0,
                             seed=f"127.0.0.1:{producer['port']}", **settings)
        nodes.append(follower)
        port = producer['port']
        RoundStart(port, round_ms)
        started = time.monotonic()
        SendFrame(port, 10, raw[:3*176])
        WaitStats(port, lambda row: row['ready_pool'] == 3, timeout=1)
        time.sleep(.05)
        assert GetStats(port)['committed'] == 0, 'Sub-step input must wait for the deadline'
        low = WaitStats(port, lambda row: row['committed'] == 3, timeout=1.25)
        low_seconds = time.monotonic()-started
        assert low_seconds < 1.5, ('Low load must not wait for future transactions', low_seconds)
        assert low['block_tail_proposals'] == 1 and low['block_step_proposals'] == 0

        # Stop all new input at K+7: both the full step and its remainder must eventually commit.
        SendFrame(port, 10, raw[3*176:])
        final = WaitStats(port, lambda row: row['committed'] == total and row['pool'] == 0, timeout=3)
        counts = BlockCounts(port, final['height'])
        assert counts == [3, step, 7], (step, round_ms, counts)
        assert final['block_step_proposals'] == 1 and final['block_tail_proposals'] == 2
        AssertReplica(producer, follower)
        for node in nodes:
            VerifyAccounts(node['port'], dataset, total, str(dataset)+'.genesis.json')
        return {'step': step, 'round_ms': round_ms, 'block_counts': counts,
                'low_load_seconds': low_seconds, 'head': final['head']}
    finally:
        for node in reversed(nodes):
            StopNode(node)


def TestBudgetDeadline(binary, work, budget, round_ms):
    work.mkdir()
    dataset = work/'contracts.bin'
    if budget == 'gas':
        step, count, gas, payload = 4, 5, 10000000, ''
    else:
        # 8192 input bytes keep the command below Windows' command-line limit.
        # The encoded 256-transaction block exceeds 2 MiB while gas stays below 30 M.
        step, count, gas, payload = 256, 256, 100000, '00'*8192
    Run([binary/ExeName('evm_sender'), '--prepare', dataset, '--count', count, '--wallets', 1,
         '--call', '00'*19+'77', '--input', payload, '--gas', gas])
    config = Configuration(work, 'node', {'block_tx_step': step})
    settings = {'config': config, 'genesis': str(dataset)+'.genesis.json', 'max-block-txs': 10000,
                'round-ms': round_ms, 'candidate-ms': 40 if round_ms else 0,
                'block-ms': 200, 'experiment-index': 1}
    nodes = []
    try:
        producer = StartNode(binary, work, 'producer', **settings)
        nodes.append(producer)
        follower = StartNode(binary, work, 'follower', produce=0,
                             seed=f"127.0.0.1:{producer['port']}", **settings)
        nodes.append(follower)
        port = producer['port']
        RoundStart(port, round_ms)
        started = time.monotonic()
        # Prepare signed inputs in advance and write all frames on one connection, preserving nonce order.
        raw, offset, frames = dataset.read_bytes(), 0, []
        while offset < len(raw):
            length = struct.unpack_from('<I', raw, offset)[0]
            offset += 4
            frames.append(Frame(17, raw[offset:offset+length]))
            offset += length
        assert len(frames) == count
        with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
            connection.sendall(b'\x02'+b''.join(frames))
        final = WaitStats(port, lambda row: row['committed'] == count and row['pool'] == 0, timeout=4)
        elapsed = time.monotonic()-started
        counts = BlockCounts(port, final['height'])
        assert len(counts) >= 2 and sum(counts) == count and all(0 < n < step for n in counts), (budget, counts)
        assert final['block_step_proposals'] == 0 and final['block_tail_proposals'] == len(counts)
        assert elapsed < 4, ('Ready count exceeding K must not spin or prevent deadline tails', budget, elapsed)
        AssertReplica(producer, follower)
        return {'budget': budget, 'round_ms': round_ms, 'block_counts': counts, 'elapsed_seconds': elapsed}
    finally:
        for node in reversed(nodes):
            StopNode(node)


def TestShutdown(binary, work, round_ms, received_candidate=False):
    """Graceful shutdown must drain a waiting tail and retain an already verified remote candidate."""
    work.mkdir()
    dataset = work/'transactions.bin'
    Run([binary/ExeName('sender'), '--prepare', dataset, '--count', 3, '--wallets', 2])
    raw = dataset.read_bytes()
    config = Configuration(work, 'node', {'block_tx_step': 500})
    stop_file = work/'stop.request'
    settings = {'config': config, 'max-block-txs': 10000, 'round-ms': round_ms,
                'candidate-ms': 10 if round_ms else 0, 'block-ms': 1000,
                'stop-file': stop_file}
    nodes = []
    try:
        producer = StartNode(binary, work, 'producer', **settings)
        nodes.append(producer)
        port = producer['port']
        RoundStart(port, round_ms)
        if received_candidate:
            parent = bytes.fromhex(GetStats(port)['head'])
            block = MakeBlock(parent, 2, [raw[i:i+176] for i in range(0, len(raw), 176)])
            SendFrame(port, 7, block)
            pending = WaitStats(port, lambda row: row['candidate_executions'] > 0 and
                                row['pending_block_verify'] == 0 and row['block_step_wait_ms'] > 0,
                                timeout=1)
            assert pending['pool'] == 0, 'Remote candidate must exercise the empty local generation path'
        else:
            SendFrame(port, 10, raw)
            pending = WaitStats(port, lambda row: row['ready_pool'] == 3, timeout=1)
        assert pending['committed'] == 0, 'Shutdown scenario must begin before the scheduled commit'
        stop_file.write_text('stop\n', encoding='utf-8')
        assert producer['process'].wait(timeout=5) == 0, 'Graceful shutdown failed'
        final = json.loads((producer['directory']/'final.json').read_text(encoding='utf-8'))
        assert not final['failed'] and final['committed'] == 3 and final['pool'] == 0, final
        if received_candidate:
            assert final['head'] == block[80:112].hex(), 'Verified remote candidate lost during shutdown'
        else:
            assert final['block_tail_proposals'] == 1, 'Shutdown must flush the incomplete local step'
        StopNode(producer)
        nodes.remove(producer)
        # Read persisted state in a fresh process; final metrics alone do not prove durable commit.
        restarted = StartNode(binary, work, 'producer', config=config, produce=0,
                              **{'max-block-txs': 10000})
        nodes.append(restarted)
        assert GetStats(restarted['port'])['head'] == final['head']
        VerifyAccounts(restarted['port'], dataset, 3, str(dataset)+'.genesis.json')
        return {'shutdown_round_ms': round_ms, 'received_candidate': received_candidate,
                'committed': 3, 'head': final['head']}
    finally:
        for node in reversed(nodes):
            StopNode(node)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    binary = args.bin.resolve()
    work = args.work.resolve()/str(time.time_ns())
    work.mkdir(parents=True)
    TestConfiguration(binary, work/'configuration')
    results = []
    for round_ms in (0, 300):
        for step in (500, 1000, 2000):
            results.append(TestNative(binary, work/f'native-{step}-{round_ms}', step, round_ms))
        for budget in ('gas', 'bytes'):
            results.append(TestBudgetDeadline(binary, work/f'{budget}-{round_ms}', budget, round_ms))
        results.append(TestShutdown(binary, work/f'shutdown-tail-{round_ms}', round_ms))
    results.append(TestShutdown(binary, work/'shutdown-candidate', 300, received_candidate=True))
    report = {'configuration': 'CLI, YAML, override and invalid values passed', 'cases': results}
    (work/'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
