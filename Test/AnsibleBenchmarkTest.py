"""Controller-only contract and incomplete-evidence tests; no remote processes."""
import concurrent.futures
import json
import inspect
from pathlib import Path
import tempfile
import threading
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import AnsibleBenchmark as B


def Nodes():
    return [dict(node_id='n%02d' % index, p2p_address='10.0.0.%s' % (index + 1),
                 ansible_host='host%s' % index, port=18089) for index in range(20)]


class WorkerContractTests(unittest.TestCase):
    def test_full_fleet_action_uses_one_playbook_and_separate_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = dict(nodes=Nodes(), run_id='comparison', test_root='/var/tmp/isolated-tests',
                           run_owner='a' * 32,
                           inventory_file=str(root / 'inventory.yml'), ansible_dir=str(root),
                           inventory_files=[str(root / 'inventory.yml'), str(root / 'overlay.yml')],
                           controller_python=str(root / '.venv' / 'bin' / 'python'),
                           ansible_extra_vars=dict(chain_python='/opt/workers/python', chain_worker_action='forged'))
            calls = []
            def Runner(command, **options):
                calls.append(command)
                request = json.loads(Path(command[-1][1:]).read_text())
                self.assertEqual(request['chain_run_owner'], 'a' * 32)
                self.assertEqual(request['chain_python'], '/opt/workers/python')
                self.assertEqual(request['chain_worker_action'], 'stop')
                output = Path(request['chain_worker_results_dir'])
                for node in context['nodes']:
                    B.Save(output / (node['node_id'] + '-' + request['chain_worker_action'] + '.json'),
                           dict(ok=True, result=dict(stopped=True)))
                return SimpleNamespace(returncode=0)
            workers = B.AnsibleWorkers(context, root / 'evidence', runner=Runner)
            values = workers.Call('stop', 'comparison-case1', dict(id='old'))
            self.assertEqual(len(calls), 1)
            self.assertEqual(set(values), {node['node_id'] for node in context['nodes']})
            self.assertEqual(calls[0][0], str(root / '.venv' / 'bin' / 'ansible-playbook'))
            inventory_indices = [index for index, value in enumerate(calls[0]) if value == '-i']
            self.assertEqual([calls[0][index + 1] for index in inventory_indices], context['inventory_files'])
            self.assertEqual(calls[0][calls[0].index('--forks') + 1], '20')
            self.assertFalse(any('ssh' == argument for argument in calls[0]))

    def test_missing_host_or_failed_playbook_never_returns_success(self):
        for returncode, count in ((0, 19), (2, 20)):
            with self.subTest(returncode=returncode, count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                context = dict(nodes=Nodes(), run_id='comparison', test_root='/var/tmp/isolated-tests',
                               run_owner='a' * 32,
                               inventory_file=str(root / 'inventory.yml'), ansible_dir=str(root))
                def Runner(command, **options):
                    request = json.loads(Path(command[-1][1:]).read_text())
                    for node in context['nodes'][:count]:
                        B.Save(Path(request['chain_worker_results_dir']) / (node['node_id'] + '.json'),
                               dict(ok=True, result=dict(removed=True)))
                    return SimpleNamespace(returncode=returncode)
                workers = B.AnsibleWorkers(context, root / 'evidence', runner=Runner)
                with self.assertRaises(RuntimeError):
                    workers.Call('delete-db', 'comparison-case1', dict(id='old'))

    def test_explicit_playbook_wins_and_default_uses_current_python_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            explicit = str(root / 'tools' / 'ansible-playbook')
            self.assertEqual(B.PlaybookPath(dict(ansible_playbook=explicit,
                                                controller_python=str(root / 'venv' / 'python'))), explicit)
        self.assertEqual(B.PlaybookPath({}), str(Path(sys.executable).parent / 'ansible-playbook'))
        with self.assertRaises(ValueError):
            B.PlaybookPath(dict(ansible_playbook='ansible-playbook'))


class StubCluster:
    def __init__(self, nodes, run, genesis, settings, output):
        self.nodes, self.run, self.genesis, self.output = nodes, run, genesis, output
        self.settings, self.started = settings, set()
        self.starts = [None for node in nodes]
        self.clients, self.offline, self.cleanup_errors = [], set(), []
        self.pool = concurrent.futures.ThreadPoolExecutor(20)

    def WaitReady(self, indices):
        pass

    def Stats(self):
        return [dict(failed=False, peers=19) for node in self.nodes]


class StepComparisonTests(unittest.TestCase):
    def test_default_preserves_two_release_order_and_omits_step(self):
        releases = [dict(id='old'), dict(id='new')]
        schedule = B.ComparisonSchedule(dict(releases=releases))
        self.assertEqual([(release['id'], kind, rate, step) for release, kind, rate, step in schedule],
                         [('old', 'throughput', 20000, None), ('new', 'throughput', 20000, None),
                          ('old', 'throughput', 30000, None), ('new', 'throughput', 30000, None),
                          ('old', 'latency', 20000, None), ('new', 'latency', 20000, None)])

    def test_step_schedule_finishes_pure_throughput_before_latency(self):
        steps = [0, 500, 1000, 2000]
        release = dict(id='new')
        schedule = B.ComparisonSchedule(dict(releases=[release], block_tx_steps=steps))
        self.assertEqual(schedule, [(release, 'throughput', rate, step) for rate in B.RATES for step in steps] +
                         [(release, 'latency', 20000, step) for step in steps])
        self.assertEqual(len(schedule), 12)
        self.assertEqual(B.ComparisonSchedule(dict(releases=[release], block_tx_steps=[1, 10000]))[-1][-1], 10000)

    def test_invalid_steps_and_release_counts_are_rejected(self):
        for steps in (None, [], '500', [True], [500.0], [-1], [10001], [500, 500]):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                B.ComparisonSchedule(dict(releases=[dict(id='new')], block_tx_steps=steps))
        for releases, steps in (([], [500]), ([{}, {}], [500]), ([{}], None), ([], None)):
            context = dict(releases=releases)
            if steps is not None:
                context['block_tx_steps'] = steps
            with self.subTest(context=context), self.assertRaises(ValueError):
                B.ComparisonSchedule(context)

    def test_actual_case_arguments_and_evidence_preserve_step_zero_and_legacy(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(B, 'Discover'), \
                mock.patch.object(B, 'InstallMeasurement', return_value=([], [])), \
                mock.patch.dict(sys.modules, {'psutil': SimpleNamespace()}):
            import Experiments as E
            root = Path(directory)
            context = dict(nodes=Nodes(), run_id='comparison', case_prefix='comparison-owned',
                           test_root='/var/tmp/isolated-tests', result_dir=str(root),
                           dataset=str(root / 'unused.bin'), dataset_sha256='d' * 64, genesis_sha256='e' * 64)
            release = dict(id='new', directory='/opt/releases/new', sha256='a' * 64)
            calls = []
            class Workers:
                def Call(self, action, run, selected, genesis=None, arguments=None, restart=False):
                    calls.append(dict(action=action, run=run, arguments=arguments))
                    return {node['node_id']: dict(binary_sha256=release['sha256'], stopped=True, removed=True)
                            for node in context['nodes']}
            experiments = SimpleNamespace(ValidatePlan=E.ValidatePlan,
                LoadCase=mock.Mock(side_effect=RuntimeError('local fixture after start')))
            runs = set()
            for step in (None, 0, 500, 1000, 2000):
                row = B.RunCase(context, {}, Workers(), release, 'throughput', 20000, 1,
                                (SimpleNamespace(Cluster=StubCluster), experiments, SimpleNamespace()), step)
                self.assertIn('local fixture after start', row['error'])
                self.assertEqual(row['cleanup_errors'], [])
                runs.add(row['run'])
                suffix = '' if step is None else '-step%s' % step
                output = root / ('01-new-throughput-20000' + suffix)
                protocol = json.loads((output / 'protocol.json').read_text())
                saved = json.loads((output / 'result.json').read_text())
                starts = json.loads((output / 'load-starts.json').read_text())
                self.assertEqual(len(starts), 20)
                self.assertEqual(saved, row)
                for item in starts:
                    argv = item['arguments']
                    if step is None:
                        self.assertNotIn('--block-tx-step', argv)
                    else:
                        self.assertEqual(argv.count('--block-tx-step'), 1)
                        self.assertEqual(argv[argv.index('--block-tx-step') + 1], str(step))
                for evidence in (row, row['case'], protocol, protocol['case']):
                    if step is None:
                        self.assertNotIn('block_tx_step', evidence)
                    else:
                        self.assertEqual(evidence['block_tx_step'], step)
                for settings in (protocol['node_settings'], protocol['plan']['node_settings']):
                    if step is None:
                        self.assertNotIn('block-tx-step', settings)
                    else:
                        self.assertEqual(settings['block-tx-step'], step)
            self.assertEqual(len(runs), 5)
            self.assertEqual([item['action'] for item in calls], ['start', 'stop', 'collect', 'delete-db'] * 5)

    def test_run_summary_uses_schedule_size_and_retains_step_protocol(self):
        for steps in (None, [0, 500, 1000, 2000]):
            with self.subTest(steps=steps), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                context = dict(run_id='comparison', run_owner='a' * 32, result_dir=str(root / 'results'),
                               dataset='dataset', genesis_file='genesis', dataset_sha256='a' * 64,
                               genesis_sha256='a' * 64, releases=[dict(id='old'), dict(id='new')])
                if steps is not None:
                    context.update(releases=[dict(id='new')], block_tx_steps=steps)
                def Case(context, genesis, workers, release, kind, rate, ordinal, modules, step):
                    return dict(success=True, case=dict(group=kind, block_tx_step=step),
                                latency=dict(all_samples_within_one_second=True))
                with mock.patch.object(B, 'ReadContext', return_value=(context, {})), \
                     mock.patch.object(B, 'Digest', return_value='a' * 64), \
                     mock.patch.object(B, 'RunCase', side_effect=Case) as run_case, \
                     mock.patch.dict(sys.modules, {name: SimpleNamespace() for name in ('Cluster', 'Experiments', 'RetestCases')}), \
                     mock.patch('builtins.print'):
                    self.assertEqual(B.Run(root / 'unused.json'), 0)
                summary = json.loads((root / 'results/results.json').read_text())
                protocol = json.loads((root / 'results/protocol.json').read_text())
                self.assertTrue(summary['passed'])
                self.assertTrue(summary['latency_one_second_target_passed'])
                self.assertEqual(run_case.call_count, 6 if steps is None else 12)
                if steps is None:
                    self.assertNotIn('block_tx_steps', protocol)
                else:
                    self.assertEqual(protocol['block_tx_steps'], steps)
                    self.assertEqual(summary['fixed_protocol']['block_tx_steps'], steps)


class LifecycleBatchTests(unittest.TestCase):
    def test_offline_audit_restart_keeps_explicit_zero_step(self):
        release = dict(id='new', sha256='a' * 64)
        calls = []
        nodes = [dict(id=node['node_id']) for node in Nodes()]
        class Workers:
            def Call(self, action, run, selected, genesis=None, arguments=None, restart=False):
                calls.append((action, arguments))
                return {node['id']: dict(binary_sha256=release['sha256']) for node in nodes}
        cluster = B.ClusterType(SimpleNamespace(Cluster=StubCluster))(
            Workers(), release, nodes, 'case-step0', {}, {'block-tx-step': 0}, Path('.'))
        try:
            def Restart(node):
                return cluster.AuditWorker(node, dict(action='start', run='case-step0', restart=True,
                    arguments=['--port', 18089, '--produce', 0]))
            with concurrent.futures.ThreadPoolExecutor(20) as pool:
                self.assertEqual(len(list(pool.map(Restart, nodes))), 20)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], 'start')
            for arguments in calls[0][1].values():
                self.assertEqual(arguments[arguments.index('--block-tx-step') + 1], '0')
        finally:
            cluster.pool.shutdown()

    def test_parallel_stop_and_audit_restart_are_single_fleet_operations(self):
        release = dict(id='old', sha256='a' * 64)
        calls = []
        lock = threading.Lock()
        nodes = [dict(id=node['node_id']) for node in Nodes()]
        class Workers:
            def Call(self, action, run, selected, genesis=None, arguments=None, restart=False):
                with lock:
                    calls.append((action, restart, arguments))
                return {node['id']: dict(stopped=True, binary_sha256=release['sha256']) for node in nodes}
        cluster = B.ClusterType(SimpleNamespace(Cluster=StubCluster))(
            Workers(), release, nodes, 'case1', {}, {}, Path('.'))
        try:
            with concurrent.futures.ThreadPoolExecutor(20) as pool:
                results = list(pool.map(cluster.StopOne, range(20)))
            self.assertEqual(len(results), 20)
            self.assertEqual([row[0] for row in calls], ['stop'])
            def Restart(node):
                return cluster.AuditWorker(node, dict(action='start', run='case1', restart=True,
                    arguments=['--port', 18089, '--produce', 0]))
            with concurrent.futures.ThreadPoolExecutor(20) as pool:
                results = list(pool.map(Restart, nodes))
            self.assertEqual(len(results), 20)
            self.assertEqual([row[0] for row in calls], ['stop', 'start'])
            self.assertTrue(calls[1][1])
            self.assertEqual(set(calls[1][2]), {node['id'] for node in nodes})
            self.assertTrue(all(arguments[-2:] == ['--experiment-index', '1'] for arguments in calls[1][2].values()))
            self.assertFalse(cluster.stopped)
            cluster.StopAll()
            self.assertEqual([row[0] for row in calls], ['stop', 'start', 'stop'])
        finally:
            cluster.pool.shutdown()


