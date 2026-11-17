import argparse
import errno
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def Load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage = Load('stage_release')
worker = Load('managed_worker')
activate = Load('activate_release')


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.files = {'bin/boost': b'\x7fELFnode', 'bin/sender': b'\x7fELFsender',
                      'source/Test/ClusterWorker.py': b'# original worker\n', 'source/Test/Netem.py': b'# original netem\n'}

    def tearDown(self):
        self.temporary.cleanup()

    def Package(self, extra=None, symlink=False):
        manifest = {'schema': 1, 'source_commit': 'a' * 40, 'files': [
            {'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'mode': '0755' if name.startswith('bin/') else '0644'}
            for name, data in self.files.items()]}
        archive = self.root / 'package.tar.gz'
        members = {**self.files, 'manifest.json': json.dumps(manifest).encode(), **(extra or {})}
        with tarfile.open(archive, 'w:gz') as output:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                output.addfile(info, io.BytesIO(data))
            if symlink:
                info = tarfile.TarInfo('escape')
                info.type, info.linkname = tarfile.SYMTYPE, '../../outside'
                output.addfile(info)
        return archive, stage.Digest(archive)

    def test_publish_idempotence_and_tamper_detection(self):
        archive, digest = self.Package()
        releases = self.root / 'releases'
        first = stage.Stage(archive, releases, 'r1', digest)
        self.assertTrue(first['changed'])
        self.assertEqual(first['binary_sha256'], hashlib.sha256(self.files['bin/boost']).hexdigest())
        self.assertFalse(stage.Stage(archive, releases, 'r1', digest)['changed'])
        (releases / 'r1/bin/boost').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            stage.Stage(archive, releases, 'r1', digest)

    def test_worker_and_transitive_imports_leave_immutable_release_unchanged(self):
        self.files['source/Test/ClusterWorker.py'] = b'import Netem\nVALUE = Netem.MARKER\n'
        self.files['source/Test/Netem.py'] = b'MARKER = 42\n'
        archive, digest = self.Package()
        releases = self.root / 'releases'
        release = Path(stage.Stage(archive, releases, 'r1', digest)['directory'])
        before = {path.relative_to(release): path.read_bytes() for path in release.rglob('*') if path.is_file()}
        # Start from Python's normal bytecode-writing setting and execute the
        # real adapter/importlib path, including a real transitive module import.
        with patch.object(sys, 'dont_write_bytecode', False), patch.object(sys, 'path', list(sys.path)), \
                patch.dict(sys.modules):
            sys.modules.pop('Netem', None)
            adapter = Load('managed_worker')
            self.assertTrue(sys.dont_write_bytecode)
            self.assertEqual(adapter.Worker(release).VALUE, 42)
        self.assertFalse(list(release.rglob('__pycache__')))
        self.assertFalse(list(release.rglob('*.pyc')))
        self.assertEqual(before, {path.relative_to(release): path.read_bytes() for path in release.rglob('*') if path.is_file()})
        self.assertFalse(stage.Stage(archive, releases, 'r1', digest)['changed'])

    def test_traversal_links_and_undeclared_files_are_rejected(self):
        for extra, link in [({'../escaped': b'bad'}, False), ({'unlisted': b'bad'}, False), ({}, True)]:
            with self.subTest(extra=extra, link=link):
                archive, digest = self.Package(extra, link)
                with self.assertRaises(ValueError):
                    stage.Stage(archive, self.root / 'releases', 'bad', digest)
                self.assertFalse((self.root / 'escaped').exists())
                self.assertFalse((self.root / 'releases/bad').exists())

    def test_package_hash_is_checked_before_publication(self):
        archive, _ = self.Package()
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            stage.Stage(archive, self.root / 'releases', 'bad', '0' * 64)
        self.assertFalse((self.root / 'releases').exists())

    def test_bad_deployment_health_restores_the_previous_override_and_preserves_db(self):
        archive, digest = self.Package()
        releases = self.root / 'releases'
        release = Path(stage.Stage(archive, releases, 'r1', digest)['directory'])
        data = self.root / 'persistent-db'
        data.mkdir()
        sentinel = data / 'must-survive'
        sentinel.write_text('existing blocks')
        before_process = {'pid': 10, 'created': 1, 'binary': str(self.root / 'old-boost'),
                          'arguments': ['old-boost', '--data', str(data), '--port', '8089'],
                          'data': str(data.resolve()), 'library_path': '/old/lib'}
        before_head = {'genesis': 'g', 'height': 3}
        systemd = self.root / 'systemd'
        override = systemd / 'chain-node.service.d/90-chain-release.conf'
        override.parent.mkdir(parents=True)
        override.write_text('[Service]\n# previous override\n')
        original = override.read_bytes()
        args = argparse.Namespace(action='activate', service='chain-node.service', state_root=str(self.root / 'states'),
                                  deployment_id='deploy_1', node='n1', release_root=str(releases), release_dir=str(release),
                                  sha256=digest, port=8089)
        with patch.object(activate, 'SYSTEMD_ROOT', systemd), patch.object(activate, 'Process', return_value=before_process), \
                patch.object(activate, 'Head', return_value=before_head), patch.object(activate, 'Run') as run, \
                patch.object(activate, 'Health', side_effect=[RuntimeError('new node failed'), {'restored': True}]):
            result = activate.Deploy(args)
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'rolled_back')
        self.assertEqual(override.read_bytes(), original)
        self.assertEqual(sentinel.read_text(), 'existing blocks')
        self.assertEqual(sum(call.args == ('systemctl', 'restart', 'chain-node.service') for call in run.call_args_list), 2)

    def test_worker_rejects_data_override_and_production_port(self):
        release_root = self.root / 'releases'
        release = release_root / 'r1'
        release.mkdir(parents=True)
        (release / '.chain-release.json').write_text('{}')
        common = {'action': 'start', 'root': str(self.root / 'tests'), 'run': 'case1', 'node': 'n1', 'run_group': 'g1',
                  'release_root': str(release_root), 'release_dir': str(release), 'genesis': {}, 'owner': 'a' * 32}
        for arguments, port in [(['--port', '18089', '--data', str(self.root / 'persistent')], 18089),
                                (['--port', '8089'], 8089)]:
            with patch.object(worker, 'Worker') as existing, self.assertRaises(ValueError):
                worker.One({**common, 'arguments': arguments, 'test_port': port})
            existing.return_value.Handle.assert_not_called()

    def test_new_start_and_audit_restart_wait_before_spawn_and_watchdog(self):
        releases = self.root / 'releases'
        release = releases / 'r1'
        release.mkdir(parents=True)
        (release / '.chain-release.json').write_text('{}')
        root = self.root / 'tests'
        case = root / 'case1/n1'
        request = dict(action='start', root=str(root), run='case1', node='n1', run_group='g1',
                       release_root=str(releases), release_dir=str(release), genesis={}, owner='a' * 32,
                       arguments=['--bind', '192.168.0.10', '--port', '18089'])
        calls = []
        def Wait(address, port):
            calls.append(('wait', address, port))
            return 2.5
        def Start(work):
            calls.append(('spawn', work['restart']))
            result = dict(pid=123, created=4, binary=str(release / 'bin/boost'), command=['boost'])
            case.mkdir(parents=True, exist_ok=True)
            (case / 'process.json').write_text(json.dumps(result))
            return result
        def Watchdog(*args, **kwargs):
            calls.append(('watchdog',))
            return SimpleNamespace(pid=456)
        fake_worker = SimpleNamespace(Handle=Start, FindProcess=lambda record: None)
        fake_psutil = SimpleNamespace(Process=lambda pid: SimpleNamespace(create_time=lambda: 5))
        with patch.object(worker, 'Worker', return_value=fake_worker), \
             patch.object(worker, 'WaitBindablePort', side_effect=Wait), \
             patch.object(worker.subprocess, 'Popen', side_effect=Watchdog), \
             patch.dict(sys.modules, {'psutil': fake_psutil}):
            for restart in (False, True):
                result = worker.One({**request, 'restart': restart})
                self.assertEqual(result['port_bind_probe']['wait_seconds'], 2.5)
        self.assertEqual(calls, [('wait', '192.168.0.10', 18089), ('spawn', False), ('watchdog',),
                                 ('wait', '192.168.0.10', 18089), ('spawn', True), ('watchdog',)])

    def test_port_wait_timeout_does_not_spawn_or_claim_fresh_case(self):
        releases = self.root / 'releases'
        release = releases / 'r1'
        release.mkdir(parents=True)
        (release / '.chain-release.json').write_text('{}')
        root = self.root / 'tests'
        request = dict(action='start', root=str(root), run='case1', node='n1', run_group='g1',
                       release_root=str(releases), release_dir=str(release), genesis={}, owner='a' * 32,
                       arguments=['--port', '18089'])
        with patch.object(worker, 'Worker') as adapter, \
             patch.object(worker, 'WaitBindablePort', side_effect=TimeoutError('occupied')), \
             patch.object(worker.subprocess, 'Popen') as process, self.assertRaises(TimeoutError):
            worker.One(request)
        adapter.return_value.Handle.assert_not_called()
        process.assert_not_called()
        self.assertFalse(root.exists())

    def test_group_cleanup_continues_after_an_ownership_failure_and_ignores_its_outputs(self):
        root = self.root / 'tests'
        directory = root / '.ansible-runs/group1'
        directory.mkdir(parents=True)
        for node in ['bad', 'good']:
            record = {'release_dir': str(self.root / 'releases/r1'), 'owner': 'a' * 32, 'run_group': 'group1', 'process': {'pid': 1},
                      'request': {'root': str(root), 'run': 'case1', 'node': node}}
            (directory / f'case1--{node}.json').write_text(json.dumps(record))
        calls = []
        def One(request):
            calls.append((request['node'], request['action']))
            if request['node'] == 'bad':
                raise RuntimeError('PID executable changed')
            return {'stopped': True, 'removed': True}
        with patch.object(worker, 'One', side_effect=One):
            result = worker.Cleanup({'root': str(root), 'run_group': 'group1', 'owner': 'a' * 32, 'release_root': str(self.root / 'releases')})
            repeated = worker.Cleanup({'root': str(root), 'run_group': 'group1', 'owner': 'a' * 32, 'release_root': str(self.root / 'releases')})
        self.assertFalse(result['ok'])
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(len(repeated['errors']), 1)
        self.assertEqual(calls.count(('bad', 'delete-db')), 0)
        self.assertEqual(calls.count(('good', 'delete-db')), 2)

    def test_existing_case_is_never_adopted_or_authorized_by_a_failed_start(self):
        release_root = self.root / 'releases'
        release = release_root / 'r1'
        release.mkdir(parents=True)
        (release / '.chain-release.json').write_text('{}')
        root = self.root / 'tests'
        database = root / 'case1/n1/db'
        database.mkdir(parents=True)
        (database / 'existing').write_text('old blocks')
        request = {'action': 'start', 'root': str(root), 'run': 'case1', 'node': 'n1', 'run_group': 'new_group',
                   'owner': 'a' * 32, 'release_root': str(release_root), 'release_dir': str(release),
                   'arguments': ['--port', '18089'], 'genesis': {}}
        with patch.object(worker, 'Worker') as existing, self.assertRaises(FileExistsError):
            worker.One(request)
        existing.return_value.Handle.assert_not_called()
        self.assertFalse((root / '.ansible-runs/new_group/case1--n1.json').exists())
        result = worker.Cleanup({'root': str(root), 'run_group': 'new_group', 'owner': 'a' * 32, 'release_root': str(release_root)})
        self.assertTrue(result['ok'])
        self.assertEqual((database / 'existing').read_text(), 'old blocks')

    def test_foreign_ownership_is_rejected_before_process_actions(self):
        root = self.root / 'tests'
        directory = root / '.ansible-runs/group1'
        directory.mkdir(parents=True)
        (directory / 'case1--n1.json').write_text(json.dumps({'owner': 'b' * 32, 'run_group': 'group1'}))
        with patch.object(worker, 'One') as one:
            result = worker.Cleanup({'root': str(root), 'run_group': 'group1', 'owner': 'a' * 32,
                                     'release_root': str(self.root / 'releases')})
        self.assertFalse(result['ok'])
        one.assert_not_called()

    def test_changed_process_record_is_rejected_before_stop(self):
        root, releases = self.root / 'tests', self.root / 'releases'
        release = releases / 'r1'
        release.mkdir(parents=True)
        (release / '.chain-release.json').write_text('{}')
        case = root / 'case1/n1'
        case.mkdir(parents=True)
        original = {'pid': 1, 'created': 2, 'binary': str(release / 'bin/boost'), 'command': ['boost']}
        (case / 'process.json').write_text(json.dumps({**original, 'pid': 999}))
        ledger_file = root / '.ansible-runs/group1/case1--n1.json'
        ledger_file.parent.mkdir(parents=True)
        identity = {'owner': 'a' * 32, 'run_group': 'group1', 'ledger': str(ledger_file)}
        ledger = {**identity, 'release_dir': str(release), 'request': {'root': str(root), 'run': 'case1', 'node': 'n1'},
                  'process': original}
        ledger_file.write_text(json.dumps(ledger))
        reservation = root / '.ansible-case-owners/case1--n1.json'
        reservation.parent.mkdir(parents=True)
        reservation.write_text(json.dumps(identity))
        request = {'action': 'stop', 'root': str(root), 'run': 'case1', 'node': 'n1', 'run_group': 'group1',
                   'owner': 'a' * 32, 'release_root': str(releases), 'release_dir': str(release)}
        with patch.object(worker, 'Worker') as existing, self.assertRaisesRegex(ValueError, 'record changed'):
            worker.One(request)
        existing.return_value.Handle.assert_not_called()

    def test_systemd_quoting_preserves_literals(self):
        quoted = activate.Quote('a b$HOME%name"\\path', command=True)
        self.assertIn('$$HOME', quoted)
        self.assertIn('%%name', quoted)
        self.assertIn('\\"', quoted)
        self.assertIn('\\\\path', quoted)


