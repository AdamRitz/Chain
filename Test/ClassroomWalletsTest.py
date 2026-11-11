"""Independent encrypted wallets, chain bindings and invalidated signing sessions."""
import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
import socket
import struct
import threading
import time
import urllib.error
from pathlib import Path

from ClassroomJoinTest import App
from NetworkTest import Frame, ReadAll


PASSWORD = 'isolated-classroom-test'
SECOND_PASSWORD = 'independent-wallet-password'
BACKUP_PASSWORD = 'compatible-backup-password'


def ThreeAccountBackup(binary):
    # Public isolated fixture keys; encryption exactly matches the legacy Vault format.
    if os.name == 'nt':
        directories = [binary] + [Path(value) for value in os.environ.get('PATH', '').split(os.pathsep) if value]
        library = next((path for directory in directories for path in directory.glob('libsodium*.dll')), None)
        assert library, 'libsodium runtime not found for the compatibility fixture'
        sodium = ctypes.CDLL(str(library.resolve()))
    else:
        sodium = ctypes.CDLL(ctypes.util.find_library('sodium'))
    assert sodium.sodium_init() >= 0
    sodium.crypto_pwhash.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_char_p, ctypes.c_ulonglong,
                                    ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_size_t, ctypes.c_int]
    sodium.crypto_secretbox_easy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_void_p, ctypes.c_void_p]
    sodium.crypto_sign_seed_keypair.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    salt, nonce = os.urandom(16), os.urandom(24)
    salt_buffer, nonce_buffer = ctypes.create_string_buffer(salt), ctypes.create_string_buffer(nonce)
    key = (ctypes.c_ubyte * 32)()
    password = BACKUP_PASSWORD.encode()
    assert sodium.crypto_pwhash(key, 32, password, len(password), salt_buffer, 2, 64 * 1024 * 1024, 2) == 0
    plaintext = json.dumps({'alice': '11' * 32, 'bob': '22' * 32, 'teacher': '33' * 32}).encode()
    message = ctypes.create_string_buffer(plaintext)
    cipher = (ctypes.c_ubyte * (len(plaintext) + 16))()
    assert sodium.crypto_secretbox_easy(cipher, message, len(plaintext), nonce_buffer, key) == 0
    public, private = (ctypes.c_ubyte * 32)(), (ctypes.c_ubyte * 64)()
    seed = ctypes.create_string_buffer(bytes.fromhex('22' * 32))
    assert sodium.crypto_sign_seed_keypair(public, private, seed) == 0
    ctypes.memset(key, 0, 32)
    ctypes.memset(private, 0, 64)
    ctypes.memset(message, 0, len(plaintext))
    return {'version': 1, 'kdf': 'argon2id-interactive', 'salt': salt.hex(),
            'nonce': nonce.hex(), 'cipher': bytes(cipher).hex()}, bytes(public).hex()


def Node(port, method):
    with socket.create_connection(('127.0.0.1', port), timeout=4) as connection:
        connection.sendall(b'\x02' + Frame(46, json.dumps({'method': method}).encode()))
        kind, size = struct.unpack('<BI', ReadAll(connection, 5))
        assert kind == 47 and size < 256 * 1024
        return json.loads(ReadAll(connection, size))


