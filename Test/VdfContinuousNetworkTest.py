"""Seven-process tests for five producers taking consecutive-block turns.

Uses fresh databases and deterministic test identities. The 32-block case runs
a complete elected epoch; the smaller case exercises timeouts and signed blocks.
"""
import argparse
from pathlib import Path
import struct
import time
import traceback

from nacl.signing import VerifyKey

from NetworkTest import ExeName, SendFrame
from VdfNetworkTest import (Candidate, Case, Context, Hash, Json, Matches, Merkle,
                            Query, Require, Transaction, TransactionBlock)


def Unsigned(slot, public_key, candidates, proof, turn_number, turn_blocks):
    Require(len(candidates) <= 5, 'At most five candidate records')
    return (struct.pack('<HQ', 2, slot) + public_key
            + bytes((len(candidates), int(proof is not None)))
            + struct.pack('<QQ', turn_number, turn_blocks)
            + b''.join(candidates) + (proof or b''))


def MakeBlock(parent, height, transactions, slot, key, candidates=(), proof=None,
              turn_number=0, turn_blocks=0):
    unsigned = Unsigned(slot, bytes(key.verify_key), candidates, proof, turn_number, turn_blocks)
    root = Hash(b'Chain/VdfConsensus/BlockRoot/v1' + Merkle(transactions) + unsigned)
    header = parent + root + struct.pack('<QQ', height, len(transactions))
    digest = Hash(header)
    tail = unsigned + key.sign(digest).signature
    return header + digest + b''.join(transactions) + b'VDB1' + struct.pack('<I', len(tail)) + tail


def Block(data):
    Require(len(data) >= 112 and data[80:112] == Hash(data[:80]), 'Block header hash')
    height, count = struct.unpack_from('<QQ', data, 64)
    offset = 112 + count * 176
    Require(offset <= len(data), 'Truncated native transactions')
    transactions = [data[112 + i * 176:112 + (i + 1) * 176] for i in range(count)]
    for tx in transactions:
        Require(Hash(tx[:80]) == tx[80:112], 'Transaction hash')
        VerifyKey(tx[:32]).verify(tx[80:112], tx[112:])
    result = {'height': height, 'hash': data[80:112].hex(), 'parent': data[:32].hex(),
              'transactions': transactions, 'root': data[32:64], 'raw': data}
    if height == 1:
        Require(offset == len(data) and count == 0, 'Genesis has no VDF extension')
        return result
    tail = data[offset:]
    Require(len(tail) >= 132 and tail[:4] == b'VDB1', 'VDF extension at transaction boundary')
    size = struct.unpack_from('<I', tail, 4)[0]
    version, slot = struct.unpack_from('<HQ', tail, 8)
    count, has_proof = tail[50:52]
    Require(version == 2 and count <= 5 and has_proof <= 1 and size == len(tail) - 8,
            'VDF consecutive-turn extension fields')
    Require(len(tail) == 132 + 382 * (count + has_proof), 'VDF extension exact length')
    public_key = tail[18:50]
    turn_number, turn_blocks = struct.unpack_from('<QQ', tail, 52)
    VerifyKey(public_key).verify(data[80:112], tail[-64:])
    Require(Hash(b'Chain/VdfConsensus/BlockRoot/v1' + Merkle(transactions) + tail[8:-64]) == data[32:64],
            'Signed turn and candidate fields are bound to the block root')
    records = [tail[68 + i * 382:68 + (i + 1) * 382] for i in range(count + has_proof)]
    for record in records:
        Candidate(record)
    result.update(slot=slot, producer=public_key.hex(), candidates=records[:count],
                  proof=records[-1] if has_proof else None,
                  turn_number=turn_number, turn_blocks=turn_blocks)
    return result


def Epoch(height, config):
    return (height - 2) // config['epoch_blocks']


def Roster(blocks, height, config):
    epoch = Epoch(height, config)
    Require(epoch >= 2, 'Turn checks require an elected epoch')
    records = blocks[epoch * config['epoch_blocks']]['candidates']
    Require(len(records) == 5, 'Five elected producers')
    ranks = [(Candidate(record)['score'], Candidate(record)['public_key']) for record in records]
    Require(ranks == sorted(set(ranks)), 'Producer list is sorted by VDF score and public key')
    return [public for _, public in ranks]


def Turn(parent, height, slot, config):
    Require(slot > parent['slot'], 'Turn slot strictly follows its parent')
    number, completed = (parent['turn_number'], parent['turn_blocks']) if (
        Epoch(parent['height'], config) == Epoch(height, config)) else (0, 0)
    if completed == config['consecutive_blocks']:
        number, completed = number + 1, 0
    skipped = (slot - parent['slot'] - 1) // config['producer_timeout_slots']
    return number + skipped, 1 if skipped else completed + 1


