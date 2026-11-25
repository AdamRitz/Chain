"""Seven-process correctness regression for VDF producer rotation.

Uses fresh databases, deterministic test identities and dynamically selected
loopback ports. The existing VdfNetwork suite continues testing legacy rules.
"""
import argparse
from pathlib import Path
import time
import traceback

from NetworkTest import ExeName, SendFrame
from VdfNetworkTest import (Block, Candidate, Case, Json, MakeBlock,
                            Require, Transaction, TransactionBlock)


class RotationCase(Case):
    def __init__(self, binary, work, timeout, epoch_blocks, slot_ms, deadline):
        super().__init__(binary, work, 'rotation', timeout, epoch_blocks, slot_ms, deadline)
        self.config['producer_rotation'] = True
        Json(self.work / 'genesis.json', self.genesis)


def Roster(blocks, height, config):
    epoch = (height - 2) // config['epoch_blocks']
    Require(epoch >= 2, 'Rotation checks require an elected epoch')
    records = blocks[epoch * config['epoch_blocks']]['candidates']
    Require(len(records) == 5, 'Five elected producer records are available')
    ranks = [(Candidate(record)['score'], Candidate(record)['public_key']) for record in records]
    Require(ranks == sorted(set(ranks)), 'On-chain producer records have deterministic score order')
    return [public for _, public in ranks]


def Owner(roster, slot, config):
    start = config['start_time_ms'] // config['slot_ms']
    Require(slot >= start and roster, 'Slot and active producer list')
    return roster[(slot - start) % len(roster)]


def AuditRotation(blocks, config):
    checked, epochs = 0, set()
    for parent, block in zip(blocks, blocks[1:]):
        epoch = (block['height'] - 2) // config['epoch_blocks']
        Require(block['slot'] > parent.get('slot', 0), 'Canonical slots strictly increase')
        if epoch < 2:
            continue
        roster = Roster(blocks, block['height'], config)
        Require(block['producer'] == Owner(roster, block['slot'], config),
                f"Wrong scheduled producer at height {block['height']}, slot {block['slot']}")
        checked += 1
        epochs.add(epoch)
    Require(checked >= config['epoch_blocks'], 'At least one full elected epoch audited')
    return {'blocks': checked, 'epochs': sorted(epochs)}


def NextFields(blocks, parent, config):
    height = parent['height'] + 1
    roster = Roster(blocks, height, config)
    previous = parent['candidates'] if (height - 2) % config['epoch_blocks'] else []
    return height, roster, previous


def FreezeAndConverge(case, name):
    case.Stop()
    case.Start(producers=())
    case.Wait(case.Converged, name + ' persistent replicas converge')
    blocks = case.Chain(name=name)
    case.AuditState(blocks, range(7))
    return blocks


