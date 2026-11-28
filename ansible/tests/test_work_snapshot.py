"""Shared real history is immutable; every restored DB has only its own Work."""
import io
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'Test'))
import work_snapshot as snapshot
from work_genesis import MakeGenesis
import ClusterWorker as worker


@contextmanager
def Temporary():
    root = Path(tempfile.mkdtemp()).resolve()
    try:
        yield str(root)
    finally:
        if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise ValueError('Temporary cleanup escaped its root')
        shutil.rmtree(snapshot.Native(root))


def Archive(path, files):
    with tarfile.open(path, 'w:gz') as target:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            target.addfile(item, io.BytesIO(data))


class WorkSnapshotTests(unittest.TestCase):
    def Fixture(self, root, height=5281):
        keys, files, identities = [], {}, []
        for n in range(5):
            key = SigningKey(bytes([n+1])*32)
            public = bytes(key.verify_key).hex()
            name = 'node'+str(n)
            path = root/(name+'.seed')
            path.write_text(bytes(key).hex())
            keys.append(public)
            files[name] = str(path)
            identities.append({'id': name, 'public_key': public})
        accounts = {'base_fee': 0, 'accounts': [{'public_key': keys[0], 'balance': 1000}]}
        genesis = MakeGenesis(accounts, identities, 200)
        genesis['vdf']['start_time_ms'] = 1234000
        genesis_path = root/'genesis.json'
        genesis_path.write_text(json.dumps(genesis))
        ledger = root/'ledger.tar.gz'
        Archive(ledger, {'db/CURRENT': b'MANIFEST-000001\n', 'db/MANIFEST-000001': b'database'})
        chain = 'ab'*32
        work_root = root/'cache'
        for public in keys:
            directory = snapshot.Native(work_root/'work-vdf'/chain/public/'65536'/'proofs')
            directory.mkdir(parents=True)
            previous = bytes(100)
            for n in range((height-1)//160):
                anchor, output = bytes([n])*32, bytes([n+1])*100
                data = b'WORK\x01\0'+struct.pack('<Q', n)+anchor+previous+output+bytes(100)
                (directory/(str(n)+'-'+anchor.hex()+'.work')).write_bytes(data)
                previous = output
        record = {'schema': 1, 'genesis': str(genesis_path), 'genesis_sha256': snapshot.Digest(genesis_path),
                  'chain_id': chain, 'head': 'cd'*32, 'height': height, 'transactions': 0,
                  'ledger_archive': str(ledger), 'ledger_sha256': snapshot.Digest(ledger),
                  'work_directory': str(work_root), 'identity_files': files}
        anchors = root/'anchors.json'
        anchors.write_text(json.dumps({'chain_id': chain, 'height': height, 'head': record['head'], 'iterations': 65536,
            'anchors': [{'round': n, 'anchor': (bytes([n])*32).hex()} for n in range((height-1)//160)]}))
        record.update(anchors_file=str(anchors), anchors_sha256=snapshot.Digest(anchors))
        path = root/'snapshot.json'
        path.write_text(json.dumps(record))
        plan = {'nodes': [{'id': item['id']} for item in identities]}
        output = root/'prepared'
        output.mkdir()
        return path, plan, accounts, output, record, keys

    def test_prepare_reuses_original_time_keys_and_packages_all_completed_rounds(self):
        with Temporary() as temporary:
            root = Path(temporary).resolve()
            path, plan, accounts, output, source, keys = self.Fixture(root, 5442)
            genesis, identities, ready = snapshot.Prepare(path, plan, accounts, output)
            self.assertEqual(genesis, json.loads(Path(source['genesis']).read_text()))
            self.assertEqual(genesis['vdf']['start_time_ms'], 1234000)
            self.assertEqual(identities, source['identity_files'])
            self.assertEqual(ready['rounds'], 34)
            for index, node in enumerate(plan['nodes']):
                archive = ready['work_archives'][node['id']]
                with tarfile.open(archive['archive']) as packaged:
                    names = packaged.getnames()
                    self.assertEqual(len(names), 34)
                    self.assertTrue(all('/'+keys[index]+'/' in name and name.endswith('.work') for name in names))
                    self.assertFalse(any('.seed' in name or '/secrets/' in name for name in names))

    def test_missing_work_or_changed_previous_output_rejected(self):
        for missing in (True, False):
            with self.subTest(missing=missing), Temporary() as temporary:
                path, plan, accounts, output, record, keys = self.Fixture(Path(temporary).resolve())
                directory = snapshot.Native(Path(record['work_directory'])/'work-vdf'/record['chain_id']/keys[0]/'65536'/'proofs')
                proof = directory/('1-'+'01'*32+'.work')
                if missing:
                    proof.unlink()
                else:
                    value = bytearray(proof.read_bytes())
                    value[46] ^= 1
                    proof.write_bytes(value)
                with self.assertRaises(ValueError):
                    snapshot.Prepare(path, plan, accounts, output)

    def test_consistent_work_file_from_another_branch_is_rejected(self):
        with Temporary() as temporary:
            path, plan, accounts, output, record, keys = self.Fixture(Path(temporary).resolve())
            directory = snapshot.Native(Path(record['work_directory'])/'work-vdf'/record['chain_id']/keys[0]/'65536'/'proofs')
            original = directory/('1-'+'01'*32+'.work')
            changed = bytearray(original.read_bytes())
            changed[14:46] = bytes.fromhex('ff'*32)
            original.unlink()
            (directory/('1-'+'ff'*32+'.work')).write_bytes(changed)
            with self.assertRaisesRegex(ValueError, 'snapshot'):
                snapshot.Prepare(path, plan, accounts, output)

    def test_restore_builds_independent_ledger_with_only_own_work_and_retains_sources(self):
        with Temporary() as temporary:
            root = Path(temporary).resolve()
            path, plan, accounts, output, record, keys = self.Fixture(root)
            _, _, ready = snapshot.Prepare(path, plan, accounts, output)
            staging = root/'test'/'.work-snapshots'/'run1'
            staging.mkdir(parents=True)
            own = ready['work_archives']['node0']
            shutil.copyfile(ready['ledger_archive'], staging/'ledger.tar.gz')
            shutil.copyfile(own['archive'], staging/'node0.tar.gz')
            metadata = {'chain_id': ready['chain_id'], 'public_key': own['public_key'], 'rounds': ready['rounds'],
                        'iterations': ready['iterations'], 'ledger_sha256': ready['ledger_sha256'], 'work_sha256': own['sha256']}
            restored = snapshot.Restore(root/'test', 'run1', 'node0', metadata)
            self.assertEqual((restored/'CURRENT').read_bytes(), b'MANIFEST-000001\n')
            self.assertEqual([item.name for item in (restored/'work-vdf'/record['chain_id']).iterdir()], [keys[0]])
            self.assertEqual(len(list(snapshot.Native(restored/'work-vdf'/record['chain_id']/keys[0]/'65536'/'proofs').glob('*.work'))), 33)
            self.assertTrue((staging/'ledger.tar.gz').is_file())
            with self.assertRaises(FileExistsError):
                snapshot.Restore(root/'test', 'run1', 'node0', metadata)

    def test_archive_rejects_path_traversal_private_files_links_and_hash_change(self):
        for name in ('../escape', 'db/../escape', '/escape', 'db/private.seed', 'db/process.json', 'db/work-vdf/0.work'):
            with self.subTest(name=name), Temporary() as temporary:
                root = Path(temporary).resolve()
                path = root/'archive.tar.gz'
                Archive(path, {name: b'bad'})
                with self.assertRaises(ValueError):
                    snapshot.Extract(path, snapshot.Digest(path), root/'db')
                self.assertFalse((root/'escape').exists())
        with Temporary() as temporary:
            root = Path(temporary).resolve()
            path = root/'link.tar.gz'
            with tarfile.open(path, 'w:gz') as target:
                item = tarfile.TarInfo('db/CURRENT')
                item.type, item.linkname = tarfile.SYMTYPE, '/etc/passwd'
                target.addfile(item)
            with self.assertRaisesRegex(ValueError, 'ordinary files'):
                snapshot.Extract(path, snapshot.Digest(path), root/'db')
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                snapshot.Extract(path, 'ab'*32, root/'db')

    def test_worker_copies_only_into_fresh_case_and_copy_failure_leaves_source(self):
        with Temporary() as temporary:
            root = Path(temporary).resolve()
            source = root/'retained'
            source.mkdir()
            (source/'CURRENT').write_text('MANIFEST-000001\n')
            (source/'MANIFEST-000001').write_bytes(b'unchanged')
            deep = snapshot.Native(source/'work-vdf'/('ab'*32)/('cd'*32)/'65536'/'proofs'/('0-'+'ef'*32+'.work'))
            deep.parent.mkdir(parents=True)
            deep.write_bytes(b'deep-path fixture')
            binary = root/'boost'
            binary.write_bytes(b'executable fixture')
            request = {'root': str(root/'tests'), 'run': 'test1', 'node': 'node0', 'action': 'start',
                       'binary': str(binary), 'genesis': {}, 'arguments': [], 'prepared_database': str(source)}
            with mock.patch.object(worker.subprocess, 'Popen', return_value=mock.Mock(pid=12)) as popen, \
                 mock.patch.object(worker.psutil, 'Process', return_value=mock.Mock(create_time=lambda: 123)), \
                 mock.patch.object(worker.platform, 'platform', return_value='Test'):
                result = worker.Handle(request)
            destination = Path(result['directory'])/'db'
            (destination/'MANIFEST-000001').write_bytes(b'changed')
            self.assertEqual((source/'MANIFEST-000001').read_bytes(), b'unchanged')
            self.assertEqual(snapshot.Native(destination/'work-vdf'/('ab'*32)/('cd'*32)/'65536'/'proofs'/deep.name).read_bytes(), b'deep-path fixture')
            self.assertIn(str(destination), popen.call_args.args[0])
            with self.assertRaises(FileExistsError):
                worker.Handle(request)
            with self.assertRaisesRegex(ValueError, 'new experiment'):
                worker.Handle({**request, 'restart': True})
            failed = {**request, 'run': 'test2'}
            def FailedCopy(original, target):
                target.mkdir()
                (target/'partial').write_text('partial')
                raise OSError('copy failure')
            with mock.patch.object(worker.shutil, 'copytree', side_effect=FailedCopy):
                with self.assertRaisesRegex(OSError, 'copy failure'):
                    worker.Handle(failed)
            self.assertFalse(worker.CaseDirectory(failed).exists())
            self.assertTrue(source.is_dir())


if __name__ == '__main__':
    unittest.main()
