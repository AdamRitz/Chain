"""Three isolated gateways: automatic v3 grants, peer sponsorship and restart idempotence."""
import argparse
import json
import time
import urllib.error
from pathlib import Path

from ClassroomJoinTest import App


GRANT = 10**12
RESERVE = 8 * 10**15
PASSWORD = 'isolated-classroom-test'


def Main(args):
    source = Path(__file__).resolve().parents[1]
    root = args.work.resolve()
    root.mkdir(parents=True, exist_ok=False)
    apps, checks, evidence = [], [], {}
    started = time.monotonic()
    deadline = started + 55
    methods = json.loads((source / 'Contracts/ClassroomChat.compiled.json').read_text())['contracts']['ClassroomChat']['evm']['methodIdentifiers']

    def New(name):
        # Register ownership before __init__ can launch a process and then fail.
        app = App.__new__(App)
        apps.append(app)
        App.__init__(app, args.bin.resolve(), source, root / name)
        return app

    def Wait(app, predicate):
        until = min(deadline, time.monotonic() + 18)
        state = {}
        while time.monotonic() < until:
            state = app.Api('status')
            if predicate(state):
                return state
            time.sleep(.12)
        # Keep diagnostics public and bounded; never persist the gateway token.
        raise AssertionError(json.dumps({
            'running': state.get('running'),
            'contract_ready': state.get('contract_ready'),
            'funding_state': state.get('funding_state'),
            'last_error': state.get('last_error'),
            'elapsed_seconds': round(time.monotonic() - started, 2),
        }, ensure_ascii=False))

    def Identity(state):
        return next(wallet for wallet in state['wallets'] if wallet['id'] == state['active_wallet'])

    def Ready(app):
        return Wait(app, lambda state: state.get('contract_ready') and
                    state.get('funding_state', {}).get('phase') == 'ready' and
                    int(Identity(state).get('balance', '0')) >= GRANT)

    def Included(app, transaction):
        return Wait(app, lambda state: any(row['hash'] == transaction['hash'] and
                    row['state'] == 'included' and row.get('execution_status', 0) == 0
                    for row in state.get('outbox', [])))

    def View(app, signature, argument=''):
        state = app.Api('status')
        result = app.Api('call', to=state['network']['contract'], input=methods[signature] + argument)
        assert result['status'] == 0 and result['read_only'], (signature, result)
        return int(result['output'], 16)

    def Claimed(app, state):
        return View(app, 'claimed(address)', Identity(state)['evm_address'].removeprefix('0x').zfill(64)) == 1

    def Snapshot(app):
        state = app.Api('status')
        wallet = Identity(state)
        return {
            'public_key': wallet['public_key'],
            'balance': wallet['balance'],
            'nonce': wallet['nonce'],
            'genesis_root': state['network']['genesis_root'],
            'contract': state['network']['contract'],
            'head': state['stats']['head'],
            'transactions': state['stats']['chain_transactions'],
            'outbox_hashes': sorted(row['hash'] for row in state.get('outbox', [])),
            'reserve': str(View(app, 'Reserve()')),
        }

    def RepeatClaim(app):
        # Gateway snapshots refresh once per second; settle the preceding claim
        # before comparing the ledger, then observe another tick after retrying.
        time.sleep(1.1)
        before = Snapshot(app)
        for _ in range(2):
            reply = app.Api('funding_claim')
            assert reply.get('claimed') is True and reply.get('already_claimed') is True, reply
        time.sleep(1.1)
        after = Snapshot(app)
        for key in ('balance', 'nonce', 'outbox_hashes', 'reserve', 'transactions'):
            assert after[key] == before[key], (key, before[key], after[key])
        return after

    try:
        creator = New('creator')
        first = creator.Api('create', title='Automatic member funding')
        assert first['network']['chat_version'] == 3
        first = Ready(creator)
        assert len(first['nodes']) == len(first['wallets']) == len(creator.NodeProcesses()) == 1
        assert View(creator, 'MEMBER_GRANT()') == GRANT
        assert View(creator, 'Reserve()') == RESERVE and Claimed(creator, first)
        assert max(int(row['balance']) for row in first['network']['genesis']['accounts']) == 9 * 10**15

        member = New('member')
        assert int(Identity(member.Api('status')).get('balance', '0')) == 0
        endpoint = f"127.0.0.1:{first['network']['ports'][0]}"
        member.Api('join', invite=endpoint)
        joined = Ready(member)
        assert joined['last_connection'] == endpoint and Claimed(member, joined)
        assert len(member.NodeProcesses()) == 1
        assert View(member, 'Reserve()') == RESERVE - GRANT
        evidence['first_member'] = RepeatClaim(member)
        checks.append('zero-balance member joins by address, receives one automatic grant and repeated claims add no transaction or balance')

        creator.Api('stop')
        stopped = creator.Api('status')
        assert not stopped['running'] and all(not row['online'] and not row['connections'] for row in stopped['nodes'])
        Wait(member, lambda state: all(not row.get('connections') for row in state['nodes']))
        evidence['creator_stopped'] = {'running': False, 'remaining_node_processes': len(creator.NodeProcesses())}
        assert evidence['creator_stopped']['remaining_node_processes'] == 0
        member_before = Snapshot(member)

        peer = New('peer')
        assert int(Identity(peer.Api('status')).get('balance', '0')) == 0
        peer_endpoint = f"127.0.0.1:{joined['network']['ports'][0]}"
        peer.Api('join', invite=peer_endpoint)
        third = Ready(peer)
        assert Claimed(peer, third) and third['last_connection'] == peer_endpoint
        assert View(peer, 'Reserve()') == RESERVE - 2 * GRANT
        sponsored = Wait(member, lambda state: int(Identity(state)['nonce']) > int(member_before['nonce']))
        assert int(Identity(sponsored)['balance']) < int(member_before['balance'])
        assert any(row['hash'] not in member_before['outbox_hashes'] and row.get('kind') == 'transfer' and
                   row.get('receiver') == Identity(third)['public_key'] and int(row.get('amount', 0)) == 2000000
                   for row in sponsored.get('outbox', [])), 'remaining peer did not sponsor the newcomer'
        evidence['third_member'] = RepeatClaim(peer)
        checks.append('with creator node stopped, an already-funded member sponsors a third zero-balance member and its independent claim succeeds')

        for app, other, body in [(member, peer, 'Member message after peer sponsorship'),
                                 (peer, member, 'New member replies without manual funding')]:
            sent = app.Api('message', room=1, body=body)
            Included(app, sent)
            Wait(other, lambda state: any(row['tx_hash'] == sent['hash'] and row['body'] == body
                                        for row in state.get('messages', [])))
        common = Snapshot(peer)
        Wait(member, lambda state: state['stats']['head'] == common['head'])
        checks.append('both automatically-funded members exchange successful contract messages on the same canonical chain')

        before_restart = Snapshot(member)
        old_pid = member.process.pid
        member.Api('shutdown')
        member.process.wait(timeout=12)
        member.process.stdout.close()
        member.Launch()
        member.Api('unlock', password=PASSWORD)
        Ready(member)
        resumed = RepeatClaim(member)
        assert member.process.pid != old_pid
        for key in ('public_key', 'balance', 'nonce', 'genesis_root', 'contract', 'head', 'outbox_hashes', 'reserve'):
            assert resumed[key] == before_restart[key], (key, before_restart[key], resumed[key])
        evidence['restart'] = resumed
        checks.append('full gateway restart resumes the encrypted identity and original ledger without a second claim or sponsorship')

        state = peer.Api('status')
        remaining = 100000
        amount = int(Identity(state)['balance']) - remaining - int(state['network']['genesis']['base_fee'])
        assert amount > 0
        Included(peer, peer.Api('transfer', receiver=resumed['public_key'], amount=str(amount)))
        low = Wait(peer, lambda value: int(Identity(value)['balance']) < 500000)
        assert int(Identity(low)['balance']) == remaining
        assert Claimed(peer, low)
        try:
            peer.Api('message', room=1, body='This transaction must report insufficient tokens')
            raise AssertionError('insufficient native balance accepted')
        except urllib.error.HTTPError as error:
            failure = json.loads(error.read().decode('utf-8'))
            assert error.code == 400 and failure.get('error') == '代币不足', failure
        evidence['insufficient_balance'] = RepeatClaim(peer)
        checks.append('an already-claimed member that spends its balance gets the precise insufficient-token error and cannot refill by claiming again')

        result = {'checks': checks, 'passed': len(checks), 'evidence': evidence,
                  'elapsed_seconds': round(time.monotonic() - started, 2), 'manual_join_funding_transfers': 0}
        assert result['elapsed_seconds'] < 60
    finally:
        errors = []
        for app in reversed(apps):
            try:
                if getattr(app, 'process', None) is not None:
                    app.Stop()
                elif getattr(app, 'log', None) is not None:
                    app.log.close()
            except Exception as error:
                errors.append(str(error))
        (root / 'database-cleanup.json').write_text(json.dumps({
            'processes_stopped': not errors,
            'remaining_databases': [str(path) for path in root.glob('*/data/networks/*/node*/db')],
        }, indent=2), encoding='utf-8')
        if errors:
            raise RuntimeError('; '.join(errors))
    # A passing artifact is written only after owned processes and DBs are gone.
    (root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': len(checks), 'checks': checks, 'elapsed_seconds': result['elapsed_seconds']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    Main(parser.parse_args())
