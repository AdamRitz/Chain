"""Seven real node processes exercising the VDF protocol on fresh databases.

This is a correctness suite. Its process count and timings are not a throughput
or 1,024-node scalability measurement. Test identities are deterministic and
must never be funded on an existing chain. Evidence is retained on failure.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time
import traceback

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from NetworkTest import FLAGS, ExeName, Frame, GetAccount, GetStats, ReadAll, SendFrame


def Hash(data):
    return hashlib.blake2b(data, digest_size=32).digest()


def Require(condition, message):
    if not condition:
        raise AssertionError(message)


def Json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def Query(port, kind, payload=b'', raw=False):
    with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
        connection.sendall(b'\x02' + Frame(kind, payload))
        response, size = struct.unpack('<BI', ReadAll(connection, 5))
        Require(response == kind + 1 and size <= 2 * 1024 * 1024, 'Invalid RPC frame')
        data = ReadAll(connection, size)
        return data if raw else json.loads(data)


def Candidates(port):
    """Follow message 50 with RPC 35; its response delimits the queued 49 frames."""
    result = []
    with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
        connection.sendall(b'\x02' + Frame(50) + Frame(35))
        deadline = time.monotonic() + 5
        while True:
            Require(time.monotonic() < deadline, 'Candidate response exceeded total timeout')
            connection.settimeout(max(.001, deadline - time.monotonic()))
            header = ReadAll(connection, 5)
            kind, size = struct.unpack('<BI', header)
            if kind == 36:
                Require(size <= 16384, 'Invalid state response after candidates')
                json.loads(ReadAll(connection, size))
                break
            Require(kind == 49 and size == 382 and len(result) < 12, 'Invalid VDF candidate response')
            data = ReadAll(connection, size)
            Candidate(data)
            result.append(data)
    return result


def Candidate(data):
    Require(len(data) == 382 and data[:6] == b'VFEL\x01\x00', 'Candidate format')
    public_key = data[78:110]
    VerifyKey(public_key).verify(Hash(b'Chain/VdfElection/Signature/v1' + data[:318]), data[318:])
    challenge = Hash(b'Chain/VdfElection/Challenge/v1' + data[4:118])
    return {'chain': data[6:38].hex(), 'epoch': struct.unpack_from('<Q', data, 38)[0],
            'anchor': data[46:78].hex(), 'public_key': public_key.hex(),
            'iterations': struct.unpack_from('<Q', data, 110)[0],
            'score': Hash(b'Chain/VdfElection/Score/v1' + challenge + data[118:218]).hex()}


def Merkle(transactions):
    hashes = [tx[80:112] for tx in transactions]
    while len(hashes) > 1:
        if len(hashes) % 2:
            hashes.append(hashes[-1])
        hashes = [Hash(hashes[i] + hashes[i + 1]) for i in range(0, len(hashes), 2)]
    return hashes[0] if hashes else bytes(32)


def Unsigned(slot, public_key, candidates, proof):
    Require(len(candidates) <= 5, 'At most five candidate records')
    return (struct.pack('<HQ', 1, slot) + public_key + bytes((len(candidates), int(proof is not None)))
            + b''.join(candidates) + (proof or b''))


def MakeBlock(parent, height, transactions, slot, key, candidates=(), proof=None):
    unsigned = Unsigned(slot, bytes(key.verify_key), candidates, proof)
    root = Hash(b'Chain/VdfConsensus/BlockRoot/v1' + Merkle(transactions) + unsigned)
    header = parent + root + struct.pack('<QQ', height, len(transactions))
    digest = Hash(header)
    tail = unsigned + key.sign(digest).signature
    return header + digest + b''.join(transactions) + b'VDB1' + struct.pack('<I', len(tail)) + tail


def Block(data):
    Require(len(data) >= 112 and data[80:112] == Hash(data[:80]), 'Block header hash')
    height, count = struct.unpack_from('<QQ', data, 64)
    offset = 112 + count * 176
    Require(offset <= len(data), 'Truncated transactions')
    transactions = [data[112 + i * 176:112 + (i + 1) * 176] for i in range(count)]
    for tx in transactions:
        Require(Hash(tx[:80]) == tx[80:112], 'Transaction hash')
        VerifyKey(tx[:32]).verify(tx[80:112], tx[112:])
    result = {'height': height, 'hash': data[80:112].hex(), 'parent': data[:32].hex(),
              'transactions': transactions, 'root': data[32:64], 'raw': data}
    if height == 1:
        Require(offset == len(data) and count == 0, 'Genesis has no VDF extension')
        return result
    # The suite emits native transfers only; never search arbitrary payload bytes for magic.
    tail = data[offset:]
    Require(len(tail) >= 116 and tail[:4] == b'VDB1', 'VDF extension at transaction boundary')
    size = struct.unpack_from('<I', tail, 4)[0]
    version, slot = struct.unpack_from('<HQ', tail, 8)
    count, has_proof = tail[50:52]
    Require(version == 1 and count <= 5 and has_proof <= 1 and size == len(tail) - 8,
            'VDF extension fields')
    Require(len(tail) == 116 + 382 * (count + has_proof), 'VDF extension exact length')
    public_key = tail[18:50]
    VerifyKey(public_key).verify(data[80:112], tail[-64:])
    Require(Hash(b'Chain/VdfConsensus/BlockRoot/v1' + Merkle(transactions) + tail[8:-64]) == data[32:64],
            'VDF extension is bound to the block Merkle root')
    records = [tail[52 + i * 382:52 + (i + 1) * 382] for i in range(count + has_proof)]
    for record in records:
        Candidate(record)
    result.update(slot=slot, producer=public_key.hex(), candidates=records[:count],
                  proof=records[-1] if has_proof else None)
    return result


def Transaction(key, receiver, amount, nonce=1):
    unsigned = bytes(key.verify_key) + receiver + struct.pack('<QQ', amount, nonce)
    digest = Hash(unsigned)
    return unsigned + digest + key.sign(digest).signature


def TransactionBlock(blocks, transaction):
    matches = [block for block in blocks for tx in block['transactions'] if tx == transaction]
    Require(len(matches) == 1, 'Recovery transaction must appear exactly once in the canonical chain')
    return matches[0]


class Case:
    def __init__(self, binary, work, name, timeout, epoch_blocks, slot_ms, deadline):
        self.binary, self.work = binary, work / name
        self.work.mkdir()
        self.timeout = timeout
        self.deadline = deadline
        self.config = {'enabled': True, 'epoch_blocks': epoch_blocks, 'iterations': 4096,
                       'slot_ms': slot_ms, 'start_time_ms': int(time.time() * 1000) // slot_ms * slot_ms,
                       'fallback_slots': 20, 'future_slots': 2, 'producers': 5}
        self.keys = [SigningKey(Hash(f'Chain/VdfNetwork/test-only/{name}/{i}'.encode())) for i in range(7)]
        self.public = [bytes(key.verify_key).hex() for key in self.keys]
        self.genesis = {'base_fee': 1, 'accounts': [{'public_key': key, 'balance': 1000000}
                                                  for key in self.public], 'vdf': self.config}
        Json(self.work / 'genesis.json', self.genesis)
        self.nodes, self.records = {}, {}
        self.report = {'case': name, 'status': 'running', 'process_count': 7, 'config': self.config,
                       'public_keys': self.public, 'checks': [], 'starts': [], 'snapshots': {}}
        sockets = [socket.socket() for _ in range(7)]
        try:
            for sock in sockets:
                sock.bind(('127.0.0.1', 0))
            self.ports = [sock.getsockname()[1] for sock in sockets]
        finally:
            for sock in sockets:
                sock.close()
        self.generations = [0] * 7
        for index, key in enumerate(self.keys):
            directory = self.work / f'node{index}'
            directory.mkdir()
            path = directory / 'identity.seed'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='ascii') as output:
                output.write(bytes(key).hex() + '\n')

    def Start(self, indices=range(7), producers=range(6), groups=None):
        started = []
        for index in indices:
            self.Budget()
            Require(index not in self.nodes, 'Cannot start a second process for a database')
            directory = self.work / f'node{index}'
            generation = self.generations[index]
            self.generations[index] += 1
            group = next(group for group in groups if index in group) if groups else range(7)
            peers = [f'127.0.0.1:{self.ports[other]}' for other in group if other != index]
            configuration = directory / f'config-{generation}.yaml'
            configuration.write_text('node: ' + json.dumps({'peers': peers}) + '\n', encoding='utf-8')
            options = {'config': configuration, 'data': directory / 'db', 'genesis': self.work / 'genesis.json',
                       'vdf-key': directory / 'identity.seed', 'bind': '127.0.0.1', 'port': self.ports[index],
                       'produce': int(index in producers), 'sync': 1, 'io-threads': 1, 'verify-threads': 1,
                       'candidate-threads': 1, 'block-verify-threads': 1, 'query-threads': 1,
                       'max-block-txs': 64, 'max-peers': 32, 'relay-transactions': 1,
                       'clock-sync': 0, 'run-seconds': 900, 'experiment-index': 1,
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
                                          'produces': index in producers, 'peers': peers})
            started.append(index)
        self.Wait(lambda: all(self.Stats(i)['vdf']['enabled'] for i in started), 'node startup')

    def Stop(self, indices=None):
        indices = list(self.nodes if indices is None else indices)
        # Signal every requested process before waiting, so the other five cannot
        # advance through several epochs while one process drains its VDF worker.
        for index in indices:
            node = self.nodes.get(index)
            if node and node['process'].poll() is None:
                node['process'].terminate()
        errors, killed = [], []
        deadline = time.monotonic() + 15
        for index in indices:
            node = self.nodes.get(index)
            if not node:
                continue
            try:
                node['process'].wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                node['process'].kill()
                killed.append(index)
                errors.append(f'node{index} required kill')
        # All processes share both deadlines. A hung group must not consume
        # seven separate 15-second waits and outlive CTest's outer timeout.
        deadline = time.monotonic() + 5
        for index in indices:
            node = self.nodes.get(index)
            if not node:
                continue
            if index in killed:
                try:
                    node['process'].wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    errors.append(f'node{index} did not exit after kill')
                    continue
            if node['process'].poll() is not None:
                node['log'].close()
                del self.nodes[index]
        if errors:
            self.report.setdefault('stop_errors', []).extend(errors)
            raise RuntimeError('; '.join(errors))

    def Stats(self, index):
        self.Budget()
        process = self.nodes[index]['process']
        Require(process.poll() is None, f'node{index} exited with {process.returncode}')
        row = GetStats(self.ports[index])
        Require(not row['failed'], f'node{index} reported failure')
        return row

    def Budget(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('VDF network suite exceeded its total time budget')

    def Wait(self, predicate, description, timeout=None):
        self.Budget()
        end = min(time.monotonic() + (timeout or self.timeout), self.deadline)
        last_error = None
        while time.monotonic() < end:
            try:
                result = predicate()
                if result:
                    return result
            except (OSError, ConnectionError) as error:
                last_error = repr(error)
            time.sleep(.03)
        raise TimeoutError(f'{description}; last transport error: {last_error}')

    def Snapshot(self, name):
        rows = {str(i): self.Stats(i) for i in self.nodes}
        self.report['snapshots'][name] = rows
        return rows

    def Capture(self):
        producers = [i for i in range(6) if i in self.nodes]
        with ThreadPoolExecutor(max_workers=6) as pool:
            batches = list(pool.map(lambda i: Candidates(self.ports[i]), producers))
        for batch in batches:
            for record in batch:
                self.records[Hash(record).hex()] = record
        directory = self.work / 'candidates'
        directory.mkdir(exist_ok=True)
        for digest, record in self.records.items():
            (directory / (digest + '.bin')).write_bytes(record)

    def Ready(self, epoch=3):
        def ready():
            rows = [self.Stats(i) for i in range(7)]
            return (min(row['vdf']['epoch'] for row in rows) >= epoch and
                    all(row['vdf']['computed'] >= 3 for row in rows[:6]) and
                    all(len(row['vdf']['producers']) == 5 for row in rows))
        self.Wait(ready, 'six producers computed VDFs and five-member roster activated')

    def Freeze(self, capture=False, require_all_proofs=False):
        if require_all_proofs:
            def complete():
                self.Capture()
                row = self.Stats(6)
                position = (row['height'] - 1) % self.config['epoch_blocks']
                if len(row['vdf']['producers']) != 5 or position > self.config['epoch_blocks'] - 3:
                    return False
                # Keep both epochs so a final already-built block can cross a
                # boundary while the producing processes shut down.
                for epoch in (row['vdf']['epoch'], row['vdf']['epoch'] + 1):
                    contexts = {}
                    for record in self.records.values():
                        info = Candidate(record)
                        if info['epoch'] == epoch:
                            key = (info['chain'], info['anchor'], info['iterations'])
                            contexts.setdefault(key, set()).add(info['public_key'])
                    if not any(set(self.public[:6]) <= keys for keys in contexts.values()):
                        return False
                return True
            self.Wait(complete, 'all six current and next VDF proofs captured before stopping')
        if capture:
            self.Capture()
        self.Snapshot('before_freeze')
        self.Stop(range(6))
        # A zero verification queue does not imply the block/fork commit loop
        # is idle. Graceful shutdown drains it; an isolated restart has a stable
        # durable chain and no delayed peer announcement can change the snapshot.
        self.Stop([6])
        self.Start([6], producers=(), groups=((6,),))
        return self.Stats(6)

    def Chain(self, index=6, name='canonical'):
        row = self.Stats(index)
        directory = self.work / name
        directory.mkdir(exist_ok=True)
        blocks = []
        for height in range(1, row['height'] + 1):
            self.Budget()
            data = Query(self.ports[index], 6, struct.pack('<Q', height), raw=True)
            block = Block(data)
            Require(block['height'] == height, 'Returned block height')
            if blocks:
                Require(block['parent'] == blocks[-1]['hash'], 'Canonical parent continuity')
            blocks.append(block)
            (directory / f'{height:06d}.bin').write_bytes(data)
        Require(blocks[-1]['hash'] == row['head'], 'Chain changed during evidence capture')
        return blocks

    def Converged(self):
        rows = [Query(self.ports[i], 35) for i in self.nodes]
        return rows[0] if all(row == rows[0] for row in rows) else False

    def AuditState(self, blocks, indices):
        expected = {key: [1000000, 0] for key in self.public}
        count = 0
        for block in blocks:
            for tx in block['transactions']:
                sender, receiver = tx[:32].hex(), tx[32:64].hex()
                amount, nonce = struct.unpack_from('<QQ', tx, 64)
                before, after = expected[sender], expected[receiver]
                Require(nonce == before[1] + 1 and before[0] >= amount + 1, 'Independent transaction execution')
                before[0] -= amount + 1
                before[1] = nonce
                after[0] += amount
                count += 1
        for index in indices:
            for key, (balance, nonce) in expected.items():
                actual = GetAccount(self.ports[index], key)
                Require(actual == {'exists': True, 'balance': balance, 'nonce': nonce}, 'Replica balances/nonces')
            stats = self.Stats(index)
            Require(stats['chain_transactions'] == count and stats['user_burned'] == count, 'Durable transaction/fee counts')
        return {'transactions': count, 'accounts': len(expected), 'burned': count}

    def Check(self, message):
        self.report['checks'].append(message)

    def Finish(self):
        snapshot_error = None
        try:
            self.Snapshot('final')
        except Exception as error:
            self.report['final_snapshot_error'] = repr(error)
            snapshot_error = error
        try:
            self.Stop()
        finally:
            Json(self.work / 'result.json', self.report)
        if snapshot_error is not None:
            raise snapshot_error


def Context(blocks, epoch, config):
    chain = blocks[0]['root']
    if epoch < 2:
        anchor = Hash(b'Chain/VdfConsensus/Bootstrap/v1' + chain + bytes.fromhex(blocks[0]['hash']))
    else:
        source = epoch - 2
        begin = 1 + source * config['epoch_blocks']
        selected = blocks[begin:begin + config['epoch_blocks']]
        Require(len(selected) == config['epoch_blocks'], 'Complete VDF anchor epoch')
        anchor = Hash(b'Chain/VdfConsensus/Anchor/v1' + chain + struct.pack('<QQ', source, len(selected))
                      + b''.join(bytes.fromhex(block['hash']) for block in selected))
    return {'chain': chain.hex(), 'epoch': epoch, 'anchor': anchor.hex(), 'iterations': config['iterations']}


def Matches(record, context, public_key=None):
    info = Candidate(record)
    return all(info[key] == value for key, value in context.items()) and (public_key is None or info['public_key'] == public_key)


def Election(case):
    case.Start()
    case.Ready(4)
    for i in range(6):
        SendFrame(case.ports[i], 1, Transaction(case.keys[i], bytes(case.keys[6].verify_key), i + 1))
    case.Wait(lambda: case.Stats(6)['chain_transactions'] == 6, 'native transfers committed')
    case.Freeze(capture=True)
    blocks = case.Chain()
    size = case.config['epoch_blocks']
    groups, fallback = {}, []
    for block in blocks[1:]:
        epoch = (block['height'] - 2) // size
        current = Context(blocks, epoch, case.config)
        parent = blocks[block['height'] - 2]
        Require(block['slot'] > parent.get('slot', 0), 'Strictly increasing slots')
        minimum = case.config['start_time_ms'] // case.config['slot_ms'] + block['height'] - 2
        Require(block['slot'] >= minimum, 'Genesis start time and height impose minimum slot')
        previous = parent.get('candidates', []) if (block['height'] - 2) % size else []
        if epoch:
            target = Context(blocks, epoch + 1, case.config)
            Require(all(Matches(record, target) for record in block['candidates']), 'Candidate context binds canonical anchor')
            ranking = [(Candidate(record)['score'], Candidate(record)['public_key']) for record in block['candidates']]
            Require(ranking == sorted(set(ranking)), 'Distinct candidates sorted by score/public key')
            combined = {Candidate(record)['public_key']: record for record in previous + block['candidates']}
            best = sorted(combined.values(), key=lambda r: (Candidate(r)['score'], Candidate(r)['public_key']))[:5]
            Require(best == block['candidates'], 'Rolling top five cannot discard a better previous candidate')
        else:
            Require(not block['candidates'], 'Bootstrap G0 has no next-epoch candidate list')
        active = blocks[epoch * size]['candidates'] if epoch >= 2 else []
        roster = [Candidate(record)['public_key'] for record in active]
        if epoch >= 2:
            Require(all(Matches(record, current) for record in active), 'Active roster context')
        member = block['producer'] in roster
        if epoch < 2 or not roster or not member:
            Require(block['proof'] is not None and Matches(block['proof'], current, block['producer']), 'Producer current proof')
        if epoch >= 2 and roster:
            if member:
                groups.setdefault(epoch, set()).add(block['producer'])
            else:
                gap = block['slot'] - parent['slot']
                Require(gap >= case.config['fallback_slots'], 'Nonmember requires fallback slot gap')
                fallback.append({'height': block['height'], 'gap': gap})
        Require(block['producer'] in case.public[:6], 'Observer never authors a block')
    Require(len(groups) >= 2 and all(len(authors) <= 5 for authors in groups.values()), 'Two elected epochs with at most five normal producers')
    for observer in (case.report['snapshots']['before_freeze']['6'], case.Stats(6)):
        Require(observer['local_proposals'] == 0 and observer['vdf']['computed'] == 0,
                'Sync-only node does no proving/production before and after restart')
    case.report['normal_producers_by_epoch'] = {str(k): sorted(v) for k, v in groups.items()}
    case.report['fallback_blocks'] = fallback
    case.report['state'] = case.AuditState(blocks, [6])
    case.Check('seven processes, bootstrap plus two elected epochs, on-chain top-five/context/signature checks')
    case.Check('sync-only observer computed zero proofs and proposed zero blocks; six native transfers verified')


def Rejection(case):
    case.Start()
    case.Ready(3)
    case.Freeze(require_all_proofs=True)
    blocks = case.Chain()
    parent = blocks[-1]
    height = parent['height'] + 1
    epoch = (height - 2) // case.config['epoch_blocks']
    context = Context(blocks, epoch, case.config)
    active = blocks[epoch * case.config['epoch_blocks']]['candidates']
    Require(len(active) == 5, 'Five active producers in rejection case')
    member = Candidate(active[0])['public_key']
    outside = next(key for key in case.public[:6] if key not in [Candidate(r)['public_key'] for r in active])
    proof = next((r for r in case.records.values() if Matches(r, context, outside)), None)
    Require(proof is not None, 'Captured current real VDF proof of nonselected sixth producer')
    previous = parent['candidates'] if (height - 2) % case.config['epoch_blocks'] else []
    member_key, outside_key = case.keys[case.public.index(member)], case.keys[case.public.index(outside)]
    # Freeze restarted the observer alone and cleared its candidate gossip set.
    # The cheap worst-score filter cannot hide invalid proof checks in this case.
    original = active[0]
    for name, offset, resign in [('candidate_signature', 318, False), ('candidate_witness', 218, True),
                                  ('candidate_context', 38, True)]:
        malformed = bytearray(original)
        malformed[offset] ^= 1 if name != 'candidate_context' else 128
        if resign:
            malformed[318:] = member_key.sign(Hash(b'Chain/VdfElection/Signature/v1' + malformed[:318])).signature
        (case.work / (name + '.bin')).write_bytes(malformed)
        before = case.Stats(6)['vdf']['invalid_proofs']
        SendFrame(case.ports[6], 49, malformed)
        case.Wait(lambda: case.Stats(6)['vdf']['invalid_proofs'] > before, name + ' rejected')
    SendFrame(case.ports[6], 49, original)
    case.Wait(lambda: original in Candidates(case.ports[6]), 'valid candidate accepted as positive control')
    before = case.Stats(6)['vdf']['skipped_proofs']
    SendFrame(case.ports[6], 49, original)
    case.Wait(lambda: case.Stats(6)['vdf']['skipped_proofs'] > before, 'duplicate candidate ignored')
    invalid = MakeBlock(bytes.fromhex(parent['hash']), height, [], parent['slot'] + 1, outside_key, previous, proof)
    (case.work / 'nonmember-before-fallback.bin').write_bytes(invalid)
    before = case.Stats(6)['invalid_user_blocks']
    SendFrame(case.ports[6], 7, invalid)
    case.Wait(lambda: case.Stats(6)['invalid_user_blocks'] > before, 'correctly signed nonmember block rejected')
    Require(case.Stats(6)['head'] == parent['hash'], 'Invalid block cannot change chain head')
    # An eligible member, identical parent/candidates/slot and a real transfer
    # make an accepted control. This distinguishes eligibility from malformed wire.
    tx = Transaction(member_key, bytes(case.keys[6].verify_key), 7)
    valid = MakeBlock(bytes.fromhex(parent['hash']), height, [tx], parent['slot'] + 1, member_key, previous)
    forged = bytearray(valid)
    forged[-1] ^= 1
    (case.work / 'block-signature.bin').write_bytes(forged)
    before = case.Stats(6)['block_tasks']
    SendFrame(case.ports[6], 7, forged)
    case.Wait(lambda: case.Stats(6)['block_tasks'] > before and case.Stats(6)['pending_block_verify'] == 0,
              'tampered block processed')
    time.sleep(.1)
    Require(case.Stats(6)['head'] == parent['hash'], 'Tampered signature cannot change chain head')
    SendFrame(case.ports[6], 7, valid)
    case.Wait(lambda: case.Stats(6)['head'] == valid[80:112].hex(), 'eligible block positive control')
    case.report['state'] = case.AuditState(case.Chain(name='accepted-control'), [6])
    case.Check('candidate signature, signed invalid VDF witness and wrong context rejected; valid/duplicate controls')
    case.Check('sixth identity with valid current proof rejected before fallback; valid member control committed')
    case.Check('tampered block signature rejected without changing the durable head')


def Restart(case):
    case.Start()
    case.Ready(2)
    SendFrame(case.ports[0], 1, Transaction(case.keys[0], bytes(case.keys[6].verify_key), 19))
    case.Wait(lambda: case.Stats(6)['chain_transactions'] == 1, 'transaction before restart')
    case.Freeze()
    expected = Query(case.ports[6], 35)
    case.Stop()
    case.Start(producers=())
    # A producer may have persisted one final block before termination. Let the
    # seven durable databases converge, then check state and every stored block.
    common = case.Wait(case.Converged, 'seven restarted databases converge')
    Require(all(common[key] == expected[key] for key in ('state_hash', 'transactions', 'supply', 'burned')),
            'Restart preserves account state and transaction score')
    blocks = case.Chain()
    case.report['state'] = case.AuditState(blocks, range(7))
    member = case.Stats(6)['vdf']['producers'][0]
    index = case.public.index(member)
    case.Stop([index])
    case.Start([index], producers=[index])
    case.Wait(lambda: case.Stats(6)['height'] >= blocks[-1]['height'] + 2, 'production resumes after restart')
    Require(case.Stats(index)['vdf']['public_key'] == member, 'VDF identity survives restart')
    case.Check('seven persistent databases recover and converge; independently executed balances and nonces agree')
    case.Check('same VDF identity resumes block production after restart')


def Partition(case):
    groups = ((0, 1, 2, 6), (3, 4, 5))
    case.Start(groups=groups)
    case.Wait(lambda: min(case.Stats(0)['vdf']['epoch'], case.Stats(3)['vdf']['epoch']) >= 2,
              'both disconnected groups cross election boundary')
    # Both transfers spend the same account/nonce with a different amount.
    SendFrame(case.ports[0], 1, Transaction(case.keys[0], bytes(case.keys[6].verify_key), 11))
    SendFrame(case.ports[3], 1, Transaction(case.keys[0], bytes(case.keys[6].verify_key), 22))
    case.Wait(lambda: case.Stats(0)['chain_transactions'] == 1 and case.Stats(3)['chain_transactions'] == 1,
              'conflicting transfers committed independently')
    case.Snapshot('partitioned')
    left, right = Query(case.ports[0], 35), Query(case.ports[3], 35)
    Require(left['state_hash'] != right['state_hash'] and left['head'] != right['head'], 'Actual fork with divergent native state')
    case.report['partition_states'] = [left, right]
    case.Stop()
    case.Start(producers=())
    common = case.Wait(case.Converged, 'partition reconnection and branch replay')
    Require(common['state_hash'] in (left['state_hash'], right['state_hash']), 'Selected branch state')
    Require(sum(case.Stats(i)['reorganizations'] for i in range(7)) > 0, 'At least one actual reorganization')
    case.report['state'] = case.AuditState(case.Chain(), range(7))
    case.Check('independent 3/3 producer groups crossed election boundary and committed conflicting native transfers')
    case.Check('reconnected seven nodes select one branch and replay identical balances, nonces and fee state')


def Fallback(case):
    case.Start()
    case.Ready(3)
    # Capture all real current proofs, then act near the start of an epoch so the
    # five stopped identities are the same set for the recovery block.
    def beginning():
        row = case.Stats(6)
        epoch, position = divmod(row['height'] - 1, case.config['epoch_blocks'])
        # Head and VDF stats are separate DB snapshots; reject a sample that
        # straddled an epoch boundary before selecting which processes to stop.
        return row if epoch == row['vdf']['epoch'] and epoch >= 3 and 1 <= position <= 2 and len(row['vdf']['producers']) == 5 else False
    row = case.Wait(beginning, 'early elected epoch')
    selected = row['vdf']['producers']
    stopped = [case.public.index(key) for key in selected]
    survivor = next(i for i in range(6) if i not in stopped)
    case.report['fallback_setup'] = {'selection_height': row['height'], 'selection_epoch': row['vdf']['epoch'],
                                     'selected_public_keys': selected, 'stopped_indices': stopped, 'survivor': survivor}
    case.Capture()
    processes = {i: case.nodes[i]['process'] for i in stopped}
    case.Stop(stopped)
    case.report['fallback_setup']['stopped_processes'] = [
        {'index': i, 'pid': process.pid, 'returncode': process.returncode} for i, process in processes.items()]
    before = case.Snapshot('after_stopping_five')['6']
    Require(before['vdf']['producers'] == selected, 'Roster did not change while stopping five processes')
    transaction = Transaction(case.keys[survivor], bytes(case.keys[6].verify_key), 31)
    case.report['recovery_transaction_hash'] = transaction[80:112].hex()
    SendFrame(case.ports[survivor], 1, transaction)
    case.Wait(lambda: case.Stats(6)['chain_transactions'] == 1, 'remaining sixth producer recovers after slot timeout')
    case.Stop([survivor])
    case.Stop([6])
    case.Start([6], producers=(), groups=((6,),))
    blocks = case.Chain()
    # A stopped producer's previously sent block can still be queued on the
    # survivor or observer after the stats snapshot. The transaction sent only
    # after all five processes exited identifies the actual recovery block.
    first = TransactionBlock(blocks, transaction)
    parent = blocks[first['height'] - 2]
    epoch = (first['height'] - 2) // case.config['epoch_blocks']
    active = blocks[epoch * case.config['epoch_blocks']]['candidates']
    roster = [Candidate(record)['public_key'] for record in active]
    gap = first['slot'] - parent['slot']
    case.report['recovery'] = {'stopped_indices': stopped, 'survivor': survivor, 'height': first['height'],
                               'producer': first['producer'], 'epoch': epoch,
                               'observer_height_after_stop': before['height'], 'parent_height': parent['height'],
                               'parent_slot': parent['slot'], 'slot': first['slot'], 'active_public_keys': roster,
                               'slot_gap': gap, 'required_slot_gap': case.config['fallback_slots']}
    Require(len(roster) == 5 and set(roster) == set(selected), 'Recovery roster is the five stopped identities')
    Require(first['producer'] == case.public[survivor], 'Recovery block was authored by the surviving sixth identity')
    Require(first['producer'] not in roster, 'Recovery author was not in the active five')
    Require(gap >= case.config['fallback_slots'], 'Recovery waited for required parent-slot gap')
    Require(first['proof'] is not None and Matches(first['proof'], Context(blocks, epoch, case.config), first['producer']),
            'Recovery block includes current-context proof')
    # Locating the transaction must not hide any earlier invalid nonmember
    # proposal, including an empty block before the recovery transaction arrived.
    for block in blocks[1:]:
        epoch = (block['height'] - 2) // case.config['epoch_blocks']
        active = blocks[epoch * case.config['epoch_blocks']]['candidates'] if epoch >= 2 else []
        if active and block['producer'] not in [Candidate(record)['public_key'] for record in active]:
            parent = blocks[block['height'] - 2]
            Require(block['slot'] - parent['slot'] >= case.config['fallback_slots'],
                    'Every nonmember block satisfies the fallback slot gap')
            Require(block['proof'] is not None and Matches(block['proof'], Context(blocks, epoch, case.config), block['producer']),
                    'Every nonmember block includes its current-context proof')
    case.report['state'] = case.AuditState(blocks, [6])
    case.Check('five selected real processes stopped; sixth identity recovered with current VDF after required slot gap')


CASES = {'election': Election, 'rejection': Rejection, 'restart': Restart, 'partition': Partition, 'fallback': Fallback}


def SelfTest():
    """Wire/signature checks only; real VDF verification is exercised by live nodes."""
    from unittest.mock import patch
    key = SigningKey(bytes(range(32)))
    tx = Transaction(key, bytes(key.verify_key), 3)
    encoded = MakeBlock(bytes(32), 2, [tx], 123, key)
    decoded = Block(encoded)
    Require(decoded['transactions'] == [tx] and decoded['slot'] == 123, 'Block codec round trip')
    for broken in (encoded + b'X', encoded[:-1], encoded[:32] + bytes(32) + encoded[64:]):
        try:
            Block(broken)
        except (AssertionError, BadSignatureError, ValueError):
            continue
        raise AssertionError('Malformed block unexpectedly decoded')
    unsigned = b'VFEL\x01\x00' + bytes(32) + struct.pack('<Q', 3) + bytes(32) + bytes(key.verify_key) + struct.pack('<Q', 4096) + bytes(200)
    record = unsigned + key.sign(Hash(b'Chain/VdfElection/Signature/v1' + unsigned)).signature
    Require(Candidate(record)['epoch'] == 3, 'Candidate field offsets')
    with_proof = Block(MakeBlock(bytes(32), 2, [], 124, key, [record], record))
    Require(with_proof['candidates'] == [record] and with_proof['proof'] == record, 'Candidate/proof extension offsets')
    # A member's delayed block may be the first height after an observer stats
    # snapshot. Only the post-stop transaction identifies the recovery block.
    delayed = Block(MakeBlock(bytes(32), 3, [], 125, SigningKey(bytes(reversed(range(32))))))
    recovery = Block(MakeBlock(bytes.fromhex(delayed['hash']), 4, [tx], 145, key, [record], record))
    Require(TransactionBlock([delayed, recovery], tx) is recovery, 'Delayed member block is not the recovery block')
    for history in ([delayed], [delayed, recovery, recovery]):
        try:
            TransactionBlock(history, tx)
        except AssertionError:
            continue
        raise AssertionError('Missing or duplicated recovery transaction was accepted')
    class FragmentedSocket:
        def __init__(self, data):
            self.data, self.sent = data, b''

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def sendall(self, data):
            self.sent += data

        def settimeout(self, _):
            pass

        def recv(self, size):
            size = min(size, 7)
            data, self.data = self.data[:size], self.data[size:]
            return data

    for count in (0, 1, 12):
        connection = FragmentedSocket(Frame(49, record) * count + Frame(36, b'{}'))
        with patch('socket.create_connection', return_value=connection):
            Require(Candidates(1) == [record] * count, 'Fragmented candidate response boundary')
        Require(connection.sent == b'\x02' + Frame(50) + Frame(35), 'Ordered candidate/state requests')
    expired = Case.__new__(Case)
    expired.deadline, expired.timeout = time.monotonic() - 1, 90
    try:
        expired.Wait(lambda: True, 'expired total budget')
    except TimeoutError:
        pass
    else:
        raise AssertionError('Expired suite budget was ignored')
    print(json.dumps({'status': 'passed', 'checks': 14, 'scope': 'Python wire codec, signatures, recovery selection, response framing and time budget'}))


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin', type=Path)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--case', action='append', choices=CASES)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--budget', type=float, default=480,
                        help='total suite seconds; leave cleanup time below the outer CTest timeout')
    parser.add_argument('--epoch-blocks', type=int, default=8)
    parser.add_argument('--slot-ms', type=int, default=250)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        SelfTest()
        return
    if args.bin is None or args.work is None:
        parser.error('--bin and --work are required for live process tests')
    if args.epoch_blocks < 4 or not 100 <= args.slot_ms <= 1000 or args.timeout <= 0 or args.budget <= 0:
        parser.error('use epoch-blocks >= 4, slot-ms 100..1000 and positive timeout/budget')
    binary, work = args.bin.resolve(), args.work.resolve()
    Require((binary / ExeName('boost')).is_file(), 'Node binary missing')
    work.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'scope': 'seven-process VDF protocol correctness',
              'budget_seconds': args.budget, 'cases': []}
    started = time.monotonic()
    try:
        for name in args.case or CASES:
            case = Case(binary, work, name, args.timeout, args.epoch_blocks, args.slot_ms, started + args.budget)
            try:
                CASES[name](case)
                case.report['status'] = 'passed'
            except BaseException as error:
                case.report.update(status='failed', error=repr(error), traceback=traceback.format_exc())
                raise
            finally:
                try:
                    case.Finish()
                except BaseException as error:
                    case.report.update(status='failed', cleanup_error=repr(error))
                    Json(case.work / 'result.json', case.report)
                    raise
                finally:
                    report['cases'].append({'case': name, 'status': case.report['status'], 'result': name + '/result.json'})
            print(json.dumps({'case': name, 'status': case.report['status']}), flush=True)
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        report['elapsed_seconds'] = time.monotonic() - started
        Json(work / 'result.json', report)


if __name__ == '__main__':
    Main()