def ExerciseRotation(case):
    case.Start()
    case.Ready(3)
    Require(all(case.Stats(i)['vdf']['producer_rotation'] for i in range(7)),
            'All processes loaded the rotation genesis rule')
    for index in range(6):
        SendFrame(case.ports[index], 1,
                  Transaction(case.keys[index], bytes(case.keys[6].verify_key), index + 1))
    case.Wait(lambda: case.Stats(6)['chain_transactions'] == 6, 'six initial transfers')

    def beginning():
        row = case.Stats(6)
        epoch, position = divmod(row['height'] - 1, case.config['epoch_blocks'])
        return row if (epoch == row['vdf']['epoch'] and epoch >= 3 and 1 <= position <= 2
                       and len(row['vdf']['producers']) == 5) else False

    row = case.Wait(beginning, 'beginning of an elected epoch before stopping one producer')
    selected, epoch = row['vdf']['producers'], row['vdf']['epoch']
    victim = case.public.index(Owner(selected, row['vdf']['slot'] + 1, case.config))
    case.Stop([victim])
    after_stop = case.Stats(6)
    Require(after_stop['vdf']['epoch'] == epoch, 'Producer stopped before the next election')
    stopped_slot = after_stop['vdf']['slot']
    transaction = Transaction(case.keys[6], bytes(case.keys[0].verify_key), 23)
    SendFrame(case.ports[next(i for i in range(6) if i != victim)], 1, transaction)
    case.Wait(lambda: case.Stats(6)['chain_transactions'] == 7 and
              case.Stats(6)['height'] >= after_stop['height'] + 6,
              'remaining producers continue across the offline producer slots')
    before_restart = case.Stats(6)['height']
    blocks = FreezeAndConverge(case, 'after-offline')
    recovery = TransactionBlock(blocks, transaction)
    parent = blocks[recovery['height'] - 2]
    recovery_gap = recovery['slot'] - parent['slot']
    Require(recovery_gap < case.config['fallback_slots'],
            'Transaction recovery did not wait for outsider fallback')
    skipped = []
    for parent, block in zip(blocks, blocks[1:]):
        if parent.get('slot', 0) < stopped_slot or (block['height'] - 2) // case.config['epoch_blocks'] != epoch:
            continue
        gap = block['slot'] - parent['slot']
        if 1 < gap < case.config['fallback_slots'] and any(
                Owner(selected, slot, case.config) == case.public[victim]
                for slot in range(parent['slot'] + 1, block['slot'])):
            skipped.append({'parent_slot': parent['slot'], 'slot': block['slot'], 'height': block['height']})
    Require(skipped, 'A missing producer slot was skipped without the 20-slot fallback wait')
    Require(case.Stats(victim)['height'] >= before_restart, 'Restarted offline producer synchronized')
    case.report['offline'] = {'producer_index': victim, 'epoch': epoch,
                              'stopped_slot': stopped_slot, 'skipped_slots': skipped,
                              'recovery_slot_gap': recovery_gap}
    case.report['rotation_before_restart'] = AuditRotation(blocks, case.config)
    case.Check('five elected identities rotate by absolute slot and on-chain score order')
    case.Check('one producer stopped; its slots skipped and native transfer committed before outsider fallback')
    case.Check('seven persistent replicas converge after the stopped producer restarts; balances and nonces agree')

    case.Stop()
    case.Start()
    restart_height = blocks[-1]['height']
    case.Wait(lambda: case.Stats(6)['height'] >= restart_height + case.config['epoch_blocks'],
              'production continues for another full epoch after restart')
    blocks = FreezeAndConverge(case, 'after-production-restart')
    case.report['rotation_after_restart'] = AuditRotation(blocks, case.config)
    Require(len(case.report['rotation_after_restart']['epochs']) >= 2,
            'Rotation audited across multiple election epochs')
    case.Check('rotation continues after restart and across an election boundary')
    # Keep one isolated nonproducing observer for hand-signed protocol attacks.
    case.Stop()
    case.Start([6], producers=(), groups=((6,),))
    return case.Chain(name='before-injection')


def FutureBlock(case, blocks, parent, transactions, name):
    height, roster, previous = NextFields(blocks, parent, case.config)
    slot_ms = case.config['slot_ms']
    # Inject near the beginning of a slot to leave almost two complete slots
    # for observing that even a better signed block cannot advance the clock.
    case.Wait(lambda: (int(time.time() * 1000) % slot_ms) < slot_ms // 4,
              'early slot for future block injection')
    slot = int(time.time() * 1000) // slot_ms + 2
    key = case.keys[case.public.index(Owner(roster, slot, case.config))]
    data = MakeBlock(bytes.fromhex(parent['hash']), height, transactions, slot, key, previous)
    (case.work / (name + '.bin')).write_bytes(data)
    before = case.Stats(6)
    SendFrame(case.ports[6], 7, data)
    observations = 0
    while time.time() * 1000 < slot * slot_ms - 20:
        row = case.Stats(6)
        # A request that started before the boundary can finish after it.
        if time.time() * 1000 < slot * slot_ms - 20:
            Require(row['head'] == before['head'], name + ' was adopted before its slot')
            observations += 1
        time.sleep(.01)
    Require(observations >= 2, 'At least two pre-slot head checks completed')
    case.Wait(lambda: case.Stats(6)['head'] == data[80:112].hex(),
              name + ' automatically adopted when its declared slot arrives', timeout=10)
    case.report.setdefault('future_blocks', []).append(
        {'case': name, 'height': height, 'slot': slot, 'pre_slot_checks': observations,
         'automatically_adopted': True})
    return Block(data)