class BindPortTests(unittest.TestCase):
    def Probe(self, busy_count, address='127.0.0.1', timeout=1, error_number=errno.EADDRINUSE):
        evidence = dict(now=0, created=0, closed=0, sleeps=[], addresses=[])
        class Socket:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                evidence['closed'] += 1
            def bind(self, endpoint):
                evidence['addresses'].append(endpoint)
                if len(evidence['addresses']) <= busy_count:
                    raise OSError(error_number, 'test error')
        def Factory(family, kind):
            evidence['created'] += 1
            evidence['family'] = family
            return Socket()
        def Sleep(seconds):
            evidence['sleeps'].append(seconds)
            evidence['now'] += seconds
        operation = lambda: worker.WaitBindablePort(address, 18089, timeout=timeout,
            socket_factory=Factory, clock=lambda: evidence['now'], sleep=Sleep)
        return evidence, operation

    def test_free_port_returns_immediately_without_reuse_or_sleep(self):
        evidence, operation = self.Probe(0)
        self.assertEqual(operation(), 0)
        self.assertEqual(evidence['sleeps'], [])
        self.assertEqual(evidence['created'], evidence['closed'])
        self.assertEqual(evidence['addresses'], [('127.0.0.1', 18089)])

    def test_busy_port_retries_until_free_and_closes_each_probe(self):
        evidence, operation = self.Probe(2, address='::1')
        self.assertEqual(operation(), .5)
        self.assertEqual(evidence['family'], worker.socket.AF_INET6)
        self.assertEqual(evidence['created'], 3)
        self.assertEqual(evidence['created'], evidence['closed'])

    def test_busy_port_times_out_at_deadline_without_process_actions(self):
        evidence, operation = self.Probe(100)
        with self.assertRaisesRegex(TimeoutError, 'still occupied after 1s; no process was stopped'):
            operation()
        self.assertEqual(evidence['now'], 1)
        self.assertEqual(evidence['created'], evidence['closed'])

    def test_other_bind_errors_fail_immediately(self):
        evidence, operation = self.Probe(1, error_number=errno.EACCES)
        with self.assertRaises(OSError) as raised:
            operation()
        self.assertEqual(raised.exception.errno, errno.EACCES)
        self.assertEqual(evidence['sleeps'], [])


if __name__ == '__main__':
    unittest.main()