class FundingGate:
    """Hold public discovery replies; forward only ordinary read-only node RPCs."""
    def __init__(self, port):
        self.node_port = port
        self.descriptor = Node(port, 'network')
        self.first = threading.Event()
        self.second = threading.Event()
        self.release_first = threading.Event()
        self.closing = threading.Event()
        self.lock = threading.Lock()
        self.discoveries = 0
        self.requests = 0
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(.2)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.Accept, daemon=True)
        self.thread.start()

    def Accept(self):
        while not self.closing.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.Serve, args=(connection,), daemon=True).start()

    def Serve(self, connection):
        with connection:
            connection.settimeout(4)
            try:
                if ReadAll(connection, 1) != b'\x02':
                    return
                kind, size = struct.unpack('<BI', ReadAll(connection, 5))
                if size > 256 * 1024:
                    return
                payload = ReadAll(connection, size)
                method = json.loads(payload).get('method') if kind == 46 else None
                if method == 'network':
                    with self.lock:
                        self.discoveries += 1
                        number = self.discoveries
                    (self.first if number == 1 else self.second).set()
                    event = self.release_first if number == 1 else self.closing
                    event.wait(4)
                    if self.closing.is_set():
                        return
                    connection.sendall(Frame(47, json.dumps(self.descriptor).encode()))
                elif method == 'funding_request':
                    with self.lock:
                        self.requests += 1
                    connection.sendall(Frame(47, b'{"queued":true,"amount":"2000000"}'))
                elif kind in (13, 19) or method in ('code', 'call'):
                    with socket.create_connection(('127.0.0.1', self.node_port), timeout=3) as target:
                        target.sendall(b'\x02' + Frame(kind, payload))
                        header = ReadAll(target, 5)
                        _, length = struct.unpack('<BI', header)
                        if length <= 256 * 1024:
                            connection.sendall(header + ReadAll(target, length))
            except (OSError, ValueError, AssertionError, ConnectionError):
                return

    def Close(self):
        self.closing.set()
        self.release_first.set()
        self.listener.close()
        self.thread.join(timeout=3)


