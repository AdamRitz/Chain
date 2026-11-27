"""Fixed-degree graph, tested-package distribution and lifecycle regressions."""
import json
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import struct
import tarfile
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
import cluster_quick as quick


def Nodes(count):
    return [{'id': f'n{index:04}', 'host': f'10.20.{index // 254}.{index % 254 + 1}'}
            for index in range(count)]


def PackageFixture(path):
    payload = json.dumps({'schema': 1, 'source_commit': 'a' * 40, 'files': []}).encode()
    with tarfile.open(path, 'w:gz') as archive:
        member = tarfile.TarInfo('manifest.json')
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))


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
            self.assertTrue(all(not node['backup_neighbors'] and not node['backup_seeds'] for node in plan['nodes']))

    def test_backup_neighbors_are_bounded_reproducible_and_survive_missing_nodes(self):
        for count in (1000, 1024):
            plan = planner.MakePlan(Nodes(count), 'backups', 8, seed=42)
            self.assertEqual(plan, planner.MakePlan(Nodes(count), 'backups', 8, seed=42))
            self.assertNotEqual([node['backup_neighbors'] for node in plan['nodes']],
                                [node['backup_neighbors'] for node in planner.MakePlan(Nodes(count), 'backups', 8, seed=43)['nodes']])
            nodes = {node['id']: node for node in plan['nodes']}
            inventory = planner.Inventory(plan)['all']['children']['chain']['hosts']
            missing = {node['id'] for node in plan['nodes'][:count // 10]}
            replacements = 0
            for node in plan['nodes']:
                backups = node['backup_neighbors']
                self.assertEqual(len(backups), 8)
                self.assertEqual(len(backups), len(set(backups)))
                self.assertNotIn(node['id'], backups)
                self.assertFalse(set(backups) & set(node['neighbors']))
                self.assertEqual(node['backup_seeds'],
                                 [planner.Endpoint(nodes[name]['host'], nodes[name]['port']) for name in backups])
                self.assertEqual(inventory[node['id']]['chain_cluster_backup_peers'], node['backup_seeds'])
                if node['id'] not in missing and set(node['neighbors']) & missing:
                    self.assertTrue(set(backups) - missing)
                    replacements += 1
            self.assertGreater(replacements, 0)
            self.assertEqual(plan['graph']['edges'], count * 8 // 2)

    def test_dense_graph_uses_only_available_backup_neighbors(self):
        plan = planner.MakePlan(Nodes(10), 'dense', degree=8)
        for node in plan['nodes']:
            self.assertEqual(len(node['backup_neighbors']), 1)
            self.assertNotIn(node['backup_neighbors'][0], node['neighbors'])

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

    def test_existing_staged_release_is_bound_to_package_manifest_and_files(self):
        staged = self.Package()
        archive = self.root / 'release.tar.gz'
        expected, manifest = build.Hash(archive), stage.ManifestDigest(archive)
        result = stage.VerifyExisting(staged.parent, 'tested', expected, manifest)
        self.assertFalse(result['changed'])
        self.assertEqual(result['binary_sha256'], self.proof['binary_sha256'])
        with self.assertRaisesRegex(ValueError, 'requested package'):
            stage.VerifyExisting(staged.parent, 'tested', '0' * 64, manifest)
        (staged / 'bin/boost').write_bytes(b'\x7fELFtampered')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            stage.VerifyExisting(staged.parent, 'tested', expected, manifest)
        contents = json.loads((staged / 'manifest.json').read_text())
        for entry in contents['files']:
            if entry['path'] == 'bin/boost':
                entry['sha256'] = build.Hash(staged / 'bin/boost')
        (staged / 'manifest.json').write_text(json.dumps(contents))
        with self.assertRaisesRegex(ValueError, 'manifest differs'):
            stage.VerifyExisting(staged.parent, 'tested', expected, manifest)

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
    def test_low_tps_requires_three_complete_windows_and_growing_backlog(self):
        import cluster_benchmark as benchmark
        guard = benchmark.LowTpsStop(10000, 2)
        def Point(seconds, transactions, backlog):
            return {'seconds': seconds, 'sent': transactions + backlog,
                    'common': {'transactions': transactions}}
        self.assertIsNone(guard(Point(31, 0, 0)))
        self.assertIsNone(guard(Point(32, 0, 0)))
        self.assertIsNone(guard(Point(46.9, 75000, 75000)))
        self.assertIsNone(guard(Point(47, 75000, 75000)))
        self.assertIsNone(guard(Point(62, 150000, 150000)))
        stop = guard(Point(77, 225000, 225000))
        self.assertEqual(len(stop['windows']), 3)
        self.assertEqual(stop['threshold_tps'], 7000)
        self.assertTrue(all(row['tps'] == 5000 and row['backlog_growth'] > 0 for row in stop['windows']))
        guard = benchmark.LowTpsStop(10000, 0, start=0)
        for seconds in range(0, 100, 15):
            self.assertIsNone(guard(Point(seconds, seconds * 1000, 100000 - seconds * 100)))

    def test_fast_window_resets_the_consecutive_slow_count(self):
        import cluster_benchmark as benchmark
        guard = benchmark.LowTpsStop(10000, 0, start=0)
        def Point(seconds, transactions):
            return {'seconds': seconds, 'sent': seconds * 10000, 'common': {'transactions': transactions}}
        for seconds, transactions in ((0, 0), (15, 15000), (30, 165000), (45, 180000), (60, 195000)):
            self.assertIsNone(guard(Point(seconds, transactions)))
        self.assertEqual(len(guard(Point(75, 210000))['windows']), 3)

    def test_load_case_stops_sender_keeps_partial_metrics_and_default_has_no_callback(self):
        import Experiments as experiments
        class FakeLoad:
            current = None
            def __init__(self, *args, **kwargs):
                FakeLoad.current = self
                self.started = 0
                self.sent, self.wire_transactions = 4, 4
                self.samples, self.late_ms = [], []
                self.parallel_ingress, self.error = False, None
                self.stop = threading.Event()
            def Run(self):
                self.started = time.monotonic()
                self.stop.wait(.5)
        class FakeCluster:
            nodes = [{'id': 'n1'}]
            def Stats(self):
                return [{'failed': False, 'head': 'a', 'pool': 0, 'pending': 0, 'reorganizations': 0,
                         'invalid': 0, 'rejected': 0, 'max_reorg_depth': 0}]
            def Common(self, *args):
                return {'transactions': 4, 'height': 2}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = {'warmup': 0, 'duration': .04, 'drain_seconds': .02, 'ingress': 'shard',
                    'sample_hz': 1, 'batch': 1, 'poll_seconds': .005, 'wallets': 1,
                    'deadline_ms': 1000, 'max_backlog_growth': .1}
            for callback in (None, lambda point: {'reason': 'fixture requested stop'}):
                output = root / ('early' if callback else 'default')
                output.mkdir()
                plan = {**base, **({'stop_when': callback} if callback else {})}
                with mock.patch.object(experiments, 'Load', FakeLoad), \
                     mock.patch.object(experiments, 'CheckSamples'), \
                     mock.patch.object(experiments, 'VerifyNativeState', return_value={'checked': 4}):
                    result = experiments.LoadCase(FakeCluster(), {'rate': 100, 'suite': 'throughput'},
                                                  plan, root / 'unused', {}, output)
                self.assertTrue(FakeLoad.current.stop.is_set())
                self.assertTrue(result['drained'])
                self.assertEqual(result['sent_unique'], 4)
                self.assertEqual('early_stop' in result, callback is not None)
                self.assertEqual((output / 'early-stop.json').exists(), callback is not None)
                if callback:
                    self.assertEqual(result['requested_duration_seconds'], .04)
                    self.assertFalse(result['sustainable'])
                    self.assertFalse(result['tx_sustainable'])

    def test_benchmark_cleans_nodes_after_early_stop_and_load_failure(self):
        import cluster_benchmark as benchmark
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for failure in (False, True):
                context = {'output': str(root / str(failure)), 'rate': 10000, 'warmup': 2,
                    'duration': 300, 'drain_seconds': 5, 'poll_seconds': .1, 'sample_hz': 20,
                    'batch': 128, 'wallets': 1024, 'dataset': '/unused', 'metrics_period': 1}
                cluster = mock.Mock(query_sweeps=[], cleanup_errors=[])
                plan = {'nodes': [{'id': 'a'}], 'injectors': ['a'], 'graph': {}}
                previous_load = benchmark.Experiments.Load
                result = {'drained': True, 'unresolved_samples': 0, 'early_stop': {'reason': 'slow'}}
                with mock.patch.object(benchmark, 'Validate', return_value=(plan, {})), \
                     mock.patch.object(benchmark, 'SparseCluster', return_value=cluster), \
                     mock.patch.object(benchmark.Experiments, 'LoadCase',
                         side_effect=RuntimeError('fixture load failure') if failure else None, return_value=result) as load:
                    outcome = benchmark.Run(context)
                cluster.Close.assert_called_once()
                self.assertIs(benchmark.Experiments.Load, previous_load)
                self.assertFalse(outcome['ok'])
                self.assertTrue((Path(context['output']) / 'result.json').is_file())
                if not failure:
                    self.assertEqual(outcome['status'], 'stopped_early')
                    self.assertIsInstance(load.call_args.args[2]['stop_when'], benchmark.LowTpsStop)

    def test_ready_requires_committed_producers_and_allows_epoch_boundary(self):
        import cluster_benchmark as benchmark
        keys = [str(index) * 64 for index in range(5)]
        genesis = {'vdf': {'enabled': True, 'producer_rotation': True, 'epoch_blocks': 32}}
        rows = [{'vdf': {'enabled': True, 'producer_rotation': True, 'epoch': 2,
                         'public_key': key, 'producers': keys}} for key in keys]
        self.assertTrue(benchmark.ConsensusReady(rows, genesis))
        rows[-1]['vdf']['epoch'] = 3
        rows[-1]['vdf']['producers'] = list(reversed(keys))
        self.assertTrue(benchmark.ConsensusReady(rows, genesis))
        rows[0]['vdf']['epoch'] = 1
        self.assertFalse(benchmark.ConsensusReady(rows, genesis))
        rows[0]['vdf']['epoch'] = 2
        rows[1]['vdf']['producers'] = list(reversed(keys))
        self.assertFalse(benchmark.ConsensusReady(rows, genesis))
        rows[1]['vdf']['producers'] = keys
        rows[0]['vdf']['producer_rotation'] = False
        self.assertFalse(benchmark.ConsensusReady(rows, genesis))

    def test_ready_rejects_missing_or_foreign_producer(self):
        import cluster_benchmark as benchmark
        rows = [{'vdf': {'enabled': True, 'epoch': 2, 'public_key': str(index),
                         'producers': ['0', '1']}} for index in range(2)]
        self.assertTrue(benchmark.ConsensusReady(rows, {'vdf': {'enabled': True}}))
        rows[0]['vdf']['producers'] = ['0', 'missing']
        self.assertFalse(benchmark.ConsensusReady(rows, {'vdf': {'enabled': True}}))

    def test_ready_checks_continuous_producer_configuration(self):
        import cluster_benchmark as benchmark
        keys = [str(index) * 64 for index in range(5)]
        genesis = {'vdf': {'enabled': True, 'producer_rotation': True, 'epoch_blocks': 160,
                           'consecutive_blocks': 32, 'producer_timeout_slots': 8}}
        rows = [{'vdf': {'enabled': True, 'producer_rotation': True, 'epoch': 2,
                         'public_key': key, 'producers': keys, 'consecutive_blocks': 32,
                         'producer_timeout_slots': 8}} for key in keys]
        self.assertTrue(benchmark.ConsensusReady(rows, genesis))
        rows[0]['vdf']['consecutive_blocks'] = 16
        self.assertFalse(benchmark.ConsensusReady(rows, genesis))
        rows[0]['vdf']['consecutive_blocks'] = 32
        rows[0]['vdf']['producer_timeout_slots'] = 4
        self.assertFalse(benchmark.ConsensusReady(rows, genesis))

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
        arguments = benchmark.Arguments(plan, {'max-block-txs': 4500})
        for node in plan['nodes']:
            args = arguments[node['id']]
            self.assertEqual(args[args.index('--max-block-txs') + 1], '4500')
            self.assertNotIn('--block-tx-step', args)
            self.assertEqual(args[args.index('--peer-discovery') + 1], '0')
            self.assertEqual(args[args.index('--max-peers') + 1], '12')
            self.assertEqual(args[args.index('--target-peers') + 1], '8')
            self.assertEqual(args[args.index('--clock-sync') + 1], '0')
            self.assertEqual(args.count('--seed'), len(node['seeds']))
            self.assertEqual(args.count('--backup-seed'), len(node['backup_seeds']))
            self.assertEqual([args[index+1] for index, value in enumerate(args) if value == '--backup-seed'], node['backup_seeds'])
            self.assertNotIn('--data', args)
        self.assertEqual(sum(args.count('--seed') for args in arguments.values()), 80)

    def test_initial_producer_limit_does_not_disable_vdf_computation(self):
        import cluster_benchmark as benchmark
        plan = planner.MakePlan(Nodes(20), 'bootstrap')
        arguments = benchmark.Arguments(plan, {}, {'n0000'})
        for name, args in arguments.items():
            self.assertEqual(args[args.index('--produce') + 1], '1')
            self.assertEqual(args[args.index('--vdf-bootstrap-produce') + 1], '1' if name == 'n0000' else '0')

    def test_node_settings_cannot_replace_backup_topology(self):
        import cluster_benchmark as benchmark
        plan = planner.MakePlan(Nodes(2), 'locked', injectors=1)
        for settings in ({'target-peers': 1000}, {'backup-seed': '192.0.2.1:18089'},
                         {'seed': '192.0.2.1:18089'}, {1: 5}):
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, 'topology'):
                benchmark.Arguments(plan, settings)


class EntryPointTests(unittest.TestCase):
    def test_documented_cli_imports_do_not_write_into_source_without_python_B(self):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        test_source = scripts.parents[1] / 'Test'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / 'ansible/scripts'
            destination.mkdir(parents=True)
            for name in ('cluster_release.py', 'cluster_control.py', 'cluster_benchmark.py',
                         'cluster_plan.py', 'cluster_quick.py', 'vdf_build.py', 'vdf_instance.py', 'stage_release.py'):
                shutil.copyfile(scripts / name, destination / name)
            (root / 'Test').mkdir()
            for name in ('Cluster.py', 'ClusterWorker.py', 'Netem.py', 'NetworkTest.py', 'Experiments.py'):
                shutil.copyfile(test_source / name, root / 'Test' / name)
            before = {path.relative_to(root).as_posix(): build.Hash(path)
                      for path in root.rglob('*') if path.is_file()}
            environment = dict(os.environ)
            environment.pop('PYTHONDONTWRITEBYTECODE', None)
            environment.pop('PYTHONPYCACHEPREFIX', None)
            for name in ('cluster_release.py', 'cluster_control.py', 'cluster_benchmark.py', 'cluster_quick.py'):
                result = subprocess.run([sys.executable, str(destination / name), '--help'],
                                        env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            after = {path.relative_to(root).as_posix(): build.Hash(path)
                     for path in root.rglob('*') if path.is_file()}
            self.assertEqual(after, before)


class QuickTests(unittest.TestCase):
    def SignedDataset(self, root, count=8, wallets=2):
        from nacl.signing import SigningKey
        keys = [SigningKey(bytes([index + 1]) * 32) for index in range(wallets)]
        data = root / 'transactions.bin'
        with data.open('wb') as output:
            for index in range(count):
                key = keys[index % wallets]
                unsigned = bytes(key.verify_key) + bytes(keys[(index + 1) % wallets].verify_key) + struct.pack('<QQ', 1, index // wallets + 1)
                digest = hashlib.blake2b(unsigned, digest_size=32).digest()
                output.write(unsigned + digest + key.sign(digest).signature)
        genesis = Path(str(data) + '.genesis.json')
        quick.Save(genesis, {'base_fee': 1, 'accounts': [
            {'public_key': bytes(key.verify_key).hex(), 'balance': 1000} for key in keys]})
        return data, genesis

    def test_explicit_dataset_is_portable_and_larger_than_required_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, genesis = self.SignedDataset(root)
            prepared = quick.PreparedDataset(data, genesis, 6, 2)
            self.assertEqual(prepared['count'], 8)
            self.assertEqual(prepared['required_count'], 6)
            self.assertEqual(prepared['verified_signatures'], 8)
            manifest = root / 'portable.json'
            quick.Save(manifest, prepared)
            other = root / 'other-os'
            other.mkdir()
            copied_data, copied_genesis = other / 'signed.bin', other / 'accounts.json'
            shutil.copyfile(data, copied_data)
            shutil.copyfile(genesis, copied_genesis)
            imported = quick.PreparedDataset(copied_data, copied_genesis, 7, 2, manifest)
            self.assertEqual(imported['dataset_sha256'], prepared['dataset_sha256'])
            self.assertEqual(imported['required_count'], 7)
            copied_data.write_bytes(b'x' + copied_data.read_bytes()[1:])
            with self.assertRaisesRegex(ValueError, 'manifest'):
                quick.PreparedDataset(copied_data, copied_genesis, 7, 2, manifest)

    def test_explicit_dataset_rejects_invalid_signature_nonce_accounts_and_size(self):
        from nacl.exceptions import BadSignatureError
        from nacl.signing import SigningKey
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, genesis = self.SignedDataset(root)
            original = data.read_bytes()
            tampered = bytearray(original)
            tampered[175] ^= 1
            data.write_bytes(tampered)
            with self.assertRaises(BadSignatureError):
                quick.PreparedDataset(data, genesis, 6, 2)
            tampered = bytearray(original)
            struct.pack_into('<Q', tampered, 72, 2)
            digest = hashlib.blake2b(tampered[:80], digest_size=32).digest()
            tampered[80:112] = digest
            tampered[112:176] = SigningKey(bytes([1]) * 32).sign(digest).signature
            data.write_bytes(tampered)
            with self.assertRaisesRegex(ValueError, 'nonce'):
                quick.PreparedDataset(data, genesis, 6, 2)
            data.write_bytes(original[:-1])
            with self.assertRaisesRegex(ValueError, 'complete'):
                quick.PreparedDataset(data, genesis, 6, 2)
            data.write_bytes(original)
            with self.assertRaisesRegex(ValueError, 'at least'):
                quick.PreparedDataset(data, genesis, 9, 2)
            with self.assertRaisesRegex(ValueError, 'wallets'):
                quick.PreparedDataset(data, genesis, 6, 3)

    def test_prepare_needs_no_linux_package_or_ansible_and_reuses_imported_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, genesis = self.SignedDataset(root)
            nodes = root / 'nodes.json'
            quick.Save(nodes, Nodes(2))
            output = root / 'offline'
            args = quick.Parser().parse_args(['prepare', '--nodes', str(nodes), '--dataset', str(data),
                '--wallets', '2', '--rate', '1', '--warmup', '0', '--duration', '6', '--output', str(output)])
            invoke = mock.Mock(side_effect=AssertionError('Offline prepare called Ansible/SSH'))
            self.assertEqual(quick.Execute(args, invoke=invoke), 0)
            invoke.assert_not_called()
            self.assertTrue((output / 'plan.json').is_file())
            self.assertEqual(json.loads((output / 'dataset.json').read_text())['count'], 8)
            self.assertNotIn('chain_package', json.loads((output / 'variables.json').read_text()))
            self.assertTrue(json.loads((output / 'operation.json').read_text())['offline'])

    def test_test_reuses_portable_dataset_without_signing_or_restaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, genesis = self.SignedDataset(root)
            manifest = root / 'portable.json'
            quick.Save(manifest, quick.PreparedDataset(data, genesis, 6, 2))
            package, nodes, sender = root / 'package.tar.gz', root / 'nodes.json', root / 'sender'
            PackageFixture(package)
            sender.write_bytes(b'sender fixture')
            quick.Save(nodes, Nodes(2))
            output = root / 'staged-test'
            args = quick.Parser().parse_args(['test', '--nodes', str(nodes), '--release-id', 'r',
                '--package', str(package), '--package-sha256', build.Hash(package), '--sender', str(sender),
                '--dataset', str(data), '--dataset-manifest', str(manifest), '--staged',
                '--wallets', '2', '--rate', '1', '--warmup', '0', '--duration', '6', '--output', str(output)])
            invoke = mock.Mock(return_value=SimpleNamespace(returncode=0))
            with mock.patch.object(quick, 'VerifySender'), \
                 mock.patch.object(quick, 'PrepareDataset', side_effect=AssertionError('Imported data was resigned')):
                self.assertEqual(quick.Execute(args, invoke=invoke), 0)
            variables = json.loads((output / 'variables.json').read_text())
            self.assertTrue(variables['chain_cluster_staged'])
            self.assertEqual(variables['chain_dataset'], str(data.resolve()))
            self.assertEqual(json.loads((output / 'dataset.json').read_text())['required_count'], 6)
            self.assertEqual(invoke.call_count, 1)

    def test_persistent_deployment_bootstrap_arguments_preserve_legacy_genesis(self):
        import ast
        import jinja2
        import yaml
        playbook = Path(__file__).resolve().parents[1] / 'cluster_deploy.yml'
        tasks = yaml.safe_load(playbook.read_text(encoding='utf-8'))[-1]['tasks']
        task = next(task for task in tasks if task['name'].startswith('Limit initial'))
        expression = task['ansible.builtin.set_fact']['chain_cluster_node_arguments']
        environment = jinja2.Environment(undefined=jinja2.ChainableUndefined)
        # Ansible provides bool; a minimal equivalent is enough for these booleans.
        environment.filters['bool'] = bool
        template = environment.from_string(expression)
        base = {'chain_cluster_arguments': ['--produce', '1'], 'groups': {'chain': ['first', 'second']},
                'inventory_hostname': 'second'}
        for genesis, expected in (({'accounts': []}, ['--produce', '1']),
                                  ({'vdf': {'producer_rotation': True}}, ['--produce', '1', '--vdf-bootstrap-produce', '0'])):
            rendered = template.render(**base, chain_cluster_genesis=genesis)
            self.assertEqual(ast.literal_eval(rendered), expected)

    def test_five_minute_count_does_not_keep_old_dataset_limit(self):
        self.assertEqual(quick.RequiredTransactions(20000, 2, 300), 6040000)
        self.assertEqual(quick.RequiredTransactions(3, .1, .1), 1)
        self.assertEqual(quick.Parser().parse_args([
            'test', '--nodes', '/nodes.json', '--release-id', 'r', '--package', '/r.tar.gz',
            '--package-sha256', 'a' * 64, '--output', '/results']).duration, 300)
        for rate, warmup, duration in ((0, 2, 300), (10, 2, float('nan')), (10000000, 2, 300)):
            with self.assertRaises(ValueError):
                quick.RequiredTransactions(rate, warmup, duration)

    def test_new_test_genesis_enables_rotation_without_modifying_source(self):
        original = {'base_fee': 1, 'accounts': [{'public_key': 'a', 'balance': 1000}]}
        result = quick.RotationGenesis(original, now_ms=1000999)
        self.assertNotIn('vdf', original)
        self.assertTrue(result['vdf']['producer_rotation'])
        self.assertEqual(result['vdf']['epoch_blocks'], 160)
        self.assertEqual(result['vdf']['consecutive_blocks'], 32)
        self.assertEqual(result['vdf']['producer_timeout_slots'], 8)
        self.assertEqual(result['vdf']['slot_ms'], 200)
        self.assertEqual(result['vdf']['start_time_ms'], 1000800)
        with self.assertRaisesRegex(ValueError, 'existing VDF genesis'):
            quick.RotationGenesis(result)

    def test_consecutive_producer_settings_are_bound_to_new_genesis(self):
        original = {'base_fee': 1, 'accounts': []}
        result = quick.RotationGenesis(original, now_ms=1000999, consecutive_blocks=16,
                                       producer_timeout_slots=4, epoch_blocks=80)
        self.assertEqual(result['vdf']['epoch_blocks'], 80)
        self.assertEqual(result['vdf']['consecutive_blocks'], 16)
        self.assertEqual(result['vdf']['producer_timeout_slots'], 4)
        self.assertNotIn('vdf', original)
        for settings in ({'consecutive_blocks': 0}, {'consecutive_blocks': 820},
                         {'consecutive_blocks': True}, {'producer_timeout_slots': 0}, {'producer_timeout_slots': 2},
                         {'producer_timeout_slots': 65537}, {'producer_timeout_slots': True},
                         {'epoch_blocks': 32}, {'epoch_blocks': 320}, {'epoch_blocks': True}):
            with self.assertRaises(ValueError):
                quick.RotationGenesis(original, **settings)

    def test_dataset_cache_is_reused_and_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sender = root / 'sender'
            sender.write_bytes(b'fixture sender')
            calls = []
            def Prepare(command, **kwargs):
                calls.append(command)
                data = Path(command[command.index('--prepare') + 1])
                data.write_bytes(b't' * (3 * 176))
                Path(str(data) + '.genesis.json').write_text(json.dumps({'accounts': [{}, {}]}))
            first = quick.PrepareDataset(sender, root / 'cache', 3, 2, invoke=Prepare)
            second = quick.PrepareDataset(sender, root / 'cache', 3, 2, invoke=Prepare)
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            Path(first['dataset']).write_bytes(b'x' * (3 * 176))
            with self.assertRaisesRegex(ValueError, 'checksum'):
                quick.PrepareDataset(sender, root / 'cache', 3, 2, invoke=Prepare)

    def test_failed_dataset_prepare_removes_only_its_temporary_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sender = root / 'sender'
            sender.write_bytes(b'fixture sender')
            cache = root / 'cache'
            cache.mkdir()
            retained = cache / 'retained.bin'
            retained.write_bytes(b'keep')
            with self.assertRaises(RuntimeError):
                quick.PrepareDataset(sender, cache, 3, 2, invoke=mock.Mock(side_effect=RuntimeError('fixture')))
            self.assertEqual(list(cache.iterdir()), [retained])

    def test_deploy_preserves_existing_genesis_and_runs_only_deploy_playbook(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, nodes, genesis = root / 'package.tar.gz', root / 'nodes.json', root / 'genesis.json'
            PackageFixture(package)
            quick.Save(nodes, Nodes(2))
            original = {'base_fee': 1, 'accounts': [], 'vdf': {'producer_rotation': False}}
            quick.Save(genesis, original)
            args = quick.Parser().parse_args(['deploy', '--nodes', str(nodes), '--release-id', 'r',
                '--package', str(package), '--package-sha256', build.Hash(package),
                '--genesis', str(genesis), '--output', str(root / 'deploy')])
            calls = []
            self.assertEqual(quick.Execute(args, invoke=lambda command, **kwargs:
                                          calls.append(command) or SimpleNamespace(returncode=0)), 0)
            self.assertEqual(len(calls), 1)
            self.assertTrue(any(item.endswith('cluster_deploy.yml') for item in calls[0]))
            variables = json.loads((root / 'deploy/variables.json').read_text())
            self.assertEqual(variables['chain_cluster_genesis'], original)
            self.assertEqual(json.loads(genesis.read_text()), original)

    def test_test_action_prepares_five_minute_input_and_keeps_data_on_controller(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, nodes, sender = root / 'package.tar.gz', root / 'nodes.json', root / 'sender'
            PackageFixture(package)
            sender.write_bytes(b'fixture sender')
            quick.Save(nodes, [{**node, 'port': 18089} for node in Nodes(2)])
            genesis = root / 'funded.json'
            quick.Save(genesis, {'base_fee': 1, 'accounts': [{}, {}]})
            dataset = {'dataset': str(root / 'cached-transactions.bin'), 'genesis': str(genesis),
                       'dataset_sha256': 'a' * 64}
            args = quick.Parser().parse_args(['test', '--nodes', str(nodes), '--port', '28089',
                '--release-id', 'r', '--package', str(package), '--package-sha256', build.Hash(package),
                '--sender', str(sender), '--wallets', '2', '--output', str(root / 'test01')])
            calls = []
            with mock.patch.object(quick, 'VerifySender') as verify, \
                 mock.patch.object(quick, 'PrepareDataset', return_value=dataset) as prepare:
                self.assertEqual(quick.Execute(args, invoke=lambda command, **kwargs:
                                              calls.append(command) or SimpleNamespace(returncode=0)), 0)
            verify.assert_called_once_with(package.resolve(), sender.resolve())
            self.assertEqual(prepare.call_args.args[2:], (3020000, 2))
            variables = json.loads((root / 'test01/variables.json').read_text())
            self.assertEqual(variables['chain_cluster_duration'], 300)
            self.assertEqual(variables['chain_dataset'], dataset['dataset'])
            self.assertEqual(variables['chain_package_manifest_sha256'], stage.ManifestDigest(package))
            self.assertTrue(variables['chain_cluster_early_stop_enabled'])
            self.assertEqual(variables['chain_cluster_early_stop_ratio'], .7)
            self.assertEqual(variables['chain_cluster_early_stop_windows'], 3)
            self.assertTrue(any(item.endswith('cluster_benchmark.yml') for item in calls[0]))
            self.assertEqual({node['port'] for node in json.loads((root / 'test01/plan.json').read_text())['nodes']}, {28089})
            with self.assertRaises(FileExistsError):
                quick.Execute(args, invoke=mock.Mock())


if __name__ == '__main__':
    unittest.main()
