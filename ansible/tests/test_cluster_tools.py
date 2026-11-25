"""Fixed-degree graph, tested-package distribution and lifecycle regressions."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cluster_plan as planner
import cluster_release as release
import cluster_control as control
import vdf_build as build
import vdf_instance as instance
import stage_release as stage
import managed_worker as worker


def Nodes(count):
    return [{'id': f'n{index:04}', 'host': f'10.20.{index // 254}.{index % 254 + 1}'}
            for index in range(count)]


class TopologyTests(unittest.TestCase):
    def test_1000_and_1024_nodes_degree_six_eight_twelve(self):
        for count in (1000, 1024):
            for degree in (6, 8, 12):
                with self.subTest(count=count, degree=degree):
                    plan = planner.MakePlan(Nodes(count), 'scale', degree)
                    self.assertEqual(plan['graph']['edges'], count * degree // 2)
                    self.assertEqual(plan['graph']['min_degree'], degree)
                    self.assertEqual(plan['graph']['max_degree'], degree)
                    self.assertTrue(plan['graph']['connected'])
                    nodes = {node['id']: node for node in plan['nodes']}
                    started = set()
                    for node in plan['nodes']:
                        self.assertEqual(len(node['neighbors']), len(set(node['neighbors'])))
                        for peer in node['neighbors']:
                            self.assertIn(node['id'], nodes[peer]['neighbors'])
                        for endpoint in node['seeds']:
                            peer = next(nodes[name] for name in node['neighbors']
                                        if planner.Endpoint(nodes[name]['host'], nodes[name]['port']) == endpoint)
                            edge = tuple(sorted((node['id'], peer['id'])))
                            self.assertNotIn(edge, started)
                            started.add(edge)
                    self.assertEqual(len(started), count * degree // 2)
                    self.assertLessEqual(plan['graph']['max_distance_from_first_node'], 20)

    def test_deterministic_and_small_clusters(self):
        self.assertEqual(planner.MakePlan(Nodes(20), 'scale'), planner.MakePlan(Nodes(20), 'scale'))
        for count in (2, 3, 5, 9):
            plan = planner.MakePlan(Nodes(count), 'small', injectors=1)
            self.assertTrue(plan['graph']['connected'])
            self.assertEqual(plan['graph']['max_degree'], min(8, count - 1))

    def test_ipv6_keeps_management_and_peer_addresses(self):
        rows = [{'id': 'a', 'host': '2001:db8::1', 'ansible_host': '192.0.2.1'},
                {'id': 'b', 'host': '2001:db8::2', 'ansible_host': '192.0.2.2'}]
        plan = planner.MakePlan(rows, 'v6', injectors=1)
        self.assertEqual(plan['nodes'][0]['seeds'], ['[2001:db8::2]:18089'])
        host = planner.Inventory(plan)['all']['children']['chain']['hosts']['a']
        self.assertEqual(host['ansible_host'], '192.0.2.1')
        self.assertEqual(host['chain_p2p_address'], '2001:db8::1')

    def test_duplicate_endpoints_secrets_and_unsafe_names_rejected(self):
        for rows in ([{'id': 'a', 'host': '127.0.0.1'}, {'id': 'b', 'host': '127.0.0.1'}],
                     [{**Nodes(2)[0], 'password': 'must-not-save'}, Nodes(2)[1]],
                     [{'id': '../bad', 'host': '127.0.0.1'}, Nodes(2)[1]]):
            with self.assertRaises(ValueError):
                planner.MakePlan(rows, 'invalid', injectors=1)
        with self.assertRaises(ValueError):
            planner.MakePlan(Nodes(2), '../path', injectors=1)
        with self.assertRaises(ValueError):
            planner.MakePlan(Nodes(2), 'scale', injectors=3)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source_release = self.root / 'build-release'
        self.files = {'Test/ClusterWorker.py': b'# worker', 'Test/Netem.py': b'# netem', 'main.cpp': b'// source'}
        for name, data in self.files.items():
            path = self.source_release / 'source' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.source_release / 'build').mkdir()
        for name in ('boost', 'sender'):
            (self.source_release / 'build' / name).write_bytes(b'\x7fELF' + name.encode())
        manifest = {'source_commit': 'a' * 40, 'files': [
            {'path': name, 'sha256': build.Hash(self.source_release / 'source' / name), 'mode': '0644'}
            for name in self.files]}
        (self.source_release / 'source-manifest.json').write_text(json.dumps(manifest))
        self.proof = {'ok': True, 'complete': True,
                      'source_manifest_sha256': build.Sources(self.source_release),
                      'binary_sha256': build.Hash(self.source_release / 'build/boost'), 'settings': {}}
        (self.source_release / 'build-proof.json').write_text(json.dumps(self.proof))

    def Package(self):
        archive = self.root / 'release.tar.gz'
        result = release.Pack(self.source_release, archive)
        first = stage.Stage(archive, self.root / 'releases', 'tested', result['sha256'])
        self.assertTrue(first['changed'])
        self.assertFalse(stage.Stage(archive, self.root / 'releases', 'tested', result['sha256'])['changed'])
        return self.root / 'releases/tested'

    def Request(self, directory):
        root = self.root / 'instances' / 'node1'
        return dict(instance_id='fleet', node_id='node1', service='chain-vdf-fleet-node1.service', user='chain-vdf',
                    release_format='prebuilt', release_dir=str(directory), instance_dir=str(root),
                    data_dir=str(root / 'data'), config_path=str(root / 'node.yaml'),
                    genesis_path=str(root / 'genesis.json'), metrics_path=str(root / 'logs/metrics.json'),
                    port=18089, arguments=[], config_text='node: {}\n', genesis={'accounts': []})

    def test_tested_package_can_create_service_without_compilers(self):
        staged = self.Package()
        _, _, arguments, _, unit, _ = instance.Validate(self.Request(staged))
        self.assertEqual(arguments[0], str(staged / 'bin/boost'))
        self.assertIn(instance.Quote('LD_LIBRARY_PATH=' + str(staged / 'lib')).encode(), unit)
        self.assertEqual(build.Sources(staged), self.proof['source_manifest_sha256'])

    def test_partial_or_modified_build_cannot_be_packaged(self):
        self.proof['complete'] = False
        (self.source_release / 'build-proof.json').write_text(json.dumps(self.proof))
        with self.assertRaisesRegex(ValueError, 'complete test proof'):
            release.Pack(self.source_release, self.root / 'bad.tar.gz')
        self.proof['complete'] = True
        (self.source_release / 'build-proof.json').write_text(json.dumps(self.proof))
        (self.source_release / 'build/boost').write_bytes(b'\x7fELFchanged')
        with self.assertRaises(ValueError):
            release.Pack(self.source_release, self.root / 'bad.tar.gz')

    def test_distributed_binary_change_fails_validation(self):
        staged = self.Package()
        (staged / 'bin/boost').write_bytes(b'\x7fELFchanged')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            instance.Validate(self.Request(staged))

    def test_classroom_build_packages_gateway_and_evm_sender(self):
        self.proof['settings']['classroom'] = True
        (self.source_release / 'build-proof.json').write_text(json.dumps(self.proof))
        for name in ('evm_sender', 'chain_classroom'):
            (self.source_release / 'build' / name).write_bytes(b'\x7fELF' + name.encode())
        staged = self.Package()
        for name in ('boost', 'sender', 'evm_sender', 'chain_classroom'):
            self.assertEqual((staged / 'bin' / name).read_bytes(), b'\x7fELF' + name.encode())

    def test_classroom_proof_requires_gateway_before_package_creation(self):
        self.proof['settings']['classroom'] = True
        (self.source_release / 'build-proof.json').write_text(json.dumps(self.proof))
        output = self.root / 'incomplete.tar.gz'
        with self.assertRaises(FileNotFoundError):
            release.Pack(self.source_release, output)
        self.assertFalse(output.exists())

    def test_paths_with_traversal_or_root_are_rejected(self):
        for destination in (self.root / '..' / 'escape.tar.gz', Path(self.root.anchor)):
            with self.assertRaises(ValueError):
                release.Pack(self.source_release, destination)


class ControlTests(unittest.TestCase):
    def test_stopping_an_already_stopped_service_is_idempotent(self):
        with mock.patch.object(control, 'Validate', return_value=['/node']), \
             mock.patch.object(control, 'Status', return_value={'active': False}), \
             mock.patch.object(instance, 'Run') as run:
            self.assertFalse(control.Control({'service': 'owned.service'}, 'stop')['changed'])
            run.assert_called_once_with('systemctl', 'stop', 'owned.service')

    def test_stop_cancels_systemd_restart_when_main_pid_is_zero(self):
        with mock.patch.object(control, 'Validate', return_value=['/node']), \
             mock.patch.object(control, 'Status', side_effect=[
                 {'active': False, 'active_state': 'activating', 'sub_state': 'auto-restart'},
                 {'active': False, 'active_state': 'inactive', 'sub_state': 'dead'}]), \
             mock.patch.object(instance, 'Run') as run:
            result = control.Control({'service': 'owned.service'}, 'stop')
            self.assertTrue(result['changed'])
            self.assertFalse(result['active'])
            run.assert_called_once_with('systemctl', 'stop', 'owned.service')

    def test_status_handles_process_exit_between_pid_and_command_lookup(self):
        import psutil
        with mock.patch.object(instance, 'Run', return_value=SimpleNamespace(stdout='MainPID=123\nActiveState=activating\nSubState=auto-restart\n')), \
             mock.patch.object(psutil, 'Process', side_effect=psutil.NoSuchProcess(123)):
            result = control.Status({'service': 'owned.service'}, ['/node'])
            self.assertFalse(result['active'])
            self.assertTrue(result['process_exited'])
            self.assertEqual(result['sub_state'], 'auto-restart')

    def test_ownership_failure_prevents_service_changes(self):
        with mock.patch.object(control, 'Validate', side_effect=ValueError('different owner')), \
             mock.patch.object(instance, 'Run') as run:
            with self.assertRaisesRegex(ValueError, 'different owner'):
                control.Control({'service': 'unowned.service'}, 'stop')
            run.assert_not_called()

    def test_stop_uses_only_validated_service(self):
        with mock.patch.object(control, 'Validate', return_value=['/node']), \
             mock.patch.object(control, 'Status', side_effect=[{'active': True, 'head_error': 'not responsive'}, {'active': False}]), \
             mock.patch.object(instance, 'Run') as run:
            self.assertTrue(control.Control({'service': 'owned.service'}, 'stop')['changed'])
            run.assert_called_once_with('systemctl', 'stop', 'owned.service')

    def test_identity_preserved_once_outside_removed_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)
            (case / 'db').mkdir()
            seed = b'a' * 64 + b'\n'
            (case / 'db/vdf-identity.seed').write_bytes(seed)
            worker.PreserveIdentity(case)
            self.assertEqual((case / 'vdf-identity.seed').read_bytes(), seed)
            worker.PreserveIdentity(case)
            (case / 'db/vdf-identity.seed').write_bytes(b'b' * 64)
            with self.assertRaisesRegex(ValueError, 'differs'):
                worker.PreserveIdentity(case)
            self.assertEqual((case / 'vdf-identity.seed').read_bytes(), seed)


class BenchmarkTests(unittest.TestCase):
    def test_worker_uses_plan_port_instead_of_global_default(self):
        import cluster_benchmark as benchmark
        plan = planner.MakePlan([{**node, 'port': 29189} for node in Nodes(2)], 'custom', injectors=1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = {'output': str(root), 'extra_vars': {'chain_test_port': 18089},
                       'release_id': 'build1', 'run': 'run1', 'owner': 'a' * 32,
                       'ansible_playbook': '/ansible-playbook', 'inventory': '/inventory.json',
                       'ansible_dir': '/ansible', 'forks': 2}
            def Invoke(command, **kwargs):
                variables = json.loads(Path(command[-1][1:]).read_text())
                self.assertEqual(variables['chain_test_port'], 29189)
                for node in plan['nodes']:
                    benchmark.Save(Path(variables['chain_worker_results_dir']) / (node['id'] + '.json'),
                                   {'ok': True, 'result': {}})
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            with mock.patch.object(benchmark.subprocess, 'run', side_effect=Invoke):
                self.assertEqual(set(benchmark.Workers(context, plan).Call('start')), {'n0000', 'n0001'})

    def test_readiness_has_a_global_deadline_and_bounded_inflight_work(self):
        import cluster_benchmark as benchmark
        from concurrent.futures import ThreadPoolExecutor
        cluster = benchmark.SparseCluster.__new__(benchmark.SparseCluster)
        cluster.nodes = [{'id': f'n{index}'} for index in range(1000)]
        cluster.query_workers = 4
        cluster.pool = ThreadPoolExecutor(max_workers=4)
        active = peak = called = 0
        lock = threading.Lock()
        def Probe(node, timeout):
            nonlocal active, peak, called
            with lock:
                active += 1
                peak = max(peak, active)
                called += 1
            try:
                time.sleep(min(.03, timeout))
                raise TimeoutError('fixture unavailable')
            finally:
                with lock:
                    active -= 1
        with tempfile.TemporaryDirectory() as temporary:
            cluster.output = Path(temporary)
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(TimeoutError, '1000 nodes'):
                    cluster.WaitReady(range(1000), timeout=.08, probe=Probe)
            finally:
                cluster.pool.shutdown(wait=True)
            self.assertLess(time.monotonic() - started, .75)
            self.assertLessEqual(peak, 4)
            self.assertLess(called, 1000)
            self.assertEqual(len(json.loads((cluster.output / 'readiness-errors.json').read_text())['nodes']), 1000)

    def test_readiness_retries_failed_nodes_without_requerying_ready_nodes(self):
        import cluster_benchmark as benchmark
        from concurrent.futures import ThreadPoolExecutor
        cluster = benchmark.SparseCluster.__new__(benchmark.SparseCluster)
        cluster.nodes = [{'id': 'good'}, {'id': 'retry'}]
        cluster.query_workers = 2
        cluster.pool = ThreadPoolExecutor(max_workers=2)
        calls = {'good': 0, 'retry': 0}
        def Probe(node, timeout):
            calls[node['id']] += 1
            if node['id'] == 'retry' and calls['retry'] == 1:
                raise ConnectionError('retry once')
            return {'failed': False}
        try:
            cluster.WaitReady(range(2), timeout=2, probe=Probe)
        finally:
            cluster.pool.shutdown(wait=True)
        self.assertEqual(calls, {'good': 1, 'retry': 2})

    def test_ansible_failure_classification_does_not_retain_credentials(self):
        import cluster_benchmark as benchmark
        diagnostics = benchmark.AnsibleErrors('fatal: [n1]: UNREACHABLE! password=secret-value', 'Permission denied')
        self.assertTrue(diagnostics)
        self.assertNotIn('secret-value', json.dumps(diagnostics))

    def test_arguments_keep_graph_and_lifecycle_values(self):
        import cluster_benchmark as benchmark
        plan = planner.MakePlan(Nodes(20), 'test')
        arguments = benchmark.Arguments(plan, {'block-tx-step': 1000})
        for node in plan['nodes']:
            args = arguments[node['id']]
            self.assertEqual(args[args.index('--block-tx-step') + 1], '1000')
            self.assertEqual(args[args.index('--peer-discovery') + 1], '0')
            self.assertEqual(args[args.index('--max-peers') + 1], '12')
            self.assertEqual(args.count('--seed'), len(node['seeds']))
            self.assertNotIn('--data', args)
        self.assertEqual(sum(args.count('--seed') for args in arguments.values()), 80)


class EntryPointTests(unittest.TestCase):
    def test_documented_cli_imports_do_not_write_into_source_without_python_B(self):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        test_source = scripts.parents[1] / 'Test'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / 'ansible/scripts'
            destination.mkdir(parents=True)
            for name in ('cluster_release.py', 'cluster_control.py', 'cluster_benchmark.py',
                         'cluster_plan.py', 'vdf_build.py', 'vdf_instance.py', 'stage_release.py'):
                shutil.copyfile(scripts / name, destination / name)
            (root / 'Test').mkdir()
            for name in ('Cluster.py', 'ClusterWorker.py', 'Netem.py', 'NetworkTest.py', 'Experiments.py'):
                shutil.copyfile(test_source / name, root / 'Test' / name)
            before = {path.relative_to(root).as_posix(): build.Hash(path)
                      for path in root.rglob('*') if path.is_file()}
            environment = dict(os.environ)
            environment.pop('PYTHONDONTWRITEBYTECODE', None)
            environment.pop('PYTHONPYCACHEPREFIX', None)
            for name in ('cluster_release.py', 'cluster_control.py', 'cluster_benchmark.py'):
                result = subprocess.run([sys.executable, str(destination / name), '--help'],
                                        env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            after = {path.relative_to(root).as_posix(): build.Hash(path)
                     for path in root.rglob('*') if path.is_file()}
            self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
