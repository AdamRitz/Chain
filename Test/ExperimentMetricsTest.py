"""Deterministic tests for benchmark sampling and five-minute metric accumulation."""
import threading
from types import SimpleNamespace
import unittest

from Experiments import CheckSamples, LoadMetrics, NodeResources


def Stats(raw_us, cpu=0, reorgs=0, network=0, **changes):
    return {'clock': {'raw_us': raw_us}, 'process_cpu_seconds': cpu,
            'reorganizations': reorgs, 'network_bytes': network,
            'network_sent_bytes': network * 2, 'process_write_bytes': network * 3,
            'db_write_ms': network / 10, 'resident_bytes': 1000,
            'invalid': 0, 'rejected': 0, 'max_reorg_depth': 0,
            'pool': 2, 'pending': 3, 'pending_tx_verify': 1,
            'pending_block_verify': 4, 'pending_metadata': 5, **changes}


class FakeClient:
    def __init__(self, resolved):
        self.resolved, self.requests = resolved, []

    def Query(self, kind, payload):
        if kind != 31 or len(payload) % 32 or len(payload) > 128 * 32:
            raise AssertionError('Invalid sample query')
        indices = [int.from_bytes(payload[start:start + 32], 'little')
                   for start in range(0, len(payload), 32)]
        self.requests.append(indices)
        return [self.resolved(index) for index in indices]


class FakeCluster:
    def __init__(self, *resolved):
        self.clients = [FakeClient(function) for function in resolved]
        self.offline = set()

    def All(self, function):
        return [function(index) for index in range(len(self.clients))]


def Samples(count):
    return SimpleNamespace(samples=[{'hash': index.to_bytes(32, 'little').hex(), 'included_at': None}
                                    for index in range(count)], sample_cursor=0, lock=threading.Lock())


