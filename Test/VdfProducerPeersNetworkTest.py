"""VDF producer connections above a two-peer ordinary ring.

Seven real node processes elect five producers on fresh databases. The test
checks authenticated endpoints, all ten producer-to-producer links, connections
to the following epoch's producers, and the order used to relay a new block.
"""
import argparse
import json
from pathlib import Path
import socket
import struct
import subprocess
import time
import traceback

from nacl.signing import VerifyKey

from NetworkTest import FLAGS, ExeName, Frame, ReadAll, SendFrame
from VdfNetworkTest import Case, Hash, Json, Query, Require, Transaction
from VdfRotationNetworkTest import Owner


class ProducerPeersCase(Case):
    def __init__(self, binary, work, timeout, epoch_blocks, slot_ms, deadline, ipv6=False):
        super().__init__(binary, work, 'producer-peers', timeout, epoch_blocks, slot_ms, deadline)
        self.config['producer_rotation'] = True
        self.peer_address = '::1' if ipv6 else '127.0.0.1'
        self.report['peer_address'] = self.peer_address
        Json(self.work / 'genesis.json', self.genesis)

    def Start(self, indices=range(7), producers=range(7), groups=None):
        """One outgoing edge and one incoming edge give each node a full ring."""
        started = []
        for index in indices:
            self.Budget()
            Require(index not in self.nodes, 'Cannot start a second process for a database')
            directory = self.work / f'node{index}'
            generation = self.generations[index]
            self.generations[index] += 1
            others = list(next(group for group in groups if index in group)) if groups else list(range(7))
            position = others.index(index)
            host = '[::1]' if self.peer_address == '::1' else self.peer_address
            peers = ([f'{host}:{self.ports[others[(position + 1) % len(others)]]}']
                     if len(others) > 1 else [])
            configuration = directory / f'config-{generation}.yaml'
            network = {'peers': peers}
            if self.peer_address == '::1':
                network['bind_v6'] = '::1'
            configuration.write_text('node: ' + json.dumps(network) + '\n', encoding='utf-8')
            options = {'config': configuration, 'data': directory / 'db', 'genesis': self.work / 'genesis.json',
                       'vdf-key': directory / 'identity.seed', 'bind': '127.0.0.1', 'port': self.ports[index],
                       'produce': int(index in producers), 'sync': 1, 'io-threads': 1, 'verify-threads': 1,
                       'candidate-threads': 1, 'block-verify-threads': 1, 'query-threads': 1,
                       'max-block-txs': 64, 'max-peers': 2, 'peer-discovery': 0, 'target-peers': 0,
                       'relay-transactions': 1, 'clock-sync': 0, 'run-seconds': 900, 'experiment-index': 1,
                       'metrics': directory / f'final-{generation}.json'}
            command = [str(self.binary / ExeName('boost'))]
            for key, value in options.items():
                command.extend(['--' + key, str(value)])
            log = (directory / f'node-{generation}.log').open('w', encoding='utf-8')
            try:
                process = subprocess.Popen(command, cwd=directory, stdout=log, stderr=log, creationflags=FLAGS)
            except BaseException:
                log.close()
                raise
            self.nodes[index] = {'process': process, 'log': log}
            self.report['starts'].append({'index': index, 'generation': generation, 'pid': process.pid,
                                          'produces': index in producers, 'peers': peers, 'max_peers': 2})
            started.append(index)
        self.Wait(lambda: all(self.Stats(i)['vdf']['enabled'] for i in started), 'node startup')


def StableRows(case):
    rows = [case.Stats(i) for i in range(7)]
    if len({row['vdf']['epoch'] for row in rows}) != 1:
        return False
    roster = rows[0]['vdf']['producers']
    if len(roster) != 5 or any(row['vdf']['producers'] != roster for row in rows):
        return False
    if max(row['height'] for row in rows) - min(row['height'] for row in rows) > 2:
        return False
    return rows


def AuditEndpoints(case, rows):
    for index, row in enumerate(rows):
        Require(row['vdf']['peer_reserve_limit'] == 9 and row['vdf']['peer_pending_limit'] == 4,
                'Producer and pending identity connections have fixed bounded capacity')
        Require(0 <= row['vdf']['peer_pending'] <= 4, 'Unauthenticated extra connections remain bounded')
        endpoints = row['vdf']['peer_connections']
        keys = [endpoint['public_key'] for endpoint in endpoints]
        Require(len(set(keys)) == len(keys), 'Authenticated producer endpoint is unique')
        for endpoint in endpoints:
            public = endpoint['public_key']
            Require(public in case.public and public != case.public[index], 'Endpoint identifies another test node')
            Require(endpoint['address'] == case.peer_address and endpoint['port'] == case.ports[case.public.index(public)],
                    'Authenticated public key is bound to its actual listening endpoint')


