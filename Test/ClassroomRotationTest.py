"""GUI-created rotation chains, invitation parameters, live state and old ledgers."""
import argparse
import copy
import hashlib
import json
import socket
import struct
from pathlib import Path

from ClassroomJoinTest import App


def LegacyProfile(current):
    saved = copy.deepcopy(current)
    saved['id'] = 'a9' * 12
    saved['title'] = 'Saved chain without VDF'
    saved['chat_version'] = 2
    saved['contract'] = ''
    saved['seeds'] = []
    saved.pop('deploy_tx', None)
    saved.pop('funding_provider', None)
    saved['genesis'].pop('vdf')
    genesis = saved['genesis']
    accounts = sorted(genesis['accounts'], key=lambda account: bytes.fromhex(account['public_key']))
    encoded = b'Chain accounts v3' + struct.pack('<QQ', genesis['base_fee'], len(accounts))
    for account in accounts:
        encoded += bytes.fromhex(account['public_key']) + struct.pack('<Q', account['balance'])
    saved['genesis_root'] = hashlib.blake2b(encoded, digest_size=32).hexdigest()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        saved['ports'] = [probe.getsockname()[1]]
    return saved


def Main(args):
    source = Path(__file__).resolve().parents[1]
    root = args.work.resolve()
    root.mkdir(parents=True, exist_ok=False)
    apps, checks = [], []

    def New(name):
        app = App.__new__(App)
        apps.append(app)
        App.__init__(app, args.bin.resolve(), source, root / name)
        return app

    def Included(app, transaction):
        return app.Wait(lambda state: any(item['hash'] == transaction['hash'] and item['state'] == 'included'
                                         for item in state.get('outbox', [])))

    def AssertConfig(state, expected):
        actual = state['vdf']
        for key in ['enabled', 'producer_rotation', 'epoch_blocks', 'iterations', 'slot_ms',
                    'start_time_ms', 'fallback_slots', 'future_slots']:
            assert actual[key] == expected[key], (key, actual, expected)
        assert actual['producer_limit'] == 5

    try:
        creator = New('creator')
        first = creator.Api('create', title='GUI rotation chain')
        profile = first['network']
        config = profile['genesis']['vdf']
        assert config == dict(enabled=True, producer_rotation=True, epoch_blocks=32,
                             iterations=65536, slot_ms=200, start_time_ms=config['start_time_ms'],
                             fallback_slots=20, future_slots=2, producers=5)
        assert config['start_time_ms'] > 0 and config['start_time_ms'] % config['slot_ms'] == 0
        AssertConfig(first, config)
        command = creator.NodeProcesses()[0].cmdline()
        assert command[command.index('--max-block-txs') + 1] == '6000'
        assert command[command.index('--block-tx-step') + 1] == '2000'
        assert first['stats']['block_tx_step'] == 2000
        checks.append('GUI creates a 200 ms rotation chain with 32-block epochs and 6000/2000 block parameters')

        invite = creator.Api('invite', seeds=[f"127.0.0.1:{profile['ports'][0]}"])
        assert invite['genesis'] == profile['genesis']
        member = New('member')
        joined = member.Api('join', invite=invite)
        joined = member.Wait(lambda state: state.get('contract_ready'))
        assert joined['network']['genesis'] == profile['genesis']
        assert joined['network']['genesis_root'] == profile['genesis_root']
        AssertConfig(joined, config)
        network_info = json.loads((member.root / 'data/networks' / joined['network']['id'] /
                                  'network-info.json').read_text(encoding='utf-8'))
        assert network_info['genesis']['vdf'] == config
        checks.append('Invitation, address discovery and joining preserve every genesis consensus parameter')

        live = creator.Wait(lambda state: state.get('vdf', {}).get('epoch', 0) >= 2 and
                            bool(state['vdf'].get('scheduled_producer')))
        AssertConfig(live, config)
        assert live['vdf']['scheduled_producer'] in live['vdf']['producers']
        assert 1 <= len(live['vdf']['producers']) <= 5
        for key, value in live['stats']['vdf'].items():
            assert live['vdf'][key] == value
        checks.append('Displayed epoch, selected members and current producer come from the running node')

        member.Wait(lambda state: int(state['wallets'][0].get('balance', '0')) > 0)
        message = member.Api('message', room=1, body='Rotating producers confirm GUI messages')
        Included(member, message)
        creator.Wait(lambda state: any(item['body'] == 'Rotating producers confirm GUI messages'
                                      for item in state.get('messages', [])))
        checks.append('A joined wallet receives funds and sends a message on the rotation chain')

        stopped = creator.Api('stop')
        AssertConfig(stopped, config)
        assert not stopped['running'] and not creator.NodeProcesses()
        for key in ['epoch', 'slot', 'scheduled_producer', 'producers', 'public_key', 'eligible']:
            assert key not in stopped['vdf'], (key, stopped['vdf'])
        assert 'vdf' not in stopped['stats']
        restarted = creator.Api('start')
        assert restarted['network']['genesis'] == profile['genesis']
        assert restarted['vdf']['public_key'] == live['vdf']['public_key']
        checks.append('Stopped GUI shows only saved settings; restarting preserves the chain and producer identity')

        # Load a pre-VDF profile directly, as installed versions do on upgrade.
        # Its native ledger is deliberately independent of the new chat chain.
        old = LegacyProfile(profile)
        old_dir = creator.root / 'data/networks' / old['id']
        old_dir.mkdir()
        (old_dir / 'profile.json').write_text(json.dumps(old), encoding='utf-8')
        creator.Api('networks_refresh')
        legacy = creator.Api('network_switch', id=old['id'])
        assert legacy['running'] and legacy['vdf'] == {'enabled': False}
        assert legacy['stats']['vdf'] == {'enabled': False}
        assert legacy['network']['genesis'] == old['genesis']
        assert legacy['network']['genesis_root'] == old['genesis_root']
        legacy = Included(creator, creator.Api('transfer', receiver='a7' * 32, amount='123'))
        balance, nonce = legacy['wallets'][0]['balance'], legacy['wallets'][0]['nonce']
        creator.Api('network_switch', id=profile['id'])
        legacy = creator.Api('network_switch', id=old['id'])
        assert legacy['wallets'][0]['balance'] == balance and legacy['wallets'][0]['nonce'] == nonce
        assert legacy['vdf'] == {'enabled': False} and 'vdf' not in legacy['network']['genesis']
        checks.append('Saved chains without VDF keep their genesis, production rules, balance and nonce across switches')
        result = {'passed': True, 'checks': checks, 'genesis_root': profile['genesis_root']}
        (root / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result))
    finally:
        errors = []
        for app in reversed(apps):
            try:
                app.Stop()
            except Exception as error:
                errors.append(str(error))
        (root / 'database-cleanup.json').write_text(json.dumps({
            'processes_stopped': not errors,
            'remaining_databases': [str(path) for path in root.glob('*/data/networks/*/node*/db')],
            'errors': errors}, indent=2), encoding='utf-8')
        if errors:
            raise RuntimeError('; '.join(errors))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    Main(parser.parse_args())