def ExerciseRejections(case, blocks):
    parent = blocks[-1]
    height, roster, previous = NextFields(blocks, parent, case.config)
    slot = max(int(time.time() * 1000) // case.config['slot_ms'], parent['slot'] + 1)
    expected = Owner(roster, slot, case.config)
    wrong = next(public for public in roster if public != expected)
    transaction = Transaction(case.keys[6], bytes(case.keys[0].verify_key), 3, nonce=2)
    invalid = MakeBlock(bytes.fromhex(parent['hash']), height, [transaction], slot,
                        case.keys[case.public.index(wrong)], previous)
    (case.work / 'wrong-member-slot.bin').write_bytes(invalid)
    before = case.Stats(6)['invalid_user_blocks']
    SendFrame(case.ports[6], 7, invalid)
    case.Wait(lambda: case.Stats(6)['invalid_user_blocks'] > before, 'valid member rejected outside its slot')
    Require(case.Stats(6)['head'] == parent['hash'], 'Wrong member cannot change the head')
    valid = MakeBlock(bytes.fromhex(parent['hash']), height, [transaction], slot,
                      case.keys[case.public.index(expected)], previous)
    SendFrame(case.ports[6], 7, valid)
    case.Wait(lambda: case.Stats(6)['head'] == valid[80:112].hex(), 'correct scheduled member positive control')
    blocks.append(Block(valid))
    case.Check('wrong-slot member rejected; identical parent, slot and transaction accepted from the scheduled member')

    parent = blocks[-1]
    transaction = Transaction(case.keys[6], bytes(case.keys[0].verify_key), 4, nonce=3)
    future = FutureBlock(case, blocks, parent, [transaction], 'future-extension')
    blocks.append(future)
    # Replacing the last block with two transactions makes the future fork
    # strictly better by the normal branch ranking, independently of hash luck.
    extra = Transaction(case.keys[0], bytes(case.keys[6].verify_key), 5, nonce=2)
    replacement = FutureBlock(case, blocks, parent, [transaction, extra], 'future-better-fork')
    Require(case.Stats(6)['reorganizations'] > 0, 'Future competing branch actually reorganized after its slot')
    blocks[-1] = replacement
    final = case.Chain(name='after-injection')
    Require(final[-1]['hash'] == replacement['hash'], 'Future fork durable canonical head')
    case.report['final_rotation'] = AuditRotation(final, case.config)
    case.report['state'] = case.AuditState(final, [6])
    case.Check('future extension and better fork remain unapplied until their slots, then apply without retransmission')


def SelfTest():
    config = {'start_time_ms': 1000, 'slot_ms': 250}
    roster = ['a', 'b', 'c', 'd', 'e']
    Require([Owner(roster, slot, config) for slot in range(4, 11)] == list('abcdeab'),
            'Rotation is relative to genesis start slot')
    Require(Owner(roster, 11, config) == 'c', 'Skipped slots still advance rotation')
    Require(Owner(roster[:3], 11, config) == 'b', 'Active list length determines modulo')
    try:
        Owner(roster, 3, config)
    except AssertionError:
        pass
    else:
        raise AssertionError('Pre-genesis slot accepted')
    print('{"status":"passed","checks":4,"scope":"rotation schedule helper"}')


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin', type=Path)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--budget', type=float, default=180)
    parser.add_argument('--epoch-blocks', type=int, default=16)
    parser.add_argument('--slot-ms', type=int, default=250)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        SelfTest()
        return
    if args.bin is None or args.work is None:
        parser.error('--bin and --work are required')
    if args.epoch_blocks < 16 or not 200 <= args.slot_ms <= 1000 or args.timeout <= 0 or args.budget <= 0:
        parser.error('use epoch-blocks >= 16, slot-ms 200..1000 and positive timeout/budget')
    binary, work = args.bin.resolve(), args.work.resolve()
    Require((binary / ExeName('boost')).is_file(), 'Node binary missing')
    work.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    case = RotationCase(binary, work, args.timeout, args.epoch_blocks, args.slot_ms, started + args.budget)
    try:
        ExerciseRejections(case, ExerciseRotation(case))
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
        finally:
            Json(work / 'result.json', case.report)
    print('{"status":"passed","scope":"seven-process producer rotation"}', flush=True)


if __name__ == '__main__':
    Main()