def CompleteCurrent(case, rows):
    roster = rows[0]['vdf']['producers']
    edges = set()
    for public in roster:
        index = case.public.index(public)
        connected = set(rows[index]['vdf']['producer_peers'])
        if not set(roster) - {public} <= connected:
            return False
        block_connections = {peer['public_key'] for peer in rows[index]['vdf']['peer_connections']
                             if peer.get('block_connection')}
        if not set(roster) - {public} <= block_connections:
            return False
        for other in roster:
            if other != public:
                edges.add(tuple(sorted((public, other))))
        Require(rows[index]['peers'] > 2, 'Producer connections can exceed the full ordinary quota of two')
    Require(len(edges) == 10, 'Five producers have all ten undirected connections')
    return sorted([list(edge) for edge in edges])


def CompleteNext(case, rows):
    current = set(rows[0]['vdf']['producers'])
    following = rows[0]['vdf']['next_producers']
    if len(following) != 5 or any(row['vdf']['next_producers'] != following for row in rows):
        return False
    if not set(following) - current:
        return False
    for public in current | set(following):
        row = rows[case.public.index(public)]
        if not set(following) - {public} <= set(row['vdf']['next_producer_peers']):
            return False
        block_connections = {peer['public_key'] for peer in row['vdf']['peer_connections']
                             if peer.get('block_connection')}
        if not set(following) - {public} <= block_connections:
            return False
    return following


def AuditPriority(case, rows):
    """Inspect locally generated blocks while their current roster is unchanged."""
    roster = rows[0]['vdf']['producers']
    epoch = rows[0]['vdf']['epoch']
    checked = []
    for public in roster:
        index = case.public.index(public)
        vdf = rows[index]['vdf']
        recipients = vdf.get('last_block_recipients', [])
        # Recipients are appended when each frame is queued. An RPC can observe
        # the broadcast between the first and fourth queue operation.
        if len(recipients) < 4 or vdf.get('last_block_epoch') != epoch:
            continue
        if (vdf['last_block_height'] - 1) % case.config['epoch_blocks'] == 0:
            # The final block of an epoch prioritizes the following roster.
            # Ordinary blocks below test the four current producer links.
            continue
        slot = vdf['last_block_slot']
        if Owner(roster, slot, case.config) != public:
            continue
        expected_next = Owner(roster, slot + 1, case.config)
        Require(len(recipients) >= 4 and recipients[0] == expected_next,
                'Next scheduled producer is the first recipient of the new block')
        Require(set(recipients[:4]) == set(roster) - {public},
                'Other three current producers follow before ordinary peers')
        checked.append({'public_key': public, 'epoch': epoch, 'slot': slot,
                        'hash': vdf['last_block_hash'], 'recipients': recipients})
    return checked


def Identity(key, chain, port, challenge):
    public = bytes(key.verify_key)
    digest = Hash(b'Chain/VdfPeer/Identity/v1' + chain + public + struct.pack('<H', port) + challenge)
    return public + key.sign(digest).signature


def RejectIdentity(case, index, announced_port, response):
    """Negotiate the real protocol and require the invalid answer to close it."""
    capabilities = json.dumps({'relay_protocol': 2, 'vdf_peer_protocol': 1}).encode()
    challenge, answered, closed = None, False, False
    with socket.create_connection(('127.0.0.1', case.ports[index]), timeout=3) as connection:
        connection.sendall(b'\x04' + struct.pack('<H', announced_port) + Frame(14, capabilities))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            connection.settimeout(max(.01, deadline - time.monotonic()))
            try:
                kind, size = struct.unpack('<BI', ReadAll(connection, 5))
                Require(size <= 2 * 1024 * 1024, 'Identity test response frame bound')
                data = ReadAll(connection, size)
            except (ConnectionError, ConnectionResetError, BrokenPipeError):
                closed = True
                break
            if kind == 13:
                connection.sendall(Frame(14, capabilities))
            elif kind == 56:
                Require(len(data) == 32 and not answered, 'A fresh connection has one 32-byte identity challenge')
                challenge, answered = data, True
                connection.sendall(Frame(57, response(challenge)))
        Require(answered and closed, 'An invalid identity answer must close its connection')
    return challenge


