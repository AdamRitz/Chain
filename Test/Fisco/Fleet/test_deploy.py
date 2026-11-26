import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('fisco_deploy', HERE/'deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        verifier = patch.object(deploy, 'verify_generated', return_value={'ok':True, 'leaf_certificates':200})
        verifier.start()
        self.addCleanup(verifier.stop)
        self.root = Path(self.temp.name)
        self.hosts = ['192.168.0.'+str(index) for index in range(1, 101)]
        self.inventory = self.root/'source-inventory.json'
        deploy.save(self.inventory, {'all': {'children': {'chain': {'hosts': {
            'node%03d'%index: {'ansible_host': host} for index, host in enumerate(self.hosts, 1)
        }}}}})
        self.args = argparse.Namespace(root=str(self.root), inventory=str(self.inventory), hosts=None,
            build_chain=str(self.root/'build_chain.sh'), binary=None, profile='pbft100', run='test-001',
            action='prepare', dry_run=True, ansible='ansible-playbook', connection=str(self.root/'connection.json'), forks=32)

    def generated(self):
        (self.root/'build_chain.sh').write_text('#!/bin/bash\n')
        deploy.save(self.root/'generation.json', {'hosts': self.hosts})
        for index, host in enumerate(self.hosts, 1):
            node = self.root/'generated'/host/'node0'
            (node/'conf').mkdir(parents=True)
            (node/'conf/node.nodeid').write_text('%0128x'%index)
            (node/'conf/node.pem').write_text('test-only-'+str(index))
            (node/'conf/ca.crt').write_text('test-only-ca')
            (node/'nodes.json').write_text('{}')
            (node/'config.ini').write_text('[consensus]\n    min_seal_time=500\n[executor]\n    enable_dag=true\n[sync]\n    send_txs_by_tree=false\n    sync_block_by_tree=false\n[txpool]\n    limit=15000\n[storage]\n    data_path=data\n[rpc]\n    thread_count=4\n')
            (node/'config.genesis').write_text('[consensus]\n    consensus_type=pbft\n    block_tx_count_limit=1000\n    consensus_timeout=3000\n    leader_period=1\n    epoch_sealer_num=4\n    epoch_block_num=1000\n' + ''.join('    node.%d=%0128x:1\n'%(n,n+1) for n in range(100)) + '[version]\n    compatibility_version=3.7.3\n[executor]\n    is_serial_execute=true\n')

    def test_inventory_requires_exact_authorized_100(self):
        hosts, inventory = deploy.load_inventory(self.args)
        self.assertEqual(hosts, self.hosts)
        self.assertEqual(inventory['node001']['fisco_host'], self.hosts[0])
        selected = self.root/'selected.json'
        self.args.hosts = str(selected)
        deploy.save(selected, self.hosts[:-1] + [self.hosts[0]])
        with self.assertRaises(ValueError): deploy.load_inventory(self.args)
        deploy.save(selected, self.hosts[:-1] + ['192.168.8.99'])
        with self.assertRaises(ValueError): deploy.load_inventory(self.args)

    def test_openssl_patch_preserves_official_script(self):
        script = self.root/'official.sh'
        content = ('#!/bin/bash\nOPENSSL_CMD="${HOME}/.fisco/tassl-1.1.1b"\nauth_mode="false"\nauth_admin_account=\n'
                   '[ v4_req ]\nbasicConstraints = CA:FALSE\n'
                   '${OPENSSL_CMD} req -new -x509 -days "${days}" -key "${chaindir}"/ca.key '
                   '-config "${cert_conf}" -out "${chaindir}"/ca.crt 2>/dev/null\n')
        script.write_text(content)
        openssl = self.root/'openssl'
        openssl.write_bytes(b'test executable')
        result = type('Result', (), {'stdout':'OpenSSL 3.5.9\n'})()
        with patch.object(deploy, 'command', return_value=result):
            patched, metadata = deploy.generation_script(script, self.root, str(openssl))
        self.assertEqual(script.read_text(), content)
        self.assertEqual(metadata['official_script_sha256'], deploy.sha(script))
        self.assertEqual(metadata['patched_script_sha256'], deploy.sha(patched))
        self.assertIn('auth_mode="false"', patched.read_text())
        self.assertIn('auth_admin_account='+deploy.UNUSED_ADMIN, patched.read_text())
        self.assertIn('basicConstraints=critical,CA:TRUE', patched.read_text())
        self.assertIn('keyUsage=critical,keyCertSign,cRLSign', patched.read_text())
        self.assertIn('-extensions v3_ca -out "${chaindir}"/ca.crt', patched.read_text())

    def test_prepare_recovers_existing_complete_generation_before_packaging(self):
        self.generated()
        (self.root/'generation.json').unlink()
        self.args.openssl = '/test/openssl'
        def recover(*args):
            deploy.save(self.root/'generation.json', {'hosts':self.hosts, 'recovered_existing_identities':True})
        with patch.object(deploy, 'generation_script', return_value=(self.root/'build_chain.sh', {})), \
                patch.object(deploy, 'recover_generation', side_effect=recover) as recovery, \
                contextlib.redirect_stdout(io.StringIO()):
            deploy.prepare(self.args)
        recovery.assert_called_once()
        self.assertTrue((self.root/'profiles/pbft100/manifest.json').is_file())

    def test_prepare_profiles_are_consistent_and_repeatable(self):
        self.generated()
        with contextlib.redirect_stdout(io.StringIO()): deploy.prepare(self.args)
        first = deploy.read(self.root/'profiles/pbft100/manifest.json')
        self.assertEqual(first['committee'], 100)
        self.assertEqual(first['leader_period'], 1)
        self.assertEqual(first['txpool_limit'], 200000)
        second = deploy.read(self.root/'profiles/rpbft5/manifest.json')
        self.assertEqual(second['committee'], 5)
        self.assertEqual(second['epoch_block_num'], 1000)
        self.assertEqual(len(second['nodes']), 100)
        genesis = (self.root/'profiles/rpbft5/node001/node/config.genesis').read_text()
        self.assertIn('consensus_type=rpbft', genesis)
        self.assertIn('is_serial_execute=false', genesis)
        self.assertIn('consensus_timeout=3000', genesis)
        config = (self.root/'profiles/pbft100/node001/node/config.ini').read_text()
        self.assertIn('thread_count=4', config)
        self.assertIn('send_txs_by_tree=true', config)
        with contextlib.redirect_stdout(io.StringIO()): deploy.prepare(self.args)
        self.assertEqual(first, deploy.read(self.root/'profiles/pbft100/manifest.json'))
        with tarfile.open(self.root/'profiles/pbft100/node001.tar.gz') as archive:
            names = archive.getnames()
            self.assertNotIn('node/fisco-bcos', names)
            self.assertNotIn('node/data', names)
            self.assertIn('node/conf/node.pem', names)

    def test_remote_stage_rejects_escaping_tar_and_is_retryable(self):
        profile = self.root/'profiles/pbft100'
        profile.mkdir(parents=True)
        archive = profile/'node.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            entry = tarfile.TarInfo('../../escape')
            entry.size = 1
            output.addfile(entry, io.BytesIO(b'x'))
        self.args.action = 'remote-stage'
        self.args.archive_sha256 = deploy.sha(archive)
        with self.assertRaises(ValueError): deploy.remote(self.args)
        (self.root/'source/node/conf').mkdir(parents=True)
        (self.root/'source/node/conf/node.pem').write_text('test')
        deploy.pack_node(self.root/'source/node', archive)
        self.args.archive_sha256 = deploy.sha(archive)
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.remote(self.args)
            deploy.remote(self.args)
        self.assertEqual(deploy.read(profile/'installed.json')['owner'], deploy.OWNER)

    def test_remote_stage_rejects_symlink_tar(self):
        profile = self.root/'profiles/pbft100'
        profile.mkdir(parents=True)
        archive = profile/'node.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            entry = tarfile.TarInfo('node/link')
            entry.type = tarfile.SYMTYPE
            entry.linkname = '/etc/passwd'
            output.addfile(entry)
        self.args.action = 'remote-stage'
        self.args.archive_sha256 = deploy.sha(archive)
        with self.assertRaises(ValueError): deploy.remote(self.args)

    def test_cleanup_requires_saved_evidence_and_stopped_processes(self):
        run = self.root/'runs/test-001'
        (run/'node/data/sub').mkdir(parents=True)
        (run/'node/data/sub/00001.sst').write_bytes(b'x'*10)
        (run/'node/config.ini').write_text('[storage]\ndata_path=data\n')
        deploy.save(run/'owner.json', {'owner':deploy.OWNER, 'run':'test-001', 'profile':'pbft100'})
        self.args.action = 'remote-clean'
        self.args.evidence_sha256 = 'mismatch'
        with patch.object(deploy, 'RUN_ROOT', self.root/'runs'), patch.object(deploy, 'active_processes', return_value=[]), patch.object(deploy, 'unit_show', return_value=''):
            with self.assertRaises(RuntimeError): deploy.remote(self.args)
            deploy.save(run/'stopped.json', {'stopped':True})
            (run/'evidence.tar.gz').write_bytes(b'saved evidence')
            with self.assertRaises(ValueError): deploy.remote(self.args)
            self.assertTrue((run/'node/data').exists())
            self.args.evidence_sha256 = deploy.sha(run/'evidence.tar.gz')
            # Windows has no fcntl; this fixture contains no RocksDB LOCK files.
            with patch.dict('sys.modules', {'fcntl':type('Fcntl', (), {})}):
                with contextlib.redirect_stdout(io.StringIO()): deploy.remote(self.args)
            self.assertFalse((run/'node/data').exists())
            self.assertTrue((run/'node/config.ini').exists())
            self.assertTrue((run/'evidence.tar.gz').exists())

    def test_start_playbook_has_no_persistent_service_or_credentials(self):
        self.generated()
        with contextlib.redirect_stdout(io.StringIO()): deploy.prepare(self.args)
        self.args.action = 'start'
        with contextlib.redirect_stdout(io.StringIO()): deploy.play(self.args)
        path = self.root/'results/test-001/start.json'
        text = path.read_text()
        self.assertIn('remote-start', text)
        self.assertNotIn('ansible_password', text)
        self.assertNotIn('enable', text)

    def test_stage_distributes_certificate_module_with_remote_controller(self):
        self.generated()
        with contextlib.redirect_stdout(io.StringIO()): deploy.prepare(self.args)
        (self.root/'bin').mkdir()
        (self.root/'bin/fisco-bcos').write_bytes(b'fixture binary')
        self.args.action = 'stage'
        version = type('Result', (), {'stdout':'FISCO BCOS 3.7.3', 'stderr':''})()
        with patch.object(deploy, 'command', return_value=version), contextlib.redirect_stdout(io.StringIO()):
            deploy.play(self.args)
        tasks = deploy.read(self.root/'results/test-001/stage.json')[0]['tasks']
        copies = [task['ansible.builtin.copy'] for task in tasks if 'ansible.builtin.copy' in task]
        self.assertTrue(any(Path(row['dest']).resolve() == (self.root/'tools/certificates.py').resolve() and Path(row['src']).is_file()
                            for row in copies))

    def test_stage_refuses_invalid_certificates_before_launching_ansible(self):
        self.generated()
        with contextlib.redirect_stdout(io.StringIO()): deploy.prepare(self.args)
        self.args.action = 'stage'
        with patch.object(deploy, 'verify_generated', side_effect=ValueError('Invalid CA')), \
                patch.object(deploy, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'Invalid CA'):
                deploy.play(self.args)
        command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
