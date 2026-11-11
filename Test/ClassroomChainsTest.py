"""Saved chains use isolated ledgers and exactly one supervised node per gateway."""
import argparse
import json
import socket
import threading
import time
import urllib.error
from pathlib import Path

import psutil
from ClassroomJoinTest import App
from NetworkTest import ExeName


def Main(args):
    source = Path(__file__).resolve().parents[1]
    root = args.work.resolve()
    root.mkdir(parents=True, exist_ok=False)
    apps, checks, seen = [], [], {}
    monitor_stop = threading.Event()
    monitor_errors = []

    def New(name):
        app = App(args.bin.resolve(), source, root / name)
        apps.append(app)
        return app

    def Included(app, transaction):
        return app.Wait(lambda state: any(item['hash'] == transaction['hash'] and item['state'] == 'included'
                                         for item in state.get('outbox', [])))

    def Rejected(app, action, **values):
        try:
            app.Api(action, **values)
        except urllib.error.HTTPError as error:
            assert error.code == 400
        else:
            raise AssertionError('Invalid operation accepted: ' + action)

    def Shutdown(app):
        app.Api('shutdown')
        app.process.wait(timeout=45)
        assert app.process.returncode == 0
        app.process.stdout.close()

    def AssertOne(app, state):
        assert len(app.NodeProcesses()) == 1
        assert sum(item['running'] for item in state['networks']) == 1
        assert sum(item['selected'] for item in state['networks']) == 1
        assert next(item['id'] for item in state['networks'] if item['selected']) == state['network']['id']

    def Monitor():
        while not monitor_stop.wait(.03):
            try:
                if not apps or apps[0].process.poll() is not None:
                    continue
                nodes = apps[0].NodeProcesses()
                assert len(nodes) <= 1, 'Old and new chain nodes overlapped'
                for node in nodes:
                    seen[(node.pid, node.create_time())] = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            except Exception as error:
                monitor_errors.append(str(error))
                return

    thread = None
    try:
        app = New('manager')
        thread = threading.Thread(target=Monitor, daemon=True)
        thread.start()
        first = app.Api('create', title='Saved chain A', chat_version=2)
        a = first['network']
        assert len(a['id']) == 24 and len(a['genesis_root']) == 64
        a_transfer = app.Api('transfer', network_id=a['id'], receiver='a6' * 32, amount='123')
        Included(app, a_transfer)
        a_message = app.Api('message', network_id=a['id'], room=1, body='Only chain A retains this message')
        a_state = Included(app, a_message)
        a_balance = a_state['wallets'][0]['balance']
        a_nonce = a_state['wallets'][0]['nonce']
        a_pid = app.NodeProcesses()[0]
        a_identity = (a_pid.pid, a_pid.create_time())
        AssertOne(app, a_state)

        owner = New('chain-b-owner')
        other = owner.Api('create', title='Saved chain A', chat_version=2)
        endpoint = '127.0.0.1:' + str(other['network']['ports'][0])
        invite = owner.Api('invite', seeds=[endpoint])
        joined = app.Api('join', network_id=a['id'], invite=invite)
        joined = app.Wait(lambda state: state.get('contract_ready') and state.get('connection_state', {}).get('phase') == 'ready')
        b = joined['network']
        assert b['id'] != a['id'] and b['genesis_root'] != a['genesis_root']
        assert b['title'] == a['title']
        assert {item['id'] for item in joined['networks']} == {a['id'], b['id']}
        assert not any(message['body'] == 'Only chain A retains this message' for message in joined['messages'])
        try:
            assert psutil.Process(a_identity[0]).create_time() != a_identity[1], 'Old chain A node still alive'
        except psutil.NoSuchProcess:
            pass
        key = joined['wallets'][0]['public_key']
        Included(owner, owner.Api('transfer', receiver=key, amount='10000000'))
        app.Wait(lambda state: int(state['wallets'][0].get('balance', '0')) >= 10000000)
        b_message = app.Api('message', network_id=b['id'], room=1, body='Only chain B retains this message')
        b_state = Included(app, b_message)
        b_balance, b_nonce = b_state['wallets'][0]['balance'], b_state['wallets'][0]['nonce']
        AssertOne(app, b_state)
        checks.append('Same-title chains remain distinct by profile ID and genesis; the old node exits before its replacement starts')

        switched = app.Api('network_switch', id=a['id'], network_id=b['id'])
        assert switched['network']['id'] == a['id'] and switched['contract_ready']
        assert switched['wallets'][0]['balance'] == a_balance and switched['wallets'][0]['nonce'] == a_nonce
        assert any(message['body'] == 'Only chain A retains this message' for message in switched['messages'])
        assert not any(message['body'] == 'Only chain B retains this message' for message in switched['messages'])
        assert any(item['hash'] == a_message['hash'] for item in switched['outbox'])
        assert not any(item['hash'] == b_message['hash'] for item in switched['outbox'])
        assert switched['last_connection'] == ''
        AssertOne(app, switched)
        Rejected(app, 'transfer', network_id=b['id'], receiver='a6' * 32, amount='1')
        Rejected(app, 'stop', network_id=b['id'])
        assert app.Api('status', network_id=b['id'])['network']['id'] == a['id']
        checks.append('Switching restores A balance, nonce, index, outbox and connection state; stale mutations are rejected')

        b_path = app.root / 'data/networks' / b['id']
        marker = b_path / 'preserve-test-marker.txt'
        marker.write_text('same ledger directory', encoding='utf-8')
        duplicate = app.Api('join', network_id=a['id'], invite=invite)
        assert duplicate['network']['id'] == b['id'] and len(duplicate['networks']) == 2
        assert duplicate['wallets'][0]['balance'] == b_balance and duplicate['wallets'][0]['nonce'] == b_nonce
        assert any(message['body'] == 'Only chain B retains this message' for message in duplicate['messages'])
        assert not any(message['body'] == 'Only chain A retains this message' for message in duplicate['messages'])
        assert marker.read_text(encoding='utf-8') == 'same ledger directory'
        pid = app.NodeProcesses()[0].pid
        duplicate = app.Api('join', network_id=b['id'], invite=invite)
        assert app.NodeProcesses()[0].pid == pid and len(duplicate['networks']) == 2
        app.Wait(lambda state: state.get('last_connection') == endpoint)
        checks.append('Joining a saved chain reuses its existing directory and history; joining the current chain keeps its PID')

        app.Api('network_switch', id=a['id'], network_id=b['id'])
        stopped = app.Api('stop', network_id=a['id'])
        assert not stopped['running'] and not app.NodeProcesses()
        Shutdown(app)
        app.Launch()
        restarted = app.Api('status')
        assert restarted['network']['id'] == a['id'] and not restarted.get('running', False)
        assert len(restarted['networks']) == 2 and not any(item['running'] for item in restarted['networks'])
        assert not app.NodeProcesses()
        app.Api('unlock', password='isolated-classroom-test')
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(('0.0.0.0', b['ports'][0]))
            occupied.listen()
            Rejected(app, 'network_switch', id=b['id'], network_id=a['id'])
            restored = app.Api('status')
            assert restored['network']['id'] == a['id'] and not restored.get('running', False)
            assert not app.NodeProcesses()
        app.Api('start', network_id=a['id'])
        app.Wait(lambda state: state.get('contract_ready'))
        checks.append('Explicit stop survives gateway restart; selecting and starting the saved chain remains possible')

        # Import the existing 24-hex directory/session format without relocating its database.
        Shutdown(app)
        session_path = app.root / 'data/session.json'
        session = json.loads(session_path.read_text(encoding='utf-8'))
        session.pop('running')
        session_path.write_text(json.dumps(session), encoding='utf-8')
        a_profile_path = app.root / 'data/networks' / a['id'] / 'profile.json'
        legacy = json.loads(a_profile_path.read_text(encoding='utf-8'))
        legacy.pop('active_wallet', None)
        legacy.pop('nickname', None)
        a_profile_path.write_text(json.dumps(legacy), encoding='utf-8')
        app.Launch()
        imported = app.Wait(lambda state: state.get('contract_ready'))
        assert imported['network']['id'] == a['id'] and len(imported['networks']) == 2
        assert any(message['body'] == 'Only chain A retains this message' for message in imported['messages'])
        app.Api('unlock', password='isolated-classroom-test')
        checks.append('Old sessions without the running flag resume their original 24-hex profile and canonical history')

        b_profile_path = b_path / 'profile.json'
        original = b_profile_path.read_bytes()
        try:
            damaged = json.loads(original)
            damaged['genesis']['accounts'][0]['balance'] += 1
            b_profile_path.write_text(json.dumps(damaged), encoding='utf-8')
            pid = app.NodeProcesses()[0].pid
            Rejected(app, 'network_switch', id=b['id'], network_id=a['id'])
            assert app.NodeProcesses()[0].pid == pid
            assert app.Api('status')['network']['id'] == a['id']
        finally:
            b_profile_path.write_bytes(original)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(('0.0.0.0', b['ports'][0]))
            occupied.listen()
            Rejected(app, 'network_switch', id=b['id'], network_id=a['id'])
            restored = app.Wait(lambda state: state.get('running') and state['network']['id'] == a['id'])
            AssertOne(app, restored)
        try:
            # A valid profile fingerprint still must not open a database belonging to B.
            wrong_db = json.loads(original)
            wrong_db['genesis'], wrong_db['genesis_root'] = a['genesis'], a['genesis_root']
            b_profile_path.write_text(json.dumps(wrong_db), encoding='utf-8')
            Rejected(app, 'network_switch', id=b['id'], network_id=a['id'])
            restored = app.Wait(lambda state: state.get('running') and state['network']['id'] == a['id'])
            assert any(message['body'] == 'Only chain A retains this message' for message in restored['messages'])
            AssertOne(app, restored)
        finally:
            b_profile_path.write_bytes(original)
        checks.append('Corrupt profiles are rejected before stopping; occupied ports and mismatched databases roll back to running A')
        assert not monitor_errors, monitor_errors
        (root / 'result.json').write_text(json.dumps({'passed': True, 'checks': checks,
            'networks': [a['id'], b['id']], 'observed_node_instances': len(seen)}, indent=2), encoding='utf-8')
        print(json.dumps({'passed': True, 'checks': checks}))
    finally:
        monitor_stop.set()
        if thread:
            thread.join(timeout=5)
        errors = list(monitor_errors)
        for app in reversed(apps):
            try:
                app.Stop()
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError('; '.join(errors))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    Main(parser.parse_args())
