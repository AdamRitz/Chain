"""Negotiated transaction relay, block prefill, fragments and peer recovery."""
import argparse
import json
from pathlib import Path
import socket
import struct
import time

from ForkNetworkTest import MakeBlock
from NetworkTest import (Frame, GetStats, ReadAll, SendFrame, StartNode,
                         StopNode, VerifyAccounts, WaitStats)
from PropagationNetworkTest import Prepare, Settings


CHUNK = 65536


def Fragments(kind, body, identity=1):
    assert len(body) > CHUNK
    return [struct.pack('<QBII', identity, kind, len(body), offset)
            + body[offset:offset+CHUNK]
            for offset in range(0, len(body), CHUNK)]


class Peer:
    def __init__(self, port, negotiate=True, **capabilities):
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=3)
        self.capabilities = {'relay_protocol': 2, **capabilities}
        self.fragments = {}
        self.fragment_count = 0
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            announced = probe.getsockname()[1]
        self.socket.sendall(b'\x03'+struct.pack('<H', announced))
        self.Send(14, json.dumps(self.capabilities).encode())
        self.Read({13})
        if negotiate:
            self.Send(13)
            self.Read({14})
        # An ordered query ensures the remote processed our capability reply.
        self.Send(19)
        self.Read({20})

    def Send(self, kind, body=b''):
        self.socket.sendall(Frame(kind, body))

    def Read(self, kinds, timeout=4):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.socket.settimeout(max(.01, deadline-time.monotonic()))
            kind, size = struct.unpack('<BI', ReadAll(self.socket, 5))
            assert size <= 2*1024*1024
            body = ReadAll(self.socket, size)
            if kind == 13:
                self.Send(14, json.dumps(self.capabilities).encode())
            if kind == 55:
                assert self.capabilities.get('message_fragments'), 'Unnegotiated fragment output'
                identity, original, total, offset = struct.unpack_from('<QBII', body)
                data = body[17:]
                state = self.fragments.setdefault(identity, [original, total, bytearray()])
                assert (state[0], state[1], len(state[2])) == (original, total, offset)
                assert len(data) == CHUNK or offset+len(data) == total
                state[2].extend(data)
                self.fragment_count += 1
                if len(state[2]) < total:
                    continue
                assert len(state[2]) == total
                kind, body = original, bytes(state[2])
                del self.fragments[identity]
            if kind in kinds:
                return kind, body
        raise TimeoutError(f'Expected one of {kinds}')

    def ExpectClosed(self):
        self.socket.settimeout(3)
        try:
            while self.socket.recv(65536):
                pass
        except (ConnectionResetError, BrokenPipeError):
            pass

    def Close(self):
        self.socket.close()


def FragmentProtocol(binary, work):
    dataset, txs, config = Prepare(binary, work, count=800)
    node = StartNode(binary, work, 'node', **Settings(config))
    peers = []
    try:
        source = Peer(node['port'], message_fragments=True)
        observer = Peer(node['port'], message_fragments=True)
        legacy = Peer(node['port'], negotiate=False, relay_protocol=1)
        peers.extend([source, observer, legacy])
        body = b''.join(txs)
        pieces = Fragments(10, body)
        source.Send(55, pieces[0])
        source.Send(19)
        _, head = source.Read({20})
        assert len(head) == 80 and struct.unpack_from('<Q', head, 64)[0] == 1
        before = GetStats(node['port'])
        assert before['valid'] == 0 and before['fragment_buffered_bytes'] == len(body)
        for piece in pieces[1:]:
            source.Send(55, piece)
        stats = WaitStats(node['port'], lambda s: s['valid'] == len(txs), timeout=10)
        assert observer.Read({10})[1] == body
        assert observer.fragment_count == len(pieces), 'Large response is sent in negotiated fragments'
        assert legacy.Read({10})[1] == body and legacy.fragment_count == 0
        assert stats['fragment_received'] == len(pieces)
        assert stats['fragment_completed'] == 1 and stats['fragment_buffered_bytes'] == 0

        # Reusing a completed fragment identity closes only the offending peer.
        source.Send(55, pieces[0])
        source.ExpectClosed()
        # A peer without the negotiated feature cannot inject fragment frames.
        legacy.Send(55, pieces[0])
        legacy.ExpectClosed()
        # A bad continuation cannot retain the allocated partial message.
        malformed = Peer(node['port'], message_fragments=True)
        peers.append(malformed)
        malformed.Send(55, pieces[0])
        wrong_offset = bytearray(pieces[1])
        struct.pack_into('<I', wrong_offset, 13, 1)
        malformed.Send(55, wrong_offset)
        malformed.ExpectClosed()
        after = WaitStats(node['port'], lambda s: s['fragment_buffered_bytes'] == 0)
        assert after['valid'] == len(txs) and not after['failed']
        observer.Send(19)
        assert len(observer.Read({20})[1]) == 80, 'Unrelated peer remains usable'
        return {'transactions': len(txs), 'bytes': len(body), 'fragments': len(pieces),
                'control_message_between_fragments': True, 'legacy_body_equal': True,
                'replay_unnegotiated_bad_offset_closed': True}
    finally:
        for peer in peers:
            peer.Close()
        StopNode(node)