def NextFields(blocks, parent, slot, config):
    height = parent['height'] + 1
    roster = Roster(blocks, height, config)
    previous = parent['candidates'] if (height - 2) % config['epoch_blocks'] else []
    number, completed = Turn(parent, height, slot, config)
    return height, roster, previous, number, completed


def Audit(blocks, config):
    epochs, recovery = {}, []
    for parent, block in zip(blocks, blocks[1:]):
        Require(block['slot'] > parent.get('slot', 0), 'Canonical slots strictly increase')
        epoch = Epoch(block['height'], config)
        if epoch < 2:
            Require((block['turn_number'], block['turn_blocks']) == (0, 0), 'Bootstrap turn is zero')
            continue
        roster = Roster(blocks, block['height'], config)
        number, completed = Turn(parent, block['height'], block['slot'], config)
        if block['producer'] in roster:
            Require(block['producer'] == roster[number % len(roster)], 'Scheduled turn producer')
            Require((block['turn_number'], block['turn_blocks']) == (number, completed),
                    'Turn counters follow the canonical parent')
        else:
            gap = block['slot'] - parent['slot']
            Require(gap >= max(config['fallback_slots'], config['producer_timeout_slots'] * len(roster) + 1),
                    'Outsider waits through all elected producers')
            Require((block['turn_number'], block['turn_blocks']) == (number, 0), 'Recovery resets completed blocks')
            Require(block['proof'] and Matches(block['proof'], Context(blocks, epoch, config), block['producer']),
                    'Recovery producer has a current VDF proof')
            recovery.append(block['height'])
        epochs.setdefault(epoch, []).append(block)
    Require(epochs, 'At least one elected epoch audited')
    return {'blocks': sum(map(len, epochs.values())), 'epochs': sorted(epochs), 'recovery_blocks': recovery}


class ContinuousCase(Case):
    def __init__(self, binary, work, name, timeout, consecutive, slot_ms, deadline):
        super().__init__(binary, work, name, timeout, consecutive * 5, slot_ms, deadline)
        self.config.update(producer_rotation=True, consecutive_blocks=consecutive, producer_timeout_slots=8)
        Json(self.work / 'genesis.json', self.genesis)

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
        Require(blocks[-1]['hash'] == row['head'], 'Chain stayed fixed while collecting evidence')
        return blocks

    def Head(self):
        row = self.Stats(6)
        return Block(Query(self.ports[6], 6, struct.pack('<Q', row['height']), raw=True))


def FreezeAndConverge(case, name):
    case.Stop()
    case.Start(producers=())
    case.Wait(case.Converged, name + ' persistent replicas converge')
    blocks = case.Chain(name=name)
    case.AuditState(blocks, range(7))
    return blocks


def FullTurns(case):
    case.Start()
    case.Ready(3)
    Require(all(case.Stats(i)['vdf']['consecutive_blocks'] == 32 and
                case.Stats(i)['vdf']['producer_timeout_slots'] == 8 for i in range(7)),
            'All seven processes loaded the 32-block and eight-slot configuration')
    for index in range(6):
        SendFrame(case.ports[index], 1, Transaction(case.keys[index], bytes(case.keys[6].verify_key), index + 1))
    case.Wait(lambda: case.Stats(6)['chain_transactions'] == 6, 'six transfers committed')
    blocks = FreezeAndConverge(case, 'complete-32-block-turns')
    size = case.config['epoch_blocks']
    elected = blocks[2 + 2 * size - 1:2 + 3 * size - 1]
    Require(len(elected) == 160, 'A complete 160-block elected epoch')
    roster = Roster(blocks, elected[0]['height'], case.config)
    for offset, block in enumerate(elected):
        number, completed = divmod(offset, 32)
        Require((block['producer'], block['turn_number'], block['turn_blocks']) ==
                (roster[number], number, completed + 1),
                f'Five producers each create 32 consecutive blocks, height {block["height"]}')
    case.report['audit'] = Audit(blocks, case.config)
    case.report['complete_epoch'] = {'epoch': 2, 'blocks': 160, 'producers': roster, 'blocks_per_producer': 32}
    case.report['state'] = case.AuditState(blocks, range(7))
    case.Check('one elected epoch contains five consecutive 32-block turns in score order')
    case.Check('seven replicas synchronize after restart; balances, nonces and six transfers agree')