def Main(args):
    source = Path(__file__).resolve().parents[1]
    root = args.work.resolve()
    root.mkdir(parents=True, exist_ok=False)
    app, gate = None, None
    checks = []

    def Identity(state):
        return next(row for row in state['wallets'] if row['id'] == state['active_wallet'])['public_key']

    def Rejected(action, **values):
        try:
            app.Api(action, **values)
        except urllib.error.HTTPError as error:
            assert error.code == 400
            body = error.read().decode('utf-8', errors='replace')
            assert all(password not in body for password in (PASSWORD, SECOND_PASSWORD, BACKUP_PASSWORD))
        else:
            raise AssertionError('Operation unexpectedly accepted: ' + action)

    def Public(value):
        if isinstance(value, dict):
            assert not ({'seed', 'secrets', 'private_key', 'encryptionKey', 'cipher', 'password'} & value.keys())
            for item in value.values():
                Public(item)
        elif isinstance(value, list):
            for item in value:
                Public(item)

    def AssertPid(pid):
        assert len(app.NodeProcesses()) == 1 and app.NodeProcesses()[0].pid == pid

    try:
        app = App.__new__(App)
        App.__init__(app, args.bin.resolve(), source, root / 'app')
        initial = app.Api('status')
        assert initial['selected_wallet_id'] == 'legacy' and initial['unlocked']
        legacy_key = Identity(initial)
        legacy_path = app.root / 'data/wallet.vault.json'
        legacy_hash = hashlib.sha256(legacy_path.read_bytes()).hexdigest()
        backup = app.Api('backup', password=BACKUP_PASSWORD)
        assert backup['version'] == 1 and backup['kdf'] == 'argon2id-interactive'
        a = app.Api('create', title='Wallet binding A', chat_version=2)['network']
        pid = app.NodeProcesses()[0].pid
        session = app.Api('status')['wallet_session']
        locked = app.Api('logout', wallet_session=session)
        assert not locked['unlocked'] and locked['wallets'] == [] and locked['running']
        assert locked['wallet_session'] > session
        AssertPid(pid)
        Rejected('transfer', receiver='a5' * 32, amount='1', wallet_session=session)
        Rejected('backup', password=BACKUP_PASSWORD)
        Rejected('message', room=1, body='Locked signing is forbidden')
        Rejected('unlock', password='incorrect-wallet-password')
        failed = app.Api('status')
        assert not failed['unlocked'] and failed['wallets'] == []
        app.Api('unlock', password=PASSWORD)
        # A failed login from an already unlocked state must also clear public account caches.
        Rejected('unlock', password='incorrect-wallet-password')
        assert app.Api('status')['wallets'] == [] and not app.Api('status')['unlocked']
        app.Api('unlock', password=PASSWORD)
        checks.append('Logout keeps the node running, invalidates old requests and prevents new signing; bad passwords leave no account cache')

        before = app.Api('status')
        Rejected('wallet_add', title='Invalid', password='short')
        Rejected('wallet_import', title='Invalid import', password='incorrect-wallet-password', backup=backup)
        damaged = dict(backup, cipher='00' + backup['cipher'][2:])
        if damaged == backup:
            damaged['cipher'] = 'ff' + backup['cipher'][2:]
        Rejected('wallet_import', title='Damaged import', password=BACKUP_PASSWORD, backup=damaged)
        after = app.Api('status')
        assert after['selected_wallet_id'] == before['selected_wallet_id'] and after['unlocked']
        assert Identity(after) == legacy_key and after['wallet_profiles'] == before['wallet_profiles']
        assert hashlib.sha256(legacy_path.read_bytes()).hexdigest() == legacy_hash
        checks.append('Invalid creation/import retains the selected unlocked account and never overwrites the legacy encrypted file')

        second = app.Api('wallet_add', title='Independent second wallet', password=SECOND_PASSWORD)
        second_id, second_key = second['selected_wallet_id'], Identity(second)
        assert len(second_id) == 24 and second_id != 'legacy' and second_key != legacy_key
        assert second['network']['wallet_id'] == second_id and second['network']['active_wallet'] == 'main'
        AssertPid(pid)
        Rejected('transfer', receiver='a5' * 32, amount='1', wallet_session=before['wallet_session'])
        Rejected('wallet_select', id='../wallet.vault.json')
        assert app.Api('status')['selected_wallet_id'] == second_id and app.Api('status')['unlocked']
        selected = app.Api('wallet_select', id='legacy')
        assert selected['selected_wallet_id'] == 'legacy' and not selected['unlocked'] and selected['wallets'] == []
        assert Identity(app.Api('unlock', password=PASSWORD)) == legacy_key
        imported = app.Api('wallet_import', title='Imported compatible backup', password=BACKUP_PASSWORD, backup=backup)
        imported_id = imported['selected_wallet_id']
        assert imported_id not in ('legacy', second_id) and Identity(imported) == legacy_key
        Rejected('restore', password=BACKUP_PASSWORD, backup=backup)
        assert app.Api('status')['selected_wallet_id'] == imported_id and app.Api('status')['unlocked']
        assert hashlib.sha256(legacy_path.read_bytes()).hexdigest() == legacy_hash
        AssertPid(pid)
        checks.append('Named creation, saved-wallet selection and old-format backup import use independent stable IDs without restarting the node')

        app.Wait(lambda state: bool(state['wallets']) and 'balance' in state['wallets'][0])
        before_failure = app.Api('status')
        failed_balance = next(row for row in before_failure['wallets'] if row['id'] == before_failure['active_wallet'])['balance']
        temporary_path = app.root / 'data/wallet-selection.json.tmp'
        temporary_path.mkdir()
        try:
            Rejected('wallet_select', id=second_id)
            after_failure = app.Api('status')
            assert after_failure['selected_wallet_id'] == before_failure['selected_wallet_id'] and after_failure['unlocked']
            assert Identity(after_failure) == legacy_key
            assert next(row for row in after_failure['wallets'] if row['id'] == after_failure['active_wallet'])['balance'] == failed_balance
            AssertPid(pid)
        finally:
            temporary_path.rmdir()  # Only this exact empty fixture directory was created above.
        triple_backup, bob_key = ThreeAccountBackup(args.bin.resolve())
        triple = app.Api('wallet_import', title='Legacy three-account wallet', password=BACKUP_PASSWORD, backup=triple_backup)
        triple_id = triple['selected_wallet_id']
        assert {row['id'] for row in triple['wallets']} == {'alice', 'bob', 'teacher'}
        chosen = app.Api('select_wallet', wallet='bob')
        assert chosen['active_wallet'] == 'bob' and Identity(chosen) == bob_key
        app.Api('wallet_select', id=second_id)
        app.Api('unlock', password=SECOND_PASSWORD)
        app.Api('wallet_select', id=triple_id)
        returned = app.Api('unlock', password=BACKUP_PASSWORD)
        assert returned['active_wallet'] == 'bob' and Identity(returned) == bob_key
        assert returned['network']['wallet_accounts'][triple_id] == 'bob'
        AssertPid(pid)
        checks.append('Persistence failure restores the unlocked identity and balance; real legacy three-account backup retains bob across container switches')

        app.Api('wallet_select', id='legacy')
        app.Api('unlock', password=PASSWORD)
        b = app.Api('create', title='Wallet binding B', chat_version=2)['network']
        app.Api('wallet_select', id=second_id)
        app.Api('unlock', password=SECOND_PASSWORD)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(('0.0.0.0', a['ports'][0]))
            occupied.listen()
            Rejected('network_switch', id=a['id'])
            rolled_back = app.Wait(lambda state: state.get('running') and state['network']['id'] == b['id'])
            assert rolled_back['selected_wallet_id'] == second_id and rolled_back['unlocked']
            assert Identity(rolled_back) == second_key and rolled_back['active_wallet'] == 'main'
        back = app.Api('network_switch', id=a['id'])
        assert back['selected_wallet_id'] == 'legacy' and not back['unlocked'] and back['network']['wallet_id'] == 'legacy'
        assert Identity(app.Api('unlock', password=PASSWORD)) == legacy_key
        back = app.Api('network_switch', id=b['id'])
        assert back['selected_wallet_id'] == second_id and not back['unlocked'] and back['wallets'] == []
        assert Identity(app.Api('unlock', password=SECOND_PASSWORD)) == second_key
        app.Api('shutdown')
        app.process.wait(timeout=45)
        app.process.stdout.close()
        app.Launch()
        resumed = app.Wait(lambda state: state.get('running'))
        assert resumed['selected_wallet_id'] == second_id and not resumed['unlocked'] and resumed['wallets'] == []
        # Existing profile files without wallet_id remain bound to the untouched legacy container.
        profile_path = app.root / 'data/networks' / a['id'] / 'profile.json'
        saved = json.loads(profile_path.read_text(encoding='utf-8'))
        saved.pop('wallet_id')
        profile_path.write_text(json.dumps(saved), encoding='utf-8')
        back = app.Api('network_switch', id=a['id'])
        assert back['selected_wallet_id'] == 'legacy' and not back['unlocked']
        app.Api('unlock', password=PASSWORD)
        checks.append('Each saved chain restores its bound wallet; cross-wallet chain switches and desktop restarts require login; legacy bindings import unchanged')

        c = app.Api('create', title='Wallet session funding', chat_version=3)
        c = app.Wait(lambda state: state.get('funding_state', {}).get('phase') == 'ready')
        port = c['network']['ports'][0]
        pid = app.NodeProcesses()[0].pid
        gate = FundingGate(port)
        invite = app.Api('invite', seeds=['127.0.0.1:' + str(gate.port)])
        app.Api('join', invite=invite)
        app.Api('wallet_select', id=second_id)
        app.Api('unlock', password=SECOND_PASSWORD)
        assert gate.first.wait(5), 'Funding discovery was not started'
        app.Api('logout')
        app.Api('unlock', password=SECOND_PASSWORD)
        gate.release_first.set()
        assert gate.second.wait(5), 'Old discovery was accepted instead of starting a fresh session discovery'
        with gate.lock:
            assert gate.requests == 0, 'A previous wallet session signed a new funding request'
        locked = app.Api('wallet_lock')
        assert not locked['unlocked'] and locked['wallets'] == []
        AssertPid(pid)
        gate.Close()
        gate = None
        checks.append('A held funding discovery is discarded after logout and login of the same key; async workers never authorize the new session')

        state = app.Api('status')
        assert all(set(row) == {'id', 'title', 'selected', 'has_wallet'} for row in state['wallet_profiles'])
        assert sum(row['selected'] for row in state['wallet_profiles']) == 1
        Public(state)
        Public(app.Api('diagnostics'))
        assert hashlib.sha256(legacy_path.read_bytes()).hexdigest() == legacy_hash
        (root / 'result.json').write_text(json.dumps({'passed': True, 'checks': checks,
            'wallet_count': len(state['wallet_profiles']), 'legacy_preserved': True}, indent=2), encoding='utf-8')
        print(json.dumps({'passed': True, 'checks': checks}))
    finally:
        if gate:
            try:
                if app and app.process.poll() is None:
                    app.Api('logout')
            finally:
                gate.Close()
        if app:
            app.Stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    Main(parser.parse_args())