def InventoryRepair(binary, work):
    _, txs, config = Prepare(binary, work)
    node = StartNode(binary, work, 'node', **Settings(config, **{'tx-push-peers': 0}))
    peers = []
    try:
        first = Peer(node['port'], tx_inventory=True)
        alternate = Peer(node['port'], tx_inventory=True)
        peers.extend([first, alternate])
        inventory = b'\0'+txs[0][80:112]
        first.Send(53, inventory)
        assert first.Read({54})[1] == inventory
        alternate.Send(53, inventory)
        invalid = bytearray(txs[0])
        invalid[112] ^= 1
        first.Send(10, invalid)
        WaitStats(node['port'], lambda s: s['invalid'] == 1)
        assert alternate.Read({54})[1] == inventory, 'Invalid body must not clear alternate source'
        alternate.Send(10, txs[0])
        stats = WaitStats(node['port'], lambda s: s['valid'] == 1)
        assert stats['tx_inventory_requests'] >= 2 and stats['tx_inventory_retries'] >= 1
        first.Send(10, txs[0])
        WaitStats(node['port'], lambda s: s['duplicate'] >= 1)
        return {'accepted': stats['valid'], 'invalid': stats['invalid'],
                'requests': stats['tx_inventory_requests'], 'retries': stats['tx_inventory_retries']}
    finally:
        for peer in peers:
            peer.Close()
        StopNode(node)


def PrefilledReplication(binary, work):
    dataset, txs, config = Prepare(binary, work)
    nodes = []
    try:
        source = StartNode(binary, work, 'source', **Settings(config, **{'compact-prefill-max': 64}))
        nodes.append(source)
        target = StartNode(binary, work, 'target', **Settings(config, **{
            'seed': f"127.0.0.1:{source['port']}", 'compact-prefill-max': 64}))
        nodes.append(target)
        for node in nodes:
            WaitStats(node['port'], lambda s: s['short_compact_peers'] == 1)
        genesis = bytes.fromhex(GetStats(source['port'])['head'])
        block = MakeBlock(genesis, 2, txs)
        SendFrame(source['port'], 7, block)
        left = WaitStats(source['port'], lambda s: s['head'] == block[80:112].hex(), timeout=10)
        right = WaitStats(target['port'], lambda s: s['head'] == block[80:112].hex(), timeout=10)
        assert left['compact_prefilled_announcements'] >= 1
        assert right['compact_prefilled_received'] == len(txs)
        assert right['missing_transactions'] == 0, 'All prefilled transactions avoid repair request'
        for node in nodes:
            VerifyAccounts(node['port'], dataset, len(txs), str(dataset)+'.genesis.json')
        return {'head': right['head'], 'prefilled': right['compact_prefilled_received'],
                'missing_transactions': right['missing_transactions']}
    finally:
        for node in reversed(nodes):
            StopNode(node)


def BackupRecovery(binary, work):
    dataset, txs, config = Prepare(binary, work)
    nodes = []
    settings = Settings(config, **{'tx-push-peers': 0})
    try:
        anchor = StartNode(binary, work, 'anchor', **settings)
        nodes.append(anchor)
        backup = StartNode(binary, work, 'backup', **{**settings,
            'seed': f"127.0.0.1:{anchor['port']}"})
        nodes.append(backup)
        primary = StartNode(binary, work, 'primary', **settings)
        nodes.append(primary)
        target = StartNode(binary, work, 'target', **{**settings,
            'seed': f"127.0.0.1:{primary['port']}", 'target-peers': 1,
            'backup-seed': f"127.0.0.1:{backup['port']}"})
        nodes.append(target)
        WaitStats(target['port'], lambda s: s['short_compact_peers'] == 1)
        WaitStats(backup['port'], lambda s: s['short_compact_peers'] == 1)
        assert GetStats(target['port'])['backup_connect_attempts'] == 0
        StopNode(primary)
        nodes.remove(primary)
        started = time.monotonic()
        recovered = WaitStats(target['port'], lambda s:
            s['backup_connect_attempts'] >= 1 and s['short_compact_peers'] == 1, timeout=18)
        recovered_seconds = time.monotonic()-started
        WaitStats(backup['port'], lambda s: s['short_compact_peers'] == 2)
        SendFrame(target['port'], 10, b''.join(txs))
        for node in nodes:
            WaitStats(node['port'], lambda s: s['pool'] == len(txs), timeout=10)
        for node in (backup, anchor):
            stats = GetStats(node['port'])
            assert stats['tx_inventory_requests'] >= len(txs)
        genesis = bytes.fromhex(GetStats(target['port'])['head'])
        block = MakeBlock(genesis, 2, txs)
        SendFrame(target['port'], 7, block)
        for node in nodes:
            WaitStats(node['port'], lambda s: s['head'] == block[80:112].hex(), timeout=10)
            VerifyAccounts(node['port'], dataset, len(txs), str(dataset)+'.genesis.json')
        return {'started_nodes': 4, 'remaining_nodes': 3, 'transactions': len(txs),
                'recovery_seconds': recovered_seconds,
                'backup_attempts': recovered['backup_connect_attempts'],
                'head': block[80:112].hex()}
    finally:
        for node in reversed(nodes):
            StopNode(node)


def Main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    binary, work = args.bin.resolve(), args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    report = {}
    # Persist each completed case so a later failing case retains its evidence.
    for name, function in [('fragments', FragmentProtocol), ('inventory', InventoryRepair),
                           ('prefill', PrefilledReplication), ('backup', BackupRecovery)]:
        report[name] = function(binary, work/name)
        (work/'result.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    Main()