def StopCurrent(case, label):
    def beginning():
        block = case.Head()
        position = (block['height'] - 2) % case.config['epoch_blocks']
        return block if (Epoch(block['height'], case.config) >= 2 and block['turn_blocks'] == 1 and
                         position < case.config['epoch_blocks'] - 5) else False
    before = case.Wait(beginning, label + ' producer has just begun its turn')
    victim = case.public.index(before['producer'])
    case.Stop([victim])
    # Drain a block already sent before termination, without consuming an empty-slot timeout.
    time.sleep(.04)
    parent = case.Head()
    Require(parent['producer'] == before['producer'] and parent['turn_blocks'] < case.config['consecutive_blocks'],
            'Stopped producer still owns an incomplete turn')
    case.Wait(lambda: case.Stats(6)['height'] > parent['height'], label + ' next producer takes over')
    first = Block(Query(case.ports[6], 6, struct.pack('<Q', parent['height'] + 1), raw=True))
    gap = first['slot'] - parent['slot']
    Require(case.config['producer_timeout_slots'] + 1 <= gap <= 2 * case.config['producer_timeout_slots'],
            'Next producer takes over after eight complete empty slots')
    Require(first['turn_number'] == parent['turn_number'] + 1 and first['turn_blocks'] == 1,
            'Timeout moves exactly one producer forward and resets its block count')
    Require(first['producer'] != parent['producer'], 'Timeout producer differs from the stopped producer')
    case.report.setdefault('offline', []).append({'label': label, 'producer_index': victim,
        'parent_height': parent['height'], 'parent_slot': parent['slot'], 'takeover_height': first['height'],
        'takeover_slot': first['slot'], 'gap': gap, 'turn_number': first['turn_number']})
    return victim, first


def Outages(case):
    case.Start()
    case.Ready(3)
    transaction = Transaction(case.keys[6], bytes(case.keys[0].verify_key), 23)
    SendFrame(case.ports[0], 1, transaction)
    case.Wait(lambda: case.Stats(6)['chain_transactions'] == 1, 'transfer before two stopped producers')
    first, takeover = StopCurrent(case, 'first offline')
    # The takeover's first block is the next producer's first completed block.
    # Stop it immediately, leaving a second consecutive missing producer.
    second = case.public.index(takeover['producer'])
    case.Stop([second])
    time.sleep(.04)
    parent = case.Head()
    Require(parent['producer'] == takeover['producer'] and parent['turn_blocks'] < case.config['consecutive_blocks'],
            'Second stopped producer still owns an incomplete turn')
    case.Wait(lambda: case.Stats(6)['height'] > parent['height'], 'second offline producer skipped')
    successor = Block(Query(case.ports[6], 6, struct.pack('<Q', parent['height'] + 1), raw=True))
    gap = successor['slot'] - parent['slot']
    Require(9 <= gap <= 16 and successor['turn_number'] == parent['turn_number'] + 1 and
            successor['turn_blocks'] == 1 and successor['producer'] not in (case.public[first], case.public[second]),
            'Two successive failed producers advance to the next available elected producer')
    case.report['offline'].append({'label': 'second offline', 'producer_index': second,
        'parent_height': parent['height'], 'parent_slot': parent['slot'], 'takeover_height': successor['height'],
        'takeover_slot': successor['slot'], 'gap': gap, 'turn_number': successor['turn_number']})
    blocks = FreezeAndConverge(case, 'after-two-offline')
    TransactionBlock(blocks, transaction)
    case.report['offline_audit'] = Audit(blocks, case.config)
    case.Check('two consecutive producers stopped; each successor starts after eight empty slots')
    case.Check('restarted offline databases synchronize; all seven account states agree')
    case.Stop()
    case.Start([6], producers=(), groups=((6,),))
    return case.Chain(name='before-injection')


def SendRejected(case, parent, data, name):
    (case.work / (name + '.bin')).write_bytes(data)
    def rejected():
        row = case.Stats(6)
        return row['invalid_user_blocks'] + row['fork_invalid_candidates']
    before = rejected()
    SendFrame(case.ports[6], 7, data)
    case.Wait(lambda: rejected() > before, name + ' rejected')
    Require(case.Stats(6)['head'] == parent['hash'], name + ' preserves the canonical head')