def EarlyChallenge(case, index, announced_port, chain):
    """A producer challenge may overtake the capabilities reply on its queue."""
    challenge = Hash(b'Chain/VdfProducerPeersNetwork/early-challenge')
    server_challenge, identity = None, None
    with socket.create_connection(('127.0.0.1', case.ports[index]), timeout=3) as connection:
        # Deliberately do not send type 14 before the two identity replies.
        connection.sendall(b'\x04' + struct.pack('<H', announced_port) + Frame(56, challenge))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (server_challenge is None or identity is None):
            connection.settimeout(max(.01, deadline - time.monotonic()))
            kind, size = struct.unpack('<BI', ReadAll(connection, 5))
            Require(size <= 2 * 1024 * 1024, 'Early challenge response frame bound')
            data = ReadAll(connection, size)
            if kind == 56:
                Require(len(data) == 32, 'Server identity challenge has the expected length')
                server_challenge = data
            elif kind == 57:
                Require(len(data) == 96 and data[:32].hex() == case.public[index],
                        'Early challenge is answered by the actual node identity')
                digest = Hash(b'Chain/VdfPeer/Identity/v1' + chain + data[:32]
                              + struct.pack('<H', case.ports[index]) + challenge)
                VerifyKey(data[:32]).verify(digest, data[32:])
                identity = data
        Require(server_challenge is not None and identity is not None,
                'Challenge before capabilities must receive both authentication replies')
        Require(server_challenge != challenge, 'Each direction uses its own challenge')
    return {'challenge_before_capabilities': True, 'server_challenge_received': True,
            'server_signature_verified': True}


def ExerciseIdentityRejections(case):
    def ready():
        rows = StableRows(case)
        return rows if rows and CompleteCurrent(case, rows) else False
    rows = case.Wait(ready, 'stable connected producer list before identity attacks')
    roster = rows[0]['vdf']['producers']
    target, claimed = [case.public.index(public) for public in roster[:2]]
    key = case.keys[claimed]
    chain = Query(case.ports[target], 19, raw=True)[:32]
    before_failures = case.Stats(target)['vdf']['peer_authentication_failures']
    # Keep the announced endpoint occupied so it cannot accidentally identify
    # another test process. This socket never listens or forwards any messages.
    with socket.socket() as endpoint:
        endpoint.bind(('127.0.0.1', 0))
        port = endpoint.getsockname()[1]
        early = EarlyChallenge(case, target, port, chain)
        first = RejectIdentity(case, target, port, lambda challenge: bytes(key.verify_key) + bytes(64))
        RejectIdentity(case, target, port, lambda challenge:
                       Identity(key, chain, port + 1 if port < 65535 else port - 1, challenge))
        different_chain = bytes((chain[0] ^ 1,)) + chain[1:]
        RejectIdentity(case, target, port, lambda challenge: Identity(key, different_chain, port, challenge))
        replay = Identity(key, chain, port, first)
        last = RejectIdentity(case, target, port, lambda challenge: replay)
        Require(last != first, 'Connection identity challenges cannot be replayed')

        def intact():
            live = StableRows(case)
            if not live or not CompleteCurrent(case, live):
                return False
            AuditEndpoints(case, live)
            return live if all(row['vdf']['peer_pending'] == 0 for row in live) else False
        after = case.Wait(intact, 'invalid identity attempts release capacity and preserve the honest producer mesh')
        failures = case.Stats(target)['vdf']['peer_authentication_failures'] - before_failures
        Require(failures >= 4, 'All four malformed identities reached authentication and were rejected')
    case.report['identity_rejections'] = {
        'invalid_signature': True, 'different_port': True, 'different_chain': True,
        'replayed_challenge': True, 'pending_after': [row['vdf']['peer_pending'] for row in after],
        'honest_producer_links_retained': True, 'authentication_failures': failures}
    case.report['early_identity_challenge'] = early
    case.Check('a challenge sent before capabilities receives a signed reply and a separate fresh challenge')
    case.Check('invalid signatures, wrong ports, wrong chains and replayed challenges cannot take producer connections')