class ExperimentMetricsTest(unittest.TestCase):
    def test_cached_statistics_use_node_time(self):
        tracker = NodeResources()
        first = Stats(10_000_000, cpu=10, reorgs=20, network=100)
        tracker.Add(2, first)
        for seconds in (3, 4, 5, 6):
            tracker.Add(seconds, first)
        tracker.Add(7, Stats(12_000_000, cpu=13, reorgs=23, network=700))
        result = tracker.Summary('one')
        self.assertEqual(result['cpu_core_equivalents'], 1.5)
        self.assertEqual(result['received_bytes_per_second'], 300)
        self.assertEqual(result['statistics_seconds'], 2)
        self.assertEqual(result['statistics_intervals'], 1)
        self.assertEqual(result['measurement_reorganizations'], 3)
        self.assertEqual(result['statistics_time_sources'], ['node_raw_us'])

    def test_process_restart_excludes_counter_reset_interval(self):
        tracker = NodeResources()
        for seconds, cpu, reorgs in [(1, 10, 100), (2, 12, 103), (3, 1, 0), (4, 3, 2)]:
            tracker.Add(seconds, Stats(seconds * 1_000_000, cpu=cpu, reorgs=reorgs))
        result = tracker.Summary('one')
        self.assertEqual(result['cpu_core_equivalents'], 2)
        self.assertEqual(result['statistics_seconds'], 2)
        self.assertEqual(result['measurement_reorganizations'], 5)
        self.assertEqual(result['statistics_resets'], 1)

    def test_legacy_statistics_use_controller_time(self):
        tracker = NodeResources()
        for seconds, cpu in [(2, 10), (6, 12)]:
            row = Stats(0, cpu=cpu)
            del row['clock']
            tracker.Add(seconds, row)
        result = tracker.Summary('legacy')
        self.assertEqual(result['cpu_core_equivalents'], .5)
        self.assertEqual(result['statistics_time_sources'], ['controller'])

    def test_missing_resource_counter_is_not_reported_as_zero(self):
        tracker = NodeResources()
        for seconds in (1, 2):
            row = Stats(seconds * 1_000_000, cpu=seconds)
            del row['process_write_bytes']
            tracker.Add(seconds, row)
        self.assertIsNone(tracker.Summary('one')['process_write_bytes_per_second'])

    def test_clock_step_does_not_create_negative_rates(self):
        tracker = NodeResources()
        for elapsed, raw, cpu in [(1, 10, 10), (2, 9, 11), (3, 10, 12)]:
            tracker.Add(elapsed, Stats(raw * 1_000_000, cpu=cpu))
        self.assertEqual(tracker.Summary('one')['cpu_core_equivalents'], 1)

    def test_measurement_delta_excludes_warmup_and_drain(self):
        metrics = LoadMetrics(2, 300, ['one', 'two'])
        for seconds, count in [(1, 50), (2, 100), (302, 107), (303, 200)]:
            metrics.Add({'seconds': seconds, 'sent': seconds * 10,
                         'common': {'transactions': seconds * 10 - 1},
                         'nodes': [Stats(seconds * 1_000_000, cpu=seconds, reorgs=count),
                                   Stats(seconds * 1_000_000, cpu=seconds, reorgs=count + 10)]})
        self.assertEqual(len(metrics.measured), 2)
        self.assertEqual(sum(node.reorganizations for node in metrics.nodes), 14)
        self.assertEqual(metrics.reorganizations_sum, 410)
        self.assertEqual(metrics.measured[0]['backlog'], 10)
        self.assertEqual(metrics.measured[0]['tx_backlog'], 6)

    def test_full_statistics_are_not_retained_in_memory(self):
        metrics = LoadMetrics(0, 300, ['one'])
        row = Stats(1_000_000, large_detail='x' * 100_000)
        metrics.Add({'seconds': 1, 'sent': 1, 'common': {'transactions': 0}, 'nodes': [row]})
        self.assertNotIn('nodes', metrics.measured[0])
        self.assertNotIn('large_detail', metrics.nodes[0].previous)
        self.assertNotIn('large_detail', metrics.measured[0])

    def test_unresolved_first_page_does_not_starve_later_samples(self):
        cluster = FakeCluster(lambda index: None if index < 128 else {'block': 'same'})
        load = Samples(300)
        self.assertEqual(CheckSamples(cluster, load), 0)
        self.assertEqual(CheckSamples(cluster, load), 128)
        self.assertEqual(CheckSamples(cluster, load), 44)
        self.assertEqual(sum(sample['included_at'] is not None for sample in load.samples), 172)
        self.assertEqual(cluster.clients[0].requests[1], list(range(128, 256)))

    def test_flush_visits_every_pending_sample_even_when_first_page_unresolved(self):
        cluster = FakeCluster(lambda index: None if index < 128 else {'block': 'same'})
        load = Samples(400)
        self.assertEqual(CheckSamples(cluster, load, flush=True), 272)
        self.assertEqual(len(cluster.clients[0].requests), 4)
        self.assertEqual(sorted(index for page in cluster.clients[0].requests for index in page), list(range(400)))

    def test_confirmation_requires_every_node_to_match(self):
        cluster = FakeCluster(lambda index: 'a', lambda index: 'a' if index == 0 else 'b')
        load = Samples(2)
        self.assertEqual(CheckSamples(cluster, load, flush=True), 1)
        self.assertIsNone(load.samples[1]['included_at'])

    def test_offline_or_empty_samples_do_not_query(self):
        cluster = FakeCluster(lambda index: 'a')
        self.assertEqual(CheckSamples(cluster, Samples(0), flush=True), 0)
        cluster.offline.add(0)
        self.assertEqual(CheckSamples(cluster, Samples(300), flush=True), 0)
        self.assertEqual(cluster.clients[0].requests, [])

    def test_new_samples_after_cursor_wrap_are_queried(self):
        cluster = FakeCluster(lambda index: 'same')
        load = Samples(1)
        self.assertEqual(CheckSamples(cluster, load), 1)
        load.samples.extend(Samples(3).samples[1:])
        self.assertEqual(CheckSamples(cluster, load), 2)


if __name__ == '__main__':
    unittest.main()
