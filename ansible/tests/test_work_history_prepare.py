"""Offline coverage for history extraction, private files and owned processes."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import work_history_prepare as history
from work_genesis import MakeGenesis


class WorkHistoryTests(unittest.TestCase):
    def Prepare(self, root):
        prepared = root / 'prepared'
        prepared.mkdir()
        private = prepared / 'identities'
        private.mkdir(mode=0o700)
        identities, files = [], {}
        for index in range(6):
            key = SigningKey(bytes([index + 1]) * 32)
            name = f'node-{index}'
            path = private / (name + '.seed')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(bytes(key).hex())
            identities.append({'id': name, 'public_key': bytes(key.verify_key).hex()})
            files[name] = str(path)
        genesis = MakeGenesis({'accounts': [], 'base_fee': 1}, identities, 200)
        history.Save(prepared / 'genesis.json', genesis)
        history.Save(prepared / 'identities.json', identities)
        history.Save(prepared / 'variables.json', {'chain_cluster_identity_files': files,
                                                  'chain_genesis_sha256': history.Digest(prepared / 'genesis.json')})
        return prepared, identities, files

    def test_existing_genesis_and_external_identity_files_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared, identities, files = self.Prepare(root)
            before = {path: path.read_bytes() for path in prepared.rglob('*') if path.is_file()}
            binary = root / 'boost'
            binary.write_bytes(b'offline fixture')
            run = history.History(binary, prepared, root / 'out', 2400)
            self.assertEqual(run.private, {key: str(Path(value).resolve()) for key, value in files.items()})
            self.assertEqual(run.identities, identities)
            self.assertEqual(run.genesis_path.read_bytes(), before[prepared / 'genesis.json'])
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            self.assertFalse(list((root / 'out').rglob('*.seed')))
            self.assertEqual(run.nodes, {})

    def test_changed_consensus_or_mismatched_seed_rejected_before_process_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared, _, files = self.Prepare(root)
            path = prepared / 'genesis.json'
            original = path.read_text()
            changed = json.loads(original)
            changed['vdf']['slot_ms'] = 1
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, '200 ms'):
                history.Prepared(prepared)
            path.write_text(original)
            Path(files['node-0']).write_text('ab' * 32)
            with self.assertRaisesRegex(ValueError, 'does not match'):
                history.Prepared(prepared)

    def test_history_manifest_does_not_mark_uncomputed_work_snapshot_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared, _, files = self.Prepare(root)
            binary = root / 'boost'
            binary.write_bytes(b'offline fixture')
            run = history.History(binary, prepared, root / 'out', 2400)
            database = run.output / 'node0' / 'db'
            database.mkdir()
            (database / 'CURRENT').write_text('MANIFEST-000001\n')
            (database / 'MANIFEST-000001').write_bytes(b'fixture')
            history.Save(run.output / 'anchors.json', {'chain_id': '12' * 32, 'anchors': []})
            with mock.patch.object(run, 'Warm', return_value=0), \
                 mock.patch.object(run, 'Collect', return_value=('12' * 32, {'height': 5281, 'head': '34' * 32}, [{}] * 33)), \
                 mock.patch.object(history.subprocess, 'Popen') as popen:
                result = run.Run()
            popen.assert_not_called()
            self.assertTrue((run.output / 'history.json').is_file())
            self.assertFalse((run.output / 'snapshot.json').exists())
            self.assertFalse(Path(result['work_directory']).exists())
            self.assertEqual(result['transactions'], 0)
            self.assertEqual(result['ledger_sha256'], history.Digest(result['ledger_archive']))
            self.assertEqual(json.loads((run.output / 'history-result.json').read_text())['status'], 'history_completed')
            public = ''.join(path.read_text() for path in run.output.glob('*.json'))
            for path in files.values():
                self.assertNotIn(Path(path).read_text().strip(), public)

    def test_anchor_binds_chain_round_size_and_only_complete_rounds(self):
        chain = bytes(range(32))
        hashes = [hashlib.sha256(struct.pack('<Q', value)).digest() for value in range(323)]
        rows = history.RoundAnchors(chain.hex(), hashes)
        self.assertEqual(len(rows), 2)
        for index, row in enumerate(rows):
            expected = hashlib.blake2b(b'Chain/VdfConsensus/Anchor/v1' + chain + struct.pack('<QQ', index, 160)
                + b''.join(hashes[1 + index * 160:1 + (index + 1) * 160]), digest_size=32).hexdigest()
            self.assertEqual(row, {'round': index, 'anchor': expected})
        self.assertNotEqual(rows, history.RoundAnchors(('ff' * 32), hashes))

    def test_header_checks_empty_height_parent_and_hash(self):
        prefix = bytes(32) + bytes([3]) * 32 + struct.pack('<QQ', 1, 0)
        raw = prefix + history.Hash(prefix)
        self.assertEqual(history.BlockHeader(raw, 1), raw[80:])
        for invalid in (raw[:-1], raw[:80] + bytes(32), raw + b'x'):
            with self.assertRaises(ValueError):
                history.BlockHeader(invalid, 1)
        with self.assertRaises(ValueError):
            history.BlockHeader(raw, 2)
        with self.assertRaises(ValueError):
            history.BlockHeader(raw, 1, bytes([5]) * 32)
        occupied = bytes(64) + struct.pack('<QQ', 2, 1)
        with self.assertRaises(ValueError):
            history.BlockHeader(occupied + history.Hash(occupied) + bytes(176), 2)

    def test_archive_contains_only_rocksdb_files_and_no_local_keys_or_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / 'db'
            database.mkdir()
            names = ['CURRENT', 'MANIFEST-000001', 'OPTIONS-000002', '000003.sst', '000004.log']
            for name in names + ['IDENTITY', 'LOCK', 'LOG', 'LOG.old.1']:
                (database / name).write_text(name)
            work = database / 'work-vdf'
            work.mkdir()
            (work / 'private.seed').write_text('secret')
            destination = root / 'ledger.tar.gz'
            self.assertEqual(history.LedgerArchive(database, destination), len(names))
            with tarfile.open(destination) as archive:
                self.assertEqual(sorted(archive.getnames()), sorted('db/' + name for name in names))
                self.assertTrue(all(member.isfile() and member.mode == 0o600 for member in archive))
            (database / 'identity.seed').write_text('secret')
            with self.assertRaisesRegex(ValueError, 'Unexpected file'):
                history.LedgerArchive(database, root / 'bad.tar.gz')
            self.assertFalse((root / 'bad.tar.gz').exists())

    def test_archive_rejects_symlinks(self):
        if os.name == 'nt':
            self.skipTest('Windows symlink privileges are not required by offline tests')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / 'db'
            database.mkdir()
            (root / 'private').write_text('secret')
            (database / 'CURRENT').symlink_to(root / 'private')
            with self.assertRaisesRegex(ValueError, 'Unexpected file'):
                history.LedgerArchive(database, root / 'bad.tar.gz')

    def test_stop_attempts_every_owned_process_after_one_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = object.__new__(history.History)
            run.nodes, run.report = {}, {'stops': []}
            owned = []
            for index in range(3):
                process = mock.Mock(returncode=0)
                process.wait.return_value = 0
                if index == 0:
                    process.wait.side_effect = [subprocess.TimeoutExpired('node', 30), 0]
                    process.returncode = -9
                metrics = root / f'metrics-{index}.json'
                history.Save(metrics, {'failed': False, 'height': 5281, 'head': '00' * 32,
                                      'vdf': dict.fromkeys(['pending_proofs', 'pending_beacons', 'pending_bytes', 'work_priority_pending'], 0)})
                node = {'process': process, 'stop': root / f'stop-{index}', 'metrics': metrics, 'log': mock.Mock()}
                owned.append(node)
                run.nodes[index] = node
            with self.assertRaisesRegex(RuntimeError, 'node0'):
                run.Stop()
            self.assertFalse(run.nodes)
            self.assertEqual(len(run.report['stops']), 3)
            owned[0]['process'].kill.assert_called_once()
            for node in owned:
                node['process'].wait.assert_called()
                node['log'].close.assert_called_once()
                self.assertTrue(node['stop'].is_file())
            for node in owned[1:]:
                node['process'].kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
