"""Build shared Work history with five real local nodes and unchanged genesis.

  python work_history_prepare.py --bin /path/to/bin --prepared /path/to/prepare \
      --output /path/to/new-history

After this command, run work_prepare with anchors.json, the prepared public
identities.json, and --out OUTPUT/work-cache. history.json describes the common
database. Write the final snapshot.json after all identities' Work files pass
verification.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import struct
import subprocess
import tarfile
import time

from work_genesis import PublicKey, ReadJson


FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
ROCKS_FILE = re.compile(r'(CURRENT|MANIFEST-\d+|OPTIONS-\d+|\d+\.(sst|ldb|log|blob))\Z')


def Digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def Save(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def Hash(value):
    return hashlib.blake2b(value, digest_size=32).digest()


def Prepared(directory):
    """Validate public identities against external private files; copy no keys."""
    from nacl.signing import SigningKey
    directory = Path(directory).resolve(strict=True)
    genesis_path = directory / 'genesis.json'
    genesis = ReadJson(genesis_path, 128 * 1024 * 1024)
    identities = ReadJson(directory / 'identities.json', 32 * 1024 * 1024)
    variables = ReadJson(directory / 'variables.json', 32 * 1024 * 1024)
    config = genesis.get('vdf', {})
    expected = {'enabled': True, 'work_election': True, 'producer_rotation': True,
                'epoch_blocks': 160, 'consecutive_blocks': 32, 'slot_ms': 200,
                'producers': 5, 'work_iterations': 65536, 'work_groups': 4, 'work_group_size': 8}
    if any(config.get(key) != value for key, value in expected.items()):
        raise ValueError('Prepared genesis must use 160 blocks, 200 ms, five producers and Work T=65536 with 4x8 proofs')
    if not isinstance(identities, list) or not 5 <= len(identities) <= 100000:
        raise ValueError('Prepared identities must contain at least five nodes')
    files = variables.get('chain_cluster_identity_files', {})
    public, resolved, seen = [], {}, set()
    for row in identities:
        name, key = row.get('id'), PublicKey(row.get('public_key'))
        if not isinstance(name, str) or not name or name in resolved or key in seen:
            raise ValueError('Prepared identity ids and public keys must be unique')
        path = Path(files.get(name, ''))
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 128:
            raise ValueError('Every prepared identity requires its existing absolute private seed file')
        if os.name != 'nt' and (path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077):
            raise ValueError('Identity files require mode 0600 and their directory mode 0700')
        seed = path.read_text(encoding='ascii').strip()
        if not re.fullmatch(r'[0-9a-fA-F]{64}', seed):
            raise ValueError('Invalid identity seed file')
        if bytes(SigningKey(bytes.fromhex(seed)).verify_key).hex() != key:
            raise ValueError('Prepared public identity does not match its seed file')
        resolved[name] = str(path.resolve(strict=True))
        public.append({'id': name, 'public_key': key})
        seen.add(key)
    if set(files) != set(resolved) or config.get('bootstrap_producers') != [row['public_key'] for row in public[:5]]:
        raise ValueError('The first five prepared identities must be the genesis committee')
    if variables.get('chain_genesis_sha256') not in (None, Digest(genesis_path)):
        raise ValueError('Prepared genesis digest mismatch')
    return genesis_path, genesis, public, resolved


class Rpc:
    def __init__(self, port):
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.socket.sendall(b'\x02')

    def Read(self, size):
        result = bytearray()
        while len(result) < size:
            part = self.socket.recv(size - len(result))
            if not part:
                raise ConnectionError('Node closed its RPC connection')
            result.extend(part)
        return bytes(result)

    def Query(self, kind, payload=b'', raw=False):
        self.socket.sendall(struct.pack('<BI', kind, len(payload)) + payload)
        response, size = struct.unpack('<BI', self.Read(5))
        if response != kind + 1 or size > 2 * 1024 * 1024:
            raise ValueError('Invalid RPC response frame')
        body = self.Read(size)
        return body if raw else json.loads(body)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.socket.close()


def Query(port, kind, payload=b'', raw=False):
    with Rpc(port) as client:
        return client.Query(kind, payload, raw)


def FreePorts(count):
    probes = [socket.socket() for _ in range(count)]
    try:
        for probe in probes:
            probe.bind(('127.0.0.1', 0))
        return [probe.getsockname()[1] for probe in probes]
    finally:
        for probe in probes:
            probe.close()


def BlockHeader(raw, height, parent=None):
    if len(raw) < 112 or Hash(raw[:80]) != raw[80:112]:
        raise ValueError('Invalid canonical block header hash')
    actual, count = struct.unpack_from('<QQ', raw, 64)
    if actual != height or count != 0 or (parent is not None and raw[:32] != parent):
        raise ValueError('Canonical history must be a continuous chain of empty blocks')
    if height == 1 and len(raw) != 112:
        raise ValueError('Invalid genesis block length')
    return raw[80:112]


def RoundAnchors(chain_id, hashes, size=160):
    if len(hashes) < 1:
        raise ValueError('Missing genesis block hash')
    return [{'round': number, 'anchor': Hash(b'Chain/VdfConsensus/Anchor/v1' + bytes.fromhex(chain_id)
            + struct.pack('<QQ', number, size) + b''.join(hashes[1 + number * size:1 + (number + 1) * size])).hex()}
            for number in range((len(hashes) - 1) // size)]


def LedgerArchive(database, destination):
    """Copy only stopped RocksDB files; Work caches and private files stay out."""
    database = Path(database)
    files = []
    for path in database.iterdir():
        if path.name in ('work-vdf', 'IDENTITY', 'LOCK', 'LOG') or path.name.startswith('LOG.old.'):
            continue
        if path.is_symlink() or not path.is_file() or not ROCKS_FILE.fullmatch(path.name):
            raise ValueError('Unexpected file in the stopped database: ' + path.name)
        files.append(path)
    if not any(path.name == 'CURRENT' for path in files) or not any(path.name.startswith('MANIFEST-') for path in files):
        raise ValueError('Stopped database is missing CURRENT or MANIFEST')
    with tarfile.open(destination, 'x:gz') as archive:
        for path in sorted(files):
            item = archive.gettarinfo(str(path), arcname='db/' + path.name)
            item.mode, item.uid, item.gid, item.uname, item.gname = 0o600, 0, 0, '', ''
            with path.open('rb') as source:
                archive.addfile(item, source)
    return len(files)


class History:
    def __init__(self, binary, prepared, output, timeout):
        self.binary, self.output = Path(binary), Path(output)
        if self.binary.is_dir():
            self.binary /= 'boost.exe' if os.name == 'nt' else 'boost'
        self.binary = self.binary.resolve(strict=True)
        if not self.binary.is_file():
            raise ValueError('Node binary must be a regular file')
        self.genesis_path, self.genesis, self.identities, self.private = Prepared(prepared)
        self.output = self.output.resolve()
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        shutil.copyfile(self.genesis_path, self.output / 'genesis.json')
        Save(self.output / 'identities.json', self.identities)
        self.genesis_path = self.output / 'genesis.json'
        self.deadline = time.monotonic() + timeout
        self.timeout, self.ports, self.nodes = timeout, FreePorts(5), {}
        self.generations = [0] * 5
        self.report = {'status': 'running', 'starts': [], 'stops': [], 'target_height': 5281,
                       'genesis_sha256': Digest(self.genesis_path), 'binary_sha256': Digest(self.binary)}
        for index in range(5):
            (self.output / f'node{index}').mkdir(mode=0o700)

    def Budget(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Work history preparation exceeded its time budget')
        for index, node in self.nodes.items():
            if node['process'].poll() is not None:
                raise RuntimeError(f'Local node {index} exited; see its node log')

    def Start(self, indices, produce):
        for index in indices:
            self.Budget()
            if index in self.nodes:
                raise RuntimeError('Node database is already open')
            directory = self.output / f'node{index}'
            generation = self.generations[index]
            self.generations[index] += 1
            config, stop, metrics = [directory / f'{name}-{generation}{suffix}'
                                     for name, suffix in [('config', '.json'), ('stop', ''), ('metrics', '.json')]]
            peers = [f'127.0.0.1:{self.ports[other]}' for other in indices if other != index] if produce else []
            Save(config, {'node': {'peers': peers}})
            options = {'config': config, 'data': directory / 'db', 'genesis': self.genesis_path,
                       'bind': '127.0.0.1', 'port': self.ports[index], 'produce': int(produce),
                       'sync': 1, 'block-ms': 200, 'max-block-txs': 6000, 'max-peers': 16,
                       'io-threads': 1, 'verify-threads': 1, 'candidate-threads': 1,
                       'block-verify-threads': 1, 'query-threads': 1, 'clock-sync': 0,
                       'peer-discovery': 0, 'experiment-index': 1, 'stop-file': stop, 'metrics': metrics,
                       'run-seconds': max(1, int(self.deadline - time.monotonic()) + 30)}
            options['vdf-key'] = self.private[self.identities[index]['id']]
            command = [str(self.binary)]
            for key, value in options.items():
                command.extend(['--' + key, str(value)])
            log = (directory / f'node-{generation}.log').open('w', encoding='utf-8')
            try:
                process = subprocess.Popen(command, cwd=directory, stdout=log, stderr=log, creationflags=FLAGS)
            except BaseException:
                log.close()
                raise
            self.nodes[index] = {'process': process, 'log': log, 'stop': stop, 'metrics': metrics}
            self.report['starts'].append({'index': index, 'generation': generation, 'pid': process.pid, 'produce': produce})
        self.Wait(lambda: self.Stats(indices), 'node startup')

    def Stats(self, indices):
        with ThreadPoolExecutor(max_workers=len(indices)) as executor:
            rows = list(executor.map(lambda index: Query(self.ports[index], 13), indices))
        if any(row.get('failed') or row.get('chain_transactions') != 0 for row in rows):
            raise ValueError('History node failed or accepted a transaction')
        return rows if all(row.get('vdf', {}).get('work_election') for row in rows) else None

    def Wait(self, action, label):
        while True:
            self.Budget()
            try:
                result = action()
                if result:
                    return result
            except (ConnectionError, OSError, TimeoutError):
                pass
            time.sleep(.25)

    def Stop(self):
        errors, owned = [], list(self.nodes.items())
        for index, node in owned:
            try:
                node['stop'].touch()
            except OSError as error:
                errors.append(f'node{index} stop file: {error}')
        end = time.monotonic() + 30
        for index, node in owned:
            process = node['process']
            result = None
            try:
                result = process.wait(timeout=max(.01, end - time.monotonic()))
            except BaseException as error:
                errors.append(f'node{index} wait failed: {type(error).__name__}')
                try:
                    process.kill()
                    process.wait(timeout=10)
                    result = process.returncode
                except BaseException as cleanup:
                    errors.append(f'node{index} kill failed: {type(cleanup).__name__}')
            finally:
                node['log'].close()
                self.nodes.pop(index, None)
            try:
                metrics = ReadJson(node['metrics'], 2 * 1024 * 1024)
            except (OSError, ValueError):
                metrics = {}
            work = metrics.get('vdf', {})
            record = {'index': index, 'exit_code': result, 'height': metrics.get('height'),
                      'head': metrics.get('head'), 'failed': metrics.get('failed'),
                      'pending': {key: work.get(key) for key in ('pending_proofs', 'pending_beacons', 'pending_bytes', 'work_priority_pending')}}
            self.report['stops'].append(record)
            if result != 0 or metrics.get('failed') is not False or any(value != 0 for value in record['pending'].values()):
                errors.append(f'node{index} did not stop cleanly')
        if errors:
            raise RuntimeError('; '.join(errors))

    def Warm(self):
        self.Start(list(range(5)), True)
        last_print = 0

        def Complete():
            nonlocal last_print
            rows = self.Stats(list(range(5)))
            if not rows:
                return None
            minimum = min(row['height'] for row in rows)
            if time.monotonic() - last_print >= 30:
                print(json.dumps({'phase': 'history', 'height': minimum, 'target': 5281}), flush=True)
                last_print = time.monotonic()
            if minimum < 5281:
                return None
            with ThreadPoolExecutor(max_workers=5) as executor:
                heads = list(executor.map(lambda index: Query(self.ports[index], 29, struct.pack('<Q', minimum)), range(5)))
            if heads[0] and heads[0]['transactions'] == 0 and all(head == heads[0] for head in heads):
                self.report['common_before_stop'] = heads[0]
                return max(range(5), key=lambda index: rows[index]['height']) + 1
            return None

        source = self.Wait(Complete, '33 complete common rounds') - 1
        self.Stop()
        return source

    def Collect(self, source):
        self.Start([source], False)
        before = self.Stats([source])[0]
        hashes, chain_id = [], None
        with Rpc(self.ports[source]) as client:
            for height in range(1, before['height'] + 1):
                self.Budget()
                raw = client.Query(6, struct.pack('<Q', height), raw=True)
                hashes.append(BlockHeader(raw, height, hashes[-1] if hashes else None))
                if height == 1:
                    chain_id = raw[32:64].hex()
            state = client.Query(35)
        after = self.Stats([source])[0]
        if before['head'] != after['head'] or hashes[-1].hex() != after['head'] or state['transactions'] != 0:
            raise ValueError('Source canonical chain changed while collecting history')
        anchors = RoundAnchors(chain_id, hashes)
        if len(anchors) < 33:
            raise ValueError('Source must contain at least 33 complete rounds')
        Save(self.output / 'anchors.json', {'chain_id': chain_id, 'iterations': 65536, 'anchors': anchors,
                                          'height': after['height'], 'head': after['head'], 'epoch_blocks': 160})
        self.Stop()
        return chain_id, after, anchors

    def Run(self):
        started = time.monotonic()
        try:
            source = self.Warm()
            chain_id, state, anchors = self.Collect(source)
            archive = self.output / 'ledger.tar.gz'
            files = LedgerArchive(self.output / f'node{source}' / 'db', archive)
            snapshot = {'schema': 1, 'genesis': str(self.genesis_path), 'genesis_sha256': Digest(self.genesis_path),
                        'chain_id': chain_id, 'height': state['height'], 'head': state['head'], 'transactions': 0,
                        'anchors_file': str(self.output / 'anchors.json'), 'anchors_sha256': Digest(self.output / 'anchors.json'),
                        'ledger_archive': str(archive), 'ledger_sha256': Digest(archive),
                        'work_directory': str(self.output / 'work-cache'), 'identity_files': self.private}
            Save(self.output / 'history.json', snapshot)
            self.report.update(status='history_completed', source_node=source, complete_rounds=len(anchors),
                               archive_files=files, history=str(self.output / 'history.json'))
            return snapshot
        except BaseException as error:
            self.report.update(status='failed', error=str(error), error_type=type(error).__name__)
            raise
        finally:
            try:
                self.Stop()
            except BaseException as error:
                self.report.update(status='failed', cleanup_error=str(error))
                raise
            finally:
                self.report['elapsed_seconds'] = time.monotonic() - started
                Save(self.output / 'history-result.json', self.report)


def Main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bin', type=Path, required=True, help='Complete node executable or its bin directory')
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New directory')
    parser.add_argument('--timeout', type=float, default=2400)
    args = parser.parse_args(argv)
    if not 1100 <= args.timeout <= 7200:
        parser.error('--timeout must be between 1100 and 7200 seconds')
    def Interrupted(signum, _frame):
        raise KeyboardInterrupt(f'Interrupted by signal {signum}')
    previous = {number: signal.signal(number, Interrupted) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        snapshot = History(args.bin, args.prepared, args.output, args.timeout).Run()
        print(json.dumps({'status': 'history_completed', 'history': str(args.output.resolve() / 'history.json'),
                          'height': snapshot['height'], 'work_directory': snapshot['work_directory']}), flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError, KeyboardInterrupt) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}), flush=True)
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    raise SystemExit(Main())
