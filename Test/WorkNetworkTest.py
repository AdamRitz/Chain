"""Four-node Work election regression with four real historical proof groups.

The test keeps 200 ms slots and uses a 30-block test round, four groups of one
Work, and 4096 iterations. Nodes 0 and 1 form the initial committee. Node 3
computes Work and joins by sending its ticket; node 2 only verifies the chain.
Every run uses a new directory, loopback ports and public test identities.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time
import traceback

from nacl.signing import SigningKey, VerifyKey

from NetworkTest import ExeName, FLAGS, SendFrame
from VdfNetworkTest import Case, Hash, Json, Merkle, Query, Require, Transaction


def Ticket(data):
    Require(len(data) >= 234 and data[:6] == b'WTKT\x01\x00', 'Work ticket header')
    epoch, = struct.unpack_from('<Q', data, 38)
    mature, iterations, groups, group_size = struct.unpack_from('<QQII', data, 110)
    public = data[134:166]
    count, = struct.unpack_from('<I', data, 166)
    Require(0 < count <= 256 and len(data) == 234 + count * 340, 'Work ticket exact length')
    VerifyKey(public).verify(Hash(b'Chain/WorkElection/TicketSignature/v1' + data[:-64]), data[-64:])
    works = []
    for index in range(count):
        offset = 170 + index * 340
        round_number, = struct.unpack_from('<Q', data, offset)
        works.append({'round': round_number, 'anchor': data[offset + 8:offset + 40].hex()})
    score = Hash(b'Chain/WorkElection/Ticket/v1' + data[6:38] + data[38:46] + data[78:110] + public)
    return {'chain': data[6:38].hex(), 'epoch': epoch, 'anchor': data[46:78].hex(),
            'seed': data[78:110].hex(), 'mature': mature, 'iterations': iterations,
            'groups': groups, 'group_size': group_size, 'public_key': public.hex(),
            'score': score.hex(), 'works': works}


def Delta(data):
    Require(len(data) >= 5 and data[0] == 1, 'Work delta version')
    commits, reveals, tickets, final = data[1:5]
    Require(commits <= 5 and reveals <= 5 and tickets <= 10 and final <= 1, 'Work delta counts')
    offset, records = 5, []
    for kind, count in (('commit', commits), ('reveal', reveals), ('ticket', tickets)):
        for _ in range(count):
            Require(offset + 4 <= len(data), 'Work record length available')
            length, = struct.unpack_from('<I', data, offset)
            offset += 4
            Require(offset + length <= len(data), 'Complete Work record')
            record = data[offset:offset + length]
            offset += length
            if kind == 'ticket':
                records.append(Ticket(record))
            else:
                Require(len(record) == 206, 'Beacon record exact length')
                domain = b'Chain/WorkElection/' + (b'CommitSignature/v1' if kind == 'commit' else b'RevealSignature/v1')
                VerifyKey(record[78:110]).verify(Hash(domain + record[:-64]), record[-64:])
    result = {'commits': commits, 'reveals': reveals, 'tickets': records, 'finalization': None}
    if final:
        Require(offset + 34 <= len(data), 'Complete finalization header')
        seed = data[offset:offset + 32].hex()
        missing, values = data[offset + 32:offset + 34]
        Require(1 <= values <= 5 and missing < (1 << values), 'Finalization reveal count')
        offset += 34 + values * 32
        Require(offset < len(data), 'Finalization committee count available')
        members = data[offset]
        offset += 1
        Require(1 <= members <= 5 and offset + members * 32 <= len(data), 'Finalization committee size')
        committee = [data[offset + i * 32:offset + (i + 1) * 32].hex() for i in range(members)]
        offset += members * 32
        result['finalization'] = {'seed': seed, 'committee': committee, 'missing': missing}
    Require(offset == len(data), 'No trailing Work delta bytes')
    return result


def Block(data):
    Require(len(data) >= 112 and data[80:112] == Hash(data[:80]), 'Block header hash')
    height, count = struct.unpack_from('<QQ', data, 64)
    offset = 112 + count * 176
    Require(offset <= len(data), 'Complete native transfers')
    transactions = [data[112 + i * 176:112 + (i + 1) * 176] for i in range(count)]
    for transaction in transactions:
        Require(Hash(transaction[:80]) == transaction[80:112], 'Transfer hash')
        VerifyKey(transaction[:32]).verify(transaction[80:112], transaction[112:])
    result = {'height': height, 'hash': data[80:112].hex(), 'parent': data[:32].hex(),
              'transactions': transactions, 'root': data[32:64]}
    if height == 1:
        Require(count == 0 and offset == len(data), 'Genesis has no Work extension')
        return result
    tail = data[offset:]
    Require(len(tail) >= 136 and tail[:4] == b'VDB1', 'Work extension boundary')
    size, version, slot = struct.unpack_from('<IHQ', tail, 4)
    Require(size == len(tail) - 8 and version == 3 and tail[50:52] == b'\x00\x00', 'Work extension header')
    delta_size, = struct.unpack_from('<I', tail, 68)
    Require(len(tail) == 136 + delta_size, 'Work extension exact length')
    public = tail[18:50]
    VerifyKey(public).verify(data[80:112], tail[-64:])
    Require(Hash(b'Chain/VdfConsensus/BlockRoot/v1' + Merkle(transactions) + tail[8:-64]) == data[32:64],
            'Work extension bound to block root')
    number, completed = struct.unpack_from('<QQ', tail, 52)
    result.update(slot=slot, producer=public.hex(), turn_number=number, turn_blocks=completed,
                  delta=Delta(tail[72:-64]))
    return result


class WorkCase(Case):
    def __init__(self, binary, work, timeout, deadline):
        self.binary, self.work, self.timeout, self.deadline = binary, work, timeout, deadline
        self.config = {'enabled': True, 'producer_rotation': True, 'epoch_blocks': 30,
                       'consecutive_blocks': 6, 'producer_timeout_slots': 8,
                       'iterations': 4096, 'slot_ms': 200,
                       'start_time_ms': (int(time.time() * 1000) // 200 + 15) * 200,
                       'fallback_slots': 20, 'future_slots': 2, 'producers': 5,
                       'work_election': True, 'work_iterations': 4096, 'work_groups': 4,
                       'work_group_size': 1, 'commit_start_slot': 2, 'commit_end_slot': 5,
                       'reveal_end_slot': 8, 'ticket_end_slot': 20, 'proof_end_slot': 26}
        self.keys = [SigningKey(Hash(f'Chain/WorkNetwork/test-only/{i}'.encode())) for i in range(4)]
        self.public = [bytes(key.verify_key).hex() for key in self.keys]
        self.config['bootstrap_producers'] = self.public[:2]
        self.genesis = {'base_fee': 1, 'accounts': [{'public_key': key, 'balance': 1000000}
                                                  for key in self.public], 'vdf': self.config}
        Json(work / 'genesis.json', self.genesis)
        self.nodes, self.generations = {}, [0] * len(self.keys)
        self.report = {'case': 'work-network', 'status': 'running', 'process_count': len(self.keys),
                       'config': self.config, 'public_keys': self.public, 'checks': [],
                       'starts': [], 'stops': [], 'snapshots': {}}
        sockets = [socket.socket() for _ in self.keys]
        try:
            for sock in sockets:
                sock.bind(('127.0.0.1', 0))
            self.ports = [sock.getsockname()[1] for sock in sockets]
        finally:
            for sock in sockets:
                sock.close()
        for index, key in enumerate(self.keys):
            directory = work / f'node{index}'
            directory.mkdir()
            descriptor = os.open(directory / 'identity.seed', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='ascii') as output:
                output.write(bytes(key).hex() + '\n')

    def Start(self, producers=(0, 1, 3)):
        for index in range(len(self.keys)):
            self.Budget()
            Require(index not in self.nodes, 'Only one process may open a node database')
            generation = self.generations[index]
            self.generations[index] += 1
            directory = self.work / f'node{index}'
            config = directory / f'config-{generation}.yaml'
            peers = [f'127.0.0.1:{self.ports[other]}' for other in range(len(self.keys)) if other != index]
            config.write_text('node: ' + json.dumps({'peers': peers}) + '\n', encoding='utf-8')
            stop = directory / f'stop-{generation}'
            metrics = directory / f'final-{generation}.json'
            options = {'config': config, 'data': directory / 'db', 'genesis': self.work / 'genesis.json',
                       'vdf-key': directory / 'identity.seed', 'bind': '127.0.0.1', 'port': self.ports[index],
                       'produce': int(index in producers), 'sync': 1, 'io-threads': 1, 'verify-threads': 1,
                       'candidate-threads': 1, 'block-verify-threads': 1, 'query-threads': 1,
                       'max-block-txs': 64, 'max-peers': 16, 'relay-transactions': 1,
                       'clock-sync': 0, 'run-seconds': 180, 'experiment-index': 1,
                       'stop-file': stop, 'metrics': metrics}
            command = [str(self.binary / ExeName('boost'))]
            for key, value in options.items():
                command.extend(['--' + key, str(value)])
            log = (directory / f'node-{generation}.log').open('w', encoding='utf-8')
            try:
                process = subprocess.Popen(command, cwd=directory, stdout=log, stderr=log, creationflags=FLAGS)
            except BaseException:
                log.close()
                raise
            self.nodes[index] = {'process': process, 'log': log, 'stop': stop, 'metrics': metrics}
            self.report['starts'].append({'index': index, 'generation': generation, 'pid': process.pid,
                                          'produces': index in producers, 'peers': peers})
        self.Wait(lambda: all(self.Stats(i)['vdf']['work_election'] for i in range(len(self.keys))), 'Work node startup')

    def Stop(self, indices=None):
        indices = list(self.nodes if indices is None else indices)
        for index in indices:
            self.nodes[index]['stop'].touch()
        end, errors = time.monotonic() + 15, []
        for index in indices:
            node = self.nodes[index]
            try:
                result = node['process'].wait(timeout=max(.01, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                node['process'].kill()
                node['process'].wait(timeout=5)
                result = node['process'].returncode
                errors.append(f'node{index} required kill')
            node['log'].close()
            del self.nodes[index]
            metrics = json.loads(node['metrics'].read_text(encoding='utf-8')) if node['metrics'].is_file() else None
            self.report['stops'].append({'index': index, 'exit_code': result, 'metrics': metrics})
            if result != 0 or not metrics or metrics.get('failed'):
                errors.append(f'node{index} did not exit successfully with metrics')
            elif any(metrics['vdf'][key] != 0 for key in ('pending_proofs', 'pending_beacons', 'pending_bytes')):
                errors.append(f'node{index} retained queued Work messages after stopping')
        if errors:
            raise RuntimeError('; '.join(errors))

    def Chain(self):
        row = self.Stats(2)
        directory = self.work / 'canonical'
        directory.mkdir()
        blocks = []
        for height in range(1, row['height'] + 1):
            self.Budget()
            raw = Query(self.ports[2], 6, struct.pack('<Q', height), raw=True)
            block = Block(raw)
            Require(block['height'] == height, 'Returned block height')
            if blocks:
                Require(block['parent'] == blocks[-1]['hash'], 'Canonical parent continuity')
            blocks.append(block)
            (directory / f'{height:06d}.bin').write_bytes(raw)
        Require(blocks[-1]['hash'] == row['head'], 'Stable chain during evidence collection')
        return blocks


def Audit(case, blocks):
    size, chain = case.config['epoch_blocks'], blocks[0]['root']
    committee = case.public[:2]
    pool, active_epoch, finalization = {}, -1, None
    tickets, full_groups, elections, adopted = 0, 0, 0, 0
    first_nonbootstrap_ticket = None
    for parent, block in zip(blocks, blocks[1:]):
        epoch = (block['height'] - 2) // size
        Require(block['slot'] > parent.get('slot', 0), 'Canonical slots increase')
        if epoch != active_epoch:
            if finalization:
                committee = finalization['committee']
                adopted += 1
            pool, finalization, active_epoch = {}, None, epoch
        Require(block['producer'] in committee, 'Block signed by the active elected committee')
        for ticket in block['delta']['tickets']:
            Require(ticket['chain'] == chain.hex() and ticket['epoch'] == epoch + 1,
                    'Ticket binds chain and target round')
            Require(ticket['anchor'] == blocks[epoch * size]['hash'], 'Ticket binds current round parent')
            Require(ticket['iterations'] == 4096 and ticket['groups'] == 4 and ticket['group_size'] == 1,
                    'Ticket uses test Work parameters')
            Require(ticket['mature'] == epoch - 1 and ticket['public_key'] not in pool,
                    'Ticket historical range and distinct identity')
            Require(len(ticket['works']) == min(4, ticket['mature']), 'Expected number of historical proof groups')
            rounds = [work['round'] for work in ticket['works']]
            Require(rounds == sorted(set(rounds)), 'Distinct sampled rounds')
            for work in ticket['works']:
                round_number = work['round']
                Require(round_number < ticket['mature'], 'Work comes from a mature round')
                history = blocks[1 + round_number * size:1 + (round_number + 1) * size]
                anchor = Hash(b'Chain/VdfConsensus/Anchor/v1' + chain + struct.pack('<QQ', round_number, size)
                              + b''.join(bytes.fromhex(item['hash']) for item in history)).hex()
                Require(len(history) == size and work['anchor'] == anchor, 'Proof historical anchor matches the chain')
            pool[ticket['public_key']] = ticket
            tickets += 1
            full_groups += len(ticket['works']) == 4
            if ticket['public_key'] == case.public[3] and first_nonbootstrap_ticket is None:
                Require(ticket['public_key'] not in committee and block['producer'] != ticket['public_key'],
                        'Joining node sends its first ticket before it can produce a block')
                first_nonbootstrap_ticket = {'height': block['height'], 'hash': block['hash'],
                    'epoch': epoch, 'target_epoch': ticket['epoch'], 'producer': block['producer'],
                    'public_key': ticket['public_key'], 'committee': list(committee)}
        record = block['delta']['finalization']
        if record:
            Require(finalization is None, 'One finalization per round')
            ranked = sorted(pool.values(), key=lambda ticket: (ticket['score'], ticket['public_key']))[:5]
            expected = [ticket['public_key'] for ticket in ranked] or committee
            Require(record['committee'] == expected, 'Finalization uses the lowest verified ticket scores')
            Require(all(ticket['seed'] == record['seed'] for ticket in pool.values()), 'One public seed per election')
            finalization = record
            elections += bool(ranked)
    Require(tickets >= 2 and full_groups >= 1 and elections >= 1 and adopted >= 1,
            'Historical tickets, four-group proof, election and following committee are present')
    Require(first_nonbootstrap_ticket is not None,
            'Current committee includes the first ticket received from the joining node')
    return {'tickets': tickets, 'four_group_tickets': full_groups, 'elections_with_tickets': elections,
            'adopted_committees': adopted, 'blocks': len(blocks),
            'first_nonbootstrap_ticket': first_nonbootstrap_ticket}


def Exercise(case):
    case.Start()
    for index in range(2):
        SendFrame(case.ports[index], 1, Transaction(case.keys[index], bytes(case.keys[2].verify_key), 7 + index))

    def elected():
        rows = [case.Stats(i) for i in range(len(case.keys))]
        observer = rows[2]['vdf']
        # A ticket can be verified in a block before its separate message arrives.
        # Audit proves the joining node sent its first ticket to another producer.
        return rows if (all(row['vdf']['epoch'] >= 6 and row['chain_transactions'] == 2 for row in rows)
                        and observer['work_ticket_cache_size'] >= 2
                        and case.public[3] in observer['producers']) else False

    rows = case.Wait(elected, 'network proof verification, four-group history and elected next round')
    Require(all(row['vdf']['work_verify_threads'] == 2 for row in rows), 'Two proof workers configured on each node')
    Require(rows[2]['vdf']['work_computed'] == 0, 'Observer only verifies received Work tickets')
    case.report['snapshots']['before_stop'] = {str(i): row for i, row in enumerate(rows)}
    case.Check('observer verifies historical tickets and the joining node enters the committee')
    case.Stop()
    case.Start(producers=())
    case.Wait(case.Converged, 'four persistent nodes converge after production stops', timeout=20)
    blocks = case.Chain()
    case.report['audit'] = Audit(case, blocks)
    case.report['accounts'] = case.AuditState(blocks, range(len(case.keys)))
    case.Check('a current committee member first includes the joining node ticket received over the network')
    case.Check('four historical proof groups are written in signed blocks and elect the following committee')
    case.Check('four restarted nodes share the canonical chain, balances and nonces')


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--budget', type=float, default=120)
    args = parser.parse_args()
    if args.timeout <= 0 or args.budget <= 0:
        parser.error('--timeout and --budget must be positive')
    binary, work = args.bin.resolve(), args.work.resolve()
    Require((binary / ExeName('boost')).is_file(), 'Node binary missing')
    work.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    case = WorkCase(binary, work, args.timeout, started + args.budget)
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
            Json(case.work / 'result.json', case.report)
            raise
    print(json.dumps({'status': case.report['status'], 'result': str(case.work / 'result.json'),
                      'elapsed_seconds': case.report['elapsed_seconds'], 'audit': case.report['audit']}))


if __name__ == '__main__':
    Main()