def Exercise(case):
    case.Start()
    case.Wait(lambda: min(case.Stats(i)['vdf']['computed'] for i in range(7)) >= 3,
              'all seven candidates computed their VDFs')
    snapshots, priorities, preconnected = {}, {}, {}
    case.report['producer_connections'] = snapshots
    initial_roster = None

    def connected():
        nonlocal initial_roster
        rows = StableRows(case)
        if not rows or rows[0]['vdf']['epoch'] < 2:
            return False
        edges = CompleteCurrent(case, rows)
        if not edges:
            return False
        AuditEndpoints(case, rows)
        roster = rows[0]['vdf']['producers']
        initial_roster = initial_roster or list(roster)
        epoch = rows[0]['vdf']['epoch']
        record = {'epoch': epoch, 'height': [row['height'] for row in rows], 'producers': roster,
                  'edges': edges, 'peers': [row['peers'] for row in rows],
                  'endpoints': [row['vdf']['peer_connections'] for row in rows]}
        for item in AuditPriority(case, rows):
            priorities[item['public_key']] = item
        case.report['block_priority'] = list(priorities.values())
        following = CompleteNext(case, rows)
        if following:
            record['next_producers'] = following
            record['next_peer_connections'] = [row['vdf']['next_producer_peers'] for row in rows]
            preconnected[(epoch + 1, tuple(following))] = record
        snapshots.setdefault('current_complete', record)
        previous = preconnected.get((epoch, tuple(roster)))
        if set(roster) != set(initial_roster) and previous:
            snapshots.setdefault('following_connected_before_activation', previous)
            snapshots.setdefault('changed_roster_complete', record)
        return (len(priorities) >= 5 and 'following_connected_before_activation' in snapshots
                and 'changed_roster_complete' in snapshots)

    case.Wait(connected, 'current and next producers connect above the ordinary quota and relay in order', timeout=90)
    case.report['producer_connections'] = snapshots
    case.report['block_priority'] = list(priorities.values())
    case.Check('five producers maintain all ten authenticated connections over a sparse ordinary ring')
    case.Check('all ten producer links use block connections while transfers propagate on the ordinary ring')
    case.Check('new producers connect before their election takes effect, despite max-peers=2')
    case.Check('producer public keys match their signed listening endpoints')
    case.Check('at least five producers send a generated block to the next producer before the other three')
    ExerciseIdentityRejections(case)

    # Exercise useful work after the extra links are established, then restart
    # without production so all seven durable state snapshots can be compared.
    transaction = Transaction(case.keys[0], bytes(case.keys[6].verify_key), 17)
    SendFrame(case.ports[0], 1, transaction)
    case.Wait(lambda: all(case.Stats(i)['chain_transactions'] == 1 for i in range(7)),
              'transfer commits on all seven replicas')
    case.Stop()
    case.Start(producers=())
    case.Wait(case.Converged, 'seven durable replicas converge after restarting without production')
    blocks = case.Chain(name='canonical')
    case.report['state'] = case.AuditState(blocks, range(7))
    case.Check('all seven replicas agree on the canonical chain, balances and nonces after restart')


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--budget', type=float, default=180)
    parser.add_argument('--epoch-blocks', type=int, default=16)
    parser.add_argument('--slot-ms', type=int, default=250)
    parser.add_argument('--ipv6', action='store_true', help='use IPv6 for node-to-node connections')
    args = parser.parse_args()
    if args.epoch_blocks < 16 or not 200 <= args.slot_ms <= 1000 or args.timeout <= 0 or args.budget <= 0:
        parser.error('use epoch-blocks >=16, slot-ms 200..1000 and positive timeout/budget')
    binary, work = args.bin.resolve(), args.work.resolve()
    Require((binary / ExeName('boost')).is_file(), 'Node binary missing')
    if args.ipv6:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(('::1', 0))
    work.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    case = ProducerPeersCase(binary, work, args.timeout, args.epoch_blocks, args.slot_ms, started + args.budget,
                             ipv6=args.ipv6)
    try:
        Exercise(case)
        case.report['status'] = 'passed'
    except BaseException as error:
        case.report.update(status='failed', error=repr(error), traceback=traceback.format_exc())
        raise
    finally:
        case.report['elapsed_seconds'] = time.monotonic() - started
        try:
            case.Finish()
        except BaseException as error:
            case.report.update(status='failed', cleanup_error=repr(error))
            raise
        finally:
            Json(work / 'result.json', case.report)
    print('{"status":"passed","scope":"seven-process VDF producer connections"}', flush=True)


if __name__ == '__main__':
    Main()
