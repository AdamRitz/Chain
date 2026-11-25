"""Propagation, compact repair and legacy protocol regression using real nodes."""
import argparse
import json
from pathlib import Path
import socket
import struct
import time

from ForkNetworkTest import MakeBlock
from NetworkTest import (ExeName, Frame, GetStats, ReadAll, Run, SendFrame,
                         StartNode, StopNode, VerifyAccounts, WaitStats)


class RelayPeer:
    def __init__(self, port, version=1):
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=3)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            announced_port = probe.getsockname()[1]
        self.version = version
        self.socket.sendall(b'\x03' + struct.pack('<H', announced_port))
        self.Send(14, json.dumps({'relay_protocol': version}).encode())
        self.Read({13})

    def Send(self, kind, data=b''):
        self.socket.sendall(Frame(kind, data))

    def Read(self, kinds, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.socket.settimeout(max(.01, deadline-time.monotonic()))
            kind, size = struct.unpack('<BI', ReadAll(self.socket, 5))
            assert size <= 2*1024*1024
            body = ReadAll(self.socket, size)
            if kind == 13:
                self.Send(14, json.dumps({'relay_protocol': self.version}).encode())
            if kind in kinds:
                return kind, body
        raise TimeoutError(f'No expected frame {kinds}')

    def Close(self):
        self.socket.close()


def Prepare(binary, work, count=16):
    work.mkdir(parents=True, exist_ok=False)
    dataset = work/'transactions.bin'
    Run([binary/ExeName('sender'), '--prepare', dataset, '--count', count,
         '--wallets', count])
    raw = dataset.read_bytes()
    config = work/'node.yaml'
    config.write_text('node: {}\n', encoding='utf-8')
    return dataset, [raw[i:i+176] for i in range(0, len(raw), 176)], config


def Settings(config, **values):
    return {'config': config, 'produce': 0, 'peer-discovery': 0,
            'io-threads': 1, 'verify-threads': 1, 'candidate-threads': 1,
            'block-verify-threads': 1, 'compact-repair-ms': 100, **values}


def FilterBatch(binary, work):
    dataset, txs, config = Prepare(binary, work)
    node = StartNode(binary, work, 'node', **Settings(config))
    peer = RelayPeer(node['port'])
    try:
        SendFrame(node['port'], 10, txs[0])
        assert peer.Read({10})[1] == txs[0]
        invalid = bytearray(txs[2]); invalid[-1] ^= 1
        SendFrame(node['port'], 10, txs[0]+txs[1]+invalid)
        assert peer.Read({10})[1] == txs[1], 'Only newly accepted transactions may be relayed'
        stats = WaitStats(node['port'], lambda s: s['valid'] == 2)
        assert stats['relayed_transactions'] == 2
        assert stats['skipped_relay_transactions'] == 2
        return {'relayed': 2, 'duplicates_or_invalid_skipped': 2}
    finally:
        peer.Close(); StopNode(node)


def RelayBeforeCommit(binary, work):
    dataset, txs, config = Prepare(binary, work)
    node = StartNode(binary, work, 'node', **Settings(config, **{
        'round-ms': 1000, 'candidate-ms': 800}))
    peer = RelayPeer(node['port'])
    try:
        genesis = bytes.fromhex(GetStats(node['port'])['head'])
        block = MakeBlock(genesis, 2, txs[:2])
        started = time.monotonic()
        SendFrame(node['port'], 7, block)
        kind, compact = peer.Read({39}, timeout=2)
        assert compact[:112] == block[:112]
        before = WaitStats(node['port'], lambda s: s['early_relayed_blocks'] >= 1)
        assert before['height'] == 1, 'Validated block must be relayed while commit is waiting'
        assert before['early_relayed_blocks'] == 1
        elapsed = time.monotonic()-started
        WaitStats(node['port'], lambda s: s['height'] == 2, timeout=4)
        VerifyAccounts(node['port'], dataset, 2, str(dataset)+'.genesis.json')
        return {'relay_seconds': elapsed, 'height_when_relayed': before['height']}
    finally:
        peer.Close(); StopNode(node)


def CompactReplication(binary, work, short_ids):
    dataset, txs, config = Prepare(binary, work)
    nodes = []
    try:
        source = StartNode(binary, work, 'source', **Settings(config))
        nodes.append(source)
        target = StartNode(binary, work, 'target', **Settings(config, **{
            'seed': f"127.0.0.1:{source['port']}", 'short-ids': short_ids}))
        nodes.append(target)
        WaitStats(source['port'], lambda s: s['peers'] >= 1)
        if short_ids:
            WaitStats(source['port'], lambda s: s['short_compact_peers'] == 1)
            WaitStats(target['port'], lambda s: s['short_compact_peers'] == 1)
        else:
            # Complete the legacy stats response before publishing the block.
            WaitStats(target['port'], lambda s: s['peers'] >= 1)
            assert GetStats(target['port'])['relay_protocol'] == 1
        genesis = bytes.fromhex(GetStats(source['port'])['head'])
        block = MakeBlock(genesis, 2, txs)
        SendFrame(source['port'], 7, block)
        expected = block[80:112].hex()
        left = WaitStats(source['port'], lambda s: s['head'] == expected)
        right = WaitStats(target['port'], lambda s: s['head'] == expected)
        assert right['compact_reconstructed'] >= 1
        assert right['missing_transactions'] >= len(txs)
        if short_ids:
            assert left['compact_short_announcements'] >= 1
        else:
            assert left['compact_short_announcements'] == 0
        for node in nodes:
            VerifyAccounts(node['port'], dataset, len(txs), str(dataset)+'.genesis.json')
        return {'short_ids': short_ids, 'head': expected,
                'missing_transactions': right['missing_transactions']}
    finally:
        for node in reversed(nodes):
            StopNode(node)


def AlternateRepair(binary, work):
    dataset, txs, config = Prepare(binary, work)
    node = StartNode(binary, work, 'node', **Settings(config))
    first = RelayPeer(node['port']); alternate = RelayPeer(node['port'])
    try:
        genesis = bytes.fromhex(GetStats(node['port'])['head'])
        block = MakeBlock(genesis, 2, txs[:3])
        compact = block[:112] + b''.join(tx[80:112]+b'\0' for tx in txs[:3])
        first.Send(39, compact)
        kind, request = first.Read({40})
        assert request[:32] == block[80:112]
        alternate.Send(39, compact)
        # The first peer stays connected but deliberately does not answer.
        kind, request = alternate.Read({40, 21}, timeout=3)
        assert request[:32] == block[80:112]
        if kind == 40:
            response = request[:32]
            for offset in range(32, len(request), 4):
                index = struct.unpack_from('<I', request, offset)[0]
                response += struct.pack('<II', index, 176)+txs[index]
            alternate.Send(41, response)
        else:
            alternate.Send(22, block)
        stats = WaitStats(node['port'], lambda s: s['head'] == block[80:112].hex())
        assert stats['compact_repair_retries']+stats['compact_full_fallbacks'] >= 1
        VerifyAccounts(node['port'], dataset, 3, str(dataset)+'.genesis.json')
        return {'repair_response_type': kind, 'head': stats['head']}
    finally:
        first.Close(); alternate.Close(); StopNode(node)


def Main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    binary, work = args.bin.resolve(), args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    report = {'filtered_batch': FilterBatch(binary, work/'batch'),
              'early_relay': RelayBeforeCommit(binary, work/'early'),
              'short_ids': CompactReplication(binary, work/'short', 1),
              'legacy': CompactReplication(binary, work/'legacy', 0),
              'alternate_repair': AlternateRepair(binary, work/'alternate')}
    (work/'result.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    Main()