class LatencyEvidenceTests(unittest.TestCase):
    def Evidence(self):
        expected = [dict(index=index, hash='%064x' % index, scheduled=10 + index / 10) for index in range(100)]
        position = dict(height=1, block='b' * 64)
        samples = [dict(**sample, included_at=sample['scheduled'] + .2,
                        send_begin=sample['scheduled'] + .01, observed_position=position) for sample in expected]
        names = [node['node_id'] for node in Nodes()]
        final = {name: {sample['hash']: position for sample in samples} for name in names}
        canonical = {name: [dict(hash=position['block'])] for name in names}
        return samples, expected, final, canonical, names

    def test_all_twenty_canonical_chains_are_required(self):
        evidence = self.Evidence()
        self.assertTrue(B.LatencySummary(*evidence)['passed'])
        evidence[3][evidence[4][-1]][0]['hash'] = 'c' * 64
        result = B.LatencySummary(*evidence)
        self.assertFalse(result['passed'])
        self.assertEqual(result['missing_samples'], 100)
        self.assertEqual(result['within_one_second_fraction'], 0)

    def test_missing_observations_remain_in_percentile_and_deadline_population(self):
        samples, expected, final, canonical, names = self.Evidence()
        result = B.LatencySummary(samples[:-2], expected, final, canonical, names)
        self.assertFalse(result['passed'])
        self.assertEqual(result['expected_samples'], 100)
        self.assertEqual(result['missing_samples'], 2)
        self.assertEqual(result['within_one_second_fraction'], .98)
        self.assertIsNone(result['p99_upper_ms'])
        self.assertEqual(result['unbounded_samples_in_percentile_population'], 2)


