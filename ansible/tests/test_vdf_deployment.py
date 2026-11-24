"""Local regressions for immutable VDF builds and new-service failure recovery."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import vdf_build as build
import vdf_instance as instance


class UnitEncodingTests(unittest.TestCase):
    def test_working_directory_is_a_literal_single_path(self):
        self.assertEqual(instance.WorkingDirectory('/var/lib/chain-vdf/fresh'), '/var/lib/chain-vdf/fresh')
        self.assertEqual(instance.WorkingDirectory('/srv/chain data/%n/"quoted"\\name$HOME'),
                         '/srv/chain data/%%n/"quoted"\\name$HOME')
        self.assertEqual(instance.WorkingDirectory('/srv/trailing space '), '/srv/trailing space /.')
        self.assertEqual(instance.WorkingDirectory('/srv/trailing\\'), '/srv/trailing\\/.')

    def test_working_directory_rejects_control_characters(self):
        for code in (*range(32), 127):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, 'Control characters'):
                instance.WorkingDirectory('/srv/chain' + chr(code) + 'data')

    @unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('systemd-analyze'),
                         'Requires Linux systemd-analyze')
    def test_systemd_analyze_verifies_working_directory_paths(self):
        with tempfile.TemporaryDirectory(prefix='vdf-unit-') as temporary:
            root = Path(temporary)
            for index, name in enumerate(('plain', 'space %n "quote" $HOME', 'trailing space ', 'trailing\\')):
                directory = root / name
                directory.mkdir()
                unit = root / f'working-{index}.service'
                unit.write_text('[Service]\nType=oneshot\n'
                                'WorkingDirectory=' + instance.WorkingDirectory(directory) + '\n'
                                'ExecStart=' + instance.Quote('/bin/true', command=True) + '\n'
                                'Environment=' + instance.Quote('EXAMPLE=' + str(directory)) + '\n'
                                'ReadWritePaths=' + instance.Quote(directory) + '\n', encoding='utf-8')
                result = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)],
                                        capture_output=True, text=True, timeout=30)
                with self.subTest(path=name):
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # Confirm the real parser catches the original regression.
            unit = root / 'old-quoted.service'
            unit.write_text('[Service]\nExecStart=/bin/true\nWorkingDirectory=' + instance.Quote(root) + '\n',
                            encoding='utf-8')
            result = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)],
                                    capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not absolute', result.stderr)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.release = self.root / 'release'
        (self.release / 'source').mkdir(parents=True)
        source = self.release / 'source' / 'main.cpp'
        source.write_bytes(b'// source\n')
        (self.release / 'source-manifest.json').write_text(json.dumps({'files': [
            {'path': 'main.cpp', 'sha256': build.Hash(source), 'bytes': source.stat().st_size, 'mode': '0644'}]}))
        (self.root / 'include' / 'boost').mkdir(parents=True)
        (self.root / 'include' / 'boost' / 'version.hpp').write_text('#define BOOST_VERSION 108600\n')
        self.args = SimpleNamespace(release_dir=str(self.release), run_id='r1', python=str(self.root / 'python'),
                                    cc=str(self.root / 'gcc'), cxx=str(self.root / 'g++'), jobs=1,
                                    require_test=['VdfCorrectness'], rocksdb_dir=None, library_path=None,
                                    boost_dir=str(self.root / 'include'), classroom=False, test_regex='', target=[])
        self.names = ['VdfCorrectness'] + ['Baseline' + str(i) for i in range(14)]
        self.calls = []
        self.fail_ctest = False

    def Run(self, command, log, environment):
        command = [str(item) for item in command]
        self.calls.append(command)
        log.write_text('.debug_info .debug_line\n' if command[0] == 'readelf' else 'ok\n')
        if command[:2] == ['cmake', '--build']:
            directory = self.release / 'build'
            directory.mkdir(exist_ok=True)
            for name in ('boost', 'sender', 'evm_sender'):
                (directory / name).write_bytes(b'ELF fixture')
        if command[0] == 'ctest':
            if self.fail_ctest:
                raise RuntimeError('deliberate test failure')
            output = Path(command[command.index('--output-junit') + 1])
            output.write_text('<testsuite>' + ''.join('<testcase name="' + name + '"/>' for name in self.names) + '</testsuite>')

    def Build(self):
        plan = {'tests': [{'name': name} for name in self.names]}
        with mock.patch.object(build, 'Run', self.Run), mock.patch.object(build.subprocess, 'run',
                return_value=SimpleNamespace(stdout=json.dumps(plan))):
            return build.Build(self.args)

    def test_full_suite_produces_a_deployable_proof_and_is_idempotent(self):
        result = self.Build()
        self.assertTrue(result['complete'])
        self.assertEqual(result['test_count'], 15)
        self.assertTrue((self.release / 'build-proof.json').is_file())
        self.calls.clear()
        self.assertFalse(self.Build()['changed'])
        self.assertEqual(self.calls, [])

    def test_partial_suite_never_produces_deployment_proof(self):
        self.args.test_regex = '^VdfCorrectness$'
        self.args.target = ['vdf_test']
        self.names = ['VdfCorrectness']
        result = self.Build()
        self.assertTrue(result['ok'])
        self.assertFalse(result['complete'])
        self.assertFalse((self.release / 'build-proof.json').exists())

    def test_failed_test_preserves_result_and_refuses_attempt_reuse(self):
        self.fail_ctest = True
        with self.assertRaisesRegex(RuntimeError, 'deliberate'):
            self.Build()
        result = json.loads((self.release / 'build-attempts' / 'r1' / 'result.json').read_text())
        self.assertFalse(result['ok'])
        self.assertFalse((self.release / 'build-proof.json').exists())
        with self.assertRaises(FileExistsError):
            self.Build()

    def test_source_change_after_success_is_rejected(self):
        self.Build()
        (self.release / 'source' / 'main.cpp').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.Build()

    def test_missing_required_test_blocks_proof(self):
        self.args.require_test = ['MissingVdfTest']
        with self.assertRaisesRegex(ValueError, 'not registered'):
            self.Build()
        self.assertFalse((self.release / 'build-proof.json').exists())

    def test_classroom_requires_packaged_frontend(self):
        self.args.classroom = True
        with self.assertRaisesRegex(ValueError, 'frontend'):
            self.Build()


class InstanceTests(unittest.TestCase):
    Run = BuildTests.Run
    Build = BuildTests.Build

    def setUp(self):
        BuildTests.setUp(self)
        self.Build()
        self.systemd = self.root / 'systemd'
        self.systemd.mkdir()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(instance, 'SYSTEMD_ROOT', self.systemd).start()
        self.unit_calls = []
        mock.patch.object(instance, 'Run', self.Systemctl).start()
        mock.patch.object(instance, 'Health', return_value={'head': {'height': 0}}).start()
        mock.patch.dict(sys.modules, {'pwd': SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_uid=1000, pw_gid=1000))}).start()
        mock.patch.object(os, 'chown', create=True).start()
        mock.patch.object(instance.socket, 'socket').start()
        root = self.root / 'instances' / 'fresh'
        self.request = dict(instance_id='fresh', node_id='node1', service='chain-vdf-fresh.service', user='chain-vdf',
                            release_dir=str(self.release), instance_dir=str(root), data_dir=str(root / 'data'),
                            config_path=str(root / 'node.yaml'), genesis_path=str(root / 'genesis.json'),
                            metrics_path=str(root / 'logs' / 'metrics.json'), port=18089, arguments=['--produce', '1'],
                            config_text='node: {}\n', genesis={'users': []})

    def Systemctl(self, *command, **kwargs):
        self.unit_calls.append(command)
        value = 'not-found\n' if '--property=LoadState' in command else '0\n' if '--property=MainPID' in command else 'inactive\n'
        return SimpleNamespace(stdout=value, returncode=0)

    def test_existing_unowned_database_is_untouched(self):
        directory = Path(self.request['data_dir'])
        directory.mkdir(parents=True)
        sentinel = directory / 'CURRENT'
        sentinel.write_bytes(b'old database')
        with self.assertRaises(FileNotFoundError):
            instance.Deploy(self.request)
        self.assertEqual(sentinel.read_bytes(), b'old database')
        self.assertEqual(self.unit_calls, [])

    def test_first_deployment_and_exact_rerun(self):
        self.assertTrue(instance.Deploy(self.request)['changed'])
        config = Path(self.request['config_path'])
        old = config.stat().st_mtime_ns
        self.assertFalse(instance.Deploy(self.request)['changed'])
        self.assertEqual(config.stat().st_mtime_ns, old)

    def test_unit_keeps_each_directive_encoding(self):
        root, paths, argv, _, unit, _ = instance.Validate(self.request)
        lines = unit.decode('utf-8').splitlines()
        self.assertIn('WorkingDirectory=' + instance.WorkingDirectory(root), lines)
        self.assertNotIn('WorkingDirectory=' + instance.Quote(root), lines)
        self.assertIn('ExecStart=' + ' '.join(instance.Quote(value, command=True) for value in argv), lines)
        self.assertIn('Environment=' + instance.Quote('LD_LIBRARY_PATH='), lines)
        self.assertIn('ReadWritePaths=' + instance.Quote(paths['data_dir']) + ' ' +
                      instance.Quote(paths['metrics_path'].parent), lines)

    def test_changed_genesis_is_never_replaced(self):
        instance.Deploy(self.request)
        old = Path(self.request['genesis_path']).read_bytes()
        self.request['genesis'] = {'users': [{'balance': 1}]}
        with self.assertRaisesRegex(ValueError, 'different or incomplete'):
            instance.Deploy(self.request)
        self.assertEqual(Path(self.request['genesis_path']).read_bytes(), old)

    def test_failed_health_stops_only_created_service_and_retains_all_data(self):
        with mock.patch.object(instance, 'Health', side_effect=RuntimeError('unhealthy')):
            with self.assertRaisesRegex(RuntimeError, 'unhealthy'):
                instance.Deploy(self.request)
        self.assertTrue(Path(self.request['genesis_path']).is_file())
        self.assertTrue(Path(self.request['data_dir']).is_dir())
        self.assertFalse((self.systemd / self.request['service']).exists())
        state = json.loads((Path(self.request['instance_dir']) / 'deployment.json').read_text())
        self.assertEqual(state['state'], 'failed')
        self.assertTrue(state['recovery']['stopped'])
        self.assertIn(('systemctl', 'disable', '--now', self.request['service']), self.unit_calls)

    def test_reserved_data_argument_is_rejected_before_writes(self):
        self.request['arguments'] = ['--data', str(self.root / 'old-db')]
        with self.assertRaisesRegex(ValueError, 'replace managed'):
            instance.Deploy(self.request)
        self.assertFalse(Path(self.request['instance_dir']).exists())


if __name__ == '__main__':
    unittest.main()