def FutureBlock(case, blocks, parent, transactions, name):
    slot_ms = case.config['slot_ms']
    case.Wait(lambda: int(time.time() * 1000) % slot_ms < slot_ms // 5, 'early slot for ' + name)
    slot = int(time.time() * 1000) // slot_ms + 2
    height, roster, previous, number, completed = NextFields(blocks, parent, slot, case.config)
    key = case.keys[case.public.index(roster[number % len(roster)])]
    data = MakeBlock(bytes.fromhex(parent['hash']), height, transactions, slot, key, previous,
                     turn_number=number, turn_blocks=completed)
    (case.work / (name + '.bin')).write_bytes(data)
    before = case.Stats(6)['head']
    SendFrame(case.ports[6], 7, data)
    observations = 0
    while time.time() * 1000 < slot * slot_ms - 20:
        row = case.Stats(6)
        if time.time() * 1000 < slot * slot_ms - 20:
            Require(row['head'] == before, name + ' cannot commit before its slot')
            observations += 1
        time.sleep(.01)
    Require(observations >= 2, 'At least two checks before the future slot')
    case.Wait(lambda: case.Stats(6)['head'] == data[80:112].hex(), name + ' automatically committed at its slot')
    case.report.setdefault('future_blocks', []).append({'name': name, 'height': height, 'slot': slot,
        'pre_slot_checks': observations, 'turn_number': number, 'turn_blocks': completed})
    return Block(data)


def Injections(case, blocks):
    parent = blocks[-1]
    slot = max(int(time.time() * 1000) // case.config['slot_ms'], parent['slot'] + 1)
    height, roster, previous, number, completed = NextFields(blocks, parent, slot, case.config)
    key = case.keys[case.public.index(roster[number % len(roster)])]
    wrong = case.keys[case.public.index(roster[(number + 1) % len(roster)])]
    tx = Transaction(case.keys[6], bytes(case.keys[0].verify_key), 3, nonce=2)
    common = (bytes.fromhex(parent['hash']), height, [tx], slot)
    SendRejected(case, parent, MakeBlock(*common, wrong, previous, turn_number=number, turn_blocks=completed),
                 'wrong-producer')
    SendRejected(case, parent, MakeBlock(*common, key, previous, turn_number=number + 1, turn_blocks=completed),
                 'wrong-turn-number')
    SendRejected(case, parent, MakeBlock(*common, key, previous, turn_number=number, turn_blocks=completed + 1),
                 'wrong-turn-block-count')
    # Re-sign every negative block, so the controls exercise consensus rules.
    valid = MakeBlock(*common, key, previous, turn_number=number, turn_blocks=completed)
    SendFrame(case.ports[6], 7, valid)
    case.Wait(lambda: case.Stats(6)['head'] == valid[80:112].hex(), 'correct turn counter positive control')
    blocks.append(Block(valid))
    parent = blocks[-1]
    early_slot = parent['slot'] + case.config['producer_timeout_slots']
    fields = NextFields(blocks, parent, early_slot, case.config)
    next_owner = case.keys[case.public.index(fields[1][(fields[3] + 1) % len(fields[1])])]
    early = MakeBlock(bytes.fromhex(parent['hash']), parent['height'] + 1, [], early_slot, next_owner,
                      fields[2], turn_number=fields[3] + 1, turn_blocks=1)
    # At the eighth successor slot only seven empty slots have completed.
    # Wait until this declared slot is no more than two slots in the future.
    case.Wait(lambda: int(time.time() * 1000) // case.config['slot_ms'] + 2 >= early_slot,
              'early takeover block fits the future-slot limit')
    SendRejected(case, parent, early, 'takeover-before-eight-empty-slots')
    late_slot = parent['slot'] + case.config['producer_timeout_slots'] + 1
    late_fields = NextFields(blocks, parent, late_slot, case.config)
    old_key = case.keys[case.public.index(parent['producer'])]
    Require(bytes(old_key.verify_key).hex() != late_fields[1][late_fields[3] % len(late_fields[1])],
            'The timed-out producer no longer owns the declared slot')
    late = MakeBlock(bytes.fromhex(parent['hash']), parent['height'] + 1, [], late_slot, old_key,
                     late_fields[2], turn_number=late_fields[3], turn_blocks=late_fields[4])
    case.Wait(lambda: int(time.time() * 1000) // case.config['slot_ms'] + 2 >= late_slot,
              'timed-out producer block fits the future-slot limit')
    SendRejected(case, parent, late, 'timed-out-producer-return')
    case.Check('wrong public key, forged counters, early takeover and timed-out producer return rejected')

    tx2 = Transaction(case.keys[6], bytes(case.keys[0].verify_key), 4, nonce=3)
    extension = FutureBlock(case, blocks, parent, [tx2], 'future-extension')
    blocks.append(extension)
    extra = Transaction(case.keys[0], bytes(case.keys[6].verify_key), 5)
    before = case.Stats(6)['reorganizations']
    replacement = FutureBlock(case, blocks, parent, [tx2, extra], 'future-better-fork')
    Require(case.Stats(6)['reorganizations'] > before, 'Better fork caused a real rollback and replay')
    blocks[-1] = replacement
    row = case.Stats(6)['vdf']
    Require((row['turn_number'], row['turn_blocks']) ==
            (replacement['turn_number'], replacement['turn_blocks']), 'Restored runtime turn matches fork head')
    case.Wait(lambda: int(time.time() * 1000) // case.config['slot_ms'] > replacement['slot'],
              'successor slot after fork replay')
    slot = int(time.time() * 1000) // case.config['slot_ms']
    height, roster, previous, number, completed = NextFields(blocks, replacement, slot, case.config)
    key = case.keys[case.public.index(roster[number % len(roster)])]
    successor = MakeBlock(bytes.fromhex(replacement['hash']), height, [], slot, key, previous,
                          turn_number=number, turn_blocks=completed)
    SendFrame(case.ports[6], 7, successor)
    case.Wait(lambda: case.Stats(6)['head'] == successor[80:112].hex(), 'successor accepted after turn rollback/replay')
    blocks.append(Block(successor))
    final = case.Chain(name='after-fork-replay')
    Require(final[-1]['hash'] == successor[80:112].hex(), 'Fork successor is the durable canonical head')
    case.report['final_audit'] = Audit(final, case.config)
    case.report['state'] = case.AuditState(final, [6])
    case.Check('future blocks wait until their slot, then commit without retransmission')
    case.Check('better fork restores producer counters and independently checked account state')


def SelfTest():
    config = {'epoch_blocks': 160, 'consecutive_blocks': 32, 'producer_timeout_slots': 8}
    parent = {'height': 325, 'slot': 100, 'turn_number': 0, 'turn_blocks': 31}
    Require(Turn(parent, 326, 101, config) == (0, 32), 'Thirty-second block stays with current producer')
    parent['turn_blocks'] = 32
    Require(Turn(parent, 326, 101, config) == (1, 1), 'Thirty-third block goes to next producer')
    parent['turn_blocks'] = 3
    Require(Turn(parent, 326, 108, config) == (0, 4), 'Eight successor slots contain seven empty slots')
    Require(Turn(parent, 326, 109, config) == (1, 1), 'Eight empty slots advance one producer')
    Require(Turn(parent, 326, 117, config) == (2, 1), 'Sixteen empty slots advance two producers')
    parent.update(height=481, turn_number=4, turn_blocks=32)
    Require(Turn(parent, 482, 101, config) == (0, 1), 'New elected epoch resets the turn')
    from nacl.signing import SigningKey
    key = SigningKey(Hash(b'Chain/VdfContinuousNetwork/self-test'))
    data = MakeBlock(bytes(32), 2, [], 100, key, turn_number=2, turn_blocks=7)
    decoded = Block(data)
    Require((decoded['turn_number'], decoded['turn_blocks']) == (2, 7), 'Version-two wire round trip')
    print('{"status":"passed","checks":7,"scope":"continuous-turn schedule and wire codec"}')


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin', type=Path)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--budget', type=float, default=300)
    parser.add_argument('--case', action='append', choices=('full-turns', 'timeouts'))
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        SelfTest()
        return
    if args.bin is None or args.work is None:
        parser.error('--bin and --work are required')
    binary, work = args.bin.resolve(), args.work.resolve()
    Require((binary / ExeName('boost')).is_file(), 'Node binary missing')
    Require(args.timeout > 0 and args.budget > 0, 'Positive timeout and budget')
    work.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {'status': 'running', 'scope': 'seven-process consecutive VDF turns', 'cases': []}
    try:
        for name in args.case or ('full-turns', 'timeouts'):
            case_started = time.monotonic()
            case = ContinuousCase(binary, work, name, args.timeout, 32 if name == 'full-turns' else 3,
                                  50 if name == 'full-turns' else 200, started + args.budget)
            try:
                FullTurns(case) if name == 'full-turns' else Injections(case, Outages(case))
                case.report['status'] = 'passed'
            except BaseException as error:
                case.report.update(status='failed', error=repr(error), traceback=traceback.format_exc())
                raise
            finally:
                case.report['elapsed_seconds'] = time.monotonic() - case_started
                try:
                    case.Finish()
                except BaseException as error:
                    case.report.update(status='failed', cleanup_error=repr(error))
                    Json(case.work / 'result.json', case.report)
                    raise
                finally:
                    report['cases'].append({'case': name, 'status': case.report['status'],
                                            'result': name + '/result.json'})
            print('{"case":"' + name + '","status":"passed"}', flush=True)
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        report['elapsed_seconds'] = time.monotonic() - started
        Json(work / 'result.json', report)


if __name__ == '__main__':
    Main()