class ArchivedSenderIntegrationTests(unittest.TestCase):
    def test_latency_samples_use_original_cutoff_sender_at_ten_hz(self):
        sys.path.insert(0, str(B.METHODS))
        # Sender construction does not use process inspection. Keep this local
        # contract test independent of the controller's optional psutil install.
        patcher = mock.patch.dict(sys.modules, {'psutil': SimpleNamespace()})
        patcher.start()
        self.addCleanup(patcher.stop)
        import Cluster as C
        import Experiments as E
        original = E.Load
        registered, observers = B.InstallMeasurement(E, C, dict(sha256='a' * 64), True)
        try:
            sender_class = inspect.getclosurevars(E.LoadCase).nonlocals['CutoffLoad']
            nodes = [dict(id=node['node_id'], host=node['p2p_address'], port=node['port']) for node in Nodes()]
            load = sender_class(SimpleNamespace(nodes=nodes), Path('not-opened-by-constructor.bin'),
                                20000, 12, 'steady', 'broadcast', 2, 128, False)
            self.assertIs(E.Load, original)
            self.assertEqual(load.every, 2000)
            self.assertEqual(load.count, 240000)
            self.assertEqual(load.batch, 128)
            self.assertFalse(load.parallel_ingress)
            self.assertEqual(sender_class.Run.__module__, 'RetestObserve')
            self.assertEqual(registered, [load])
        finally:
            for observer in observers:
                observer.Close()


class DrainBudgetTests(unittest.TestCase):
    def test_post_budget_stop_or_duplicate_host_does_not_prove_timely_drain(self):
        names = [node['node_id'] for node in Nodes()]
        rows = [dict(node=name, start_seconds=16, end_seconds=16.1,
                     head=dict(hash='b' * 64, transactions=240000)) for name in names]
        self.assertTrue(B.DrainWithinBudget(dict(nodes=rows), {240000}, names))
        rows[-1]['end_seconds'] = 17.01
        self.assertFalse(B.DrainWithinBudget(dict(nodes=rows), {240000}, names))
        rows[-1]['end_seconds'] = 16.1
        rows[-1]['node'] = rows[0]['node']
        self.assertFalse(B.DrainWithinBudget(dict(nodes=rows), {240000}, names))


if __name__ == '__main__':
    unittest.main()
