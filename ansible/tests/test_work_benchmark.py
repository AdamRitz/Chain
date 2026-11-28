"""Work history, continuous nonce ranges and one-start rate series."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cluster_benchmark as benchmark
import cluster_plan
import cluster_quick as quick
from Experiments import Load, NodeResources


class WorkBenchmarkTests(unittest.TestCase):
    def test_offline_work_prepare_preserves_accounts_and_keeps_seeds_private(self):
        from nacl.signing import SigningKey
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keys = [SigningKey(bytes([n])*32) for n in (1, 2)]
            accounts = {'base_fee': 0, 'accounts': [
                {'public_key': bytes(key.verify_key).hex(), 'balance': 1000} for key in keys]}
            dataset = root/'tx.bin'
            with dataset.open('wb') as stream:
                for index in range(8):
                    key = keys[index % 2]
                    body = bytes(key.verify_key)+bytes(keys[(index+1) % 2].verify_key)+struct.pack('<QQ', 1, index//2+1)
                    digest = hashlib.blake2b(body, digest_size=32).digest()
                    stream.write(body+digest+key.sign(digest).signature)
            quick.Save(Path(str(dataset)+'.genesis.json'), accounts)
            nodes = root/'nodes.json'
            quick.Save(nodes, [{'id': f'node{n}', 'host': f'10.0.0.{n}'} for n in range(1, 6)])
            out = root/'prepare'
            args = quick.Parser().parse_args(['prepare', '--nodes', str(nodes), '--dataset', str(dataset),
                '--wallets', '2', '--rates', '1', '3', '--warmup', '0', '--duration', '2',
                '--work-election', '--output', str(out)])
            invoke = mock.Mock(side_effect=AssertionError('Offline preparation used Ansible'))
            self.assertEqual(quick.Execute(args, invoke), 0)
            invoke.assert_not_called()
            genesis = json.loads((out/'genesis.json').read_text())
            self.assertEqual(genesis['accounts'], accounts['accounts'])
            self.assertEqual(genesis['vdf']['producer_timeout_slots'], 2)
            identities = json.loads((out/'identities.json').read_text())
            variables = json.loads((out/'variables.json').read_text())
            self.assertEqual(genesis['vdf']['bootstrap_producers'], [row['public_key'] for row in identities])
            public = ''.join(path.read_text() for path in out.glob('*.json'))
            for row in identities:
                seed = Path(variables['chain_cluster_identity_files'][row['id']]).read_text().strip()
                self.assertNotIn(seed, public)
                self.assertEqual(bytes(SigningKey(bytes.fromhex(seed)).verify_key).hex(), row['public_key'])
            self.assertEqual(json.loads((out/'test-plan.json').read_text())['minimum_work_startup_seconds'], 1088)
            self.assertEqual(json.loads((out/'dataset.json').read_text())['required_count'], 8)

    def Rows(self):
        keys = [str(n)*64 for n in range(1, 6)]
        return [{'vdf': {'enabled': True, 'work_election': True, 'producer_rotation': True,
            'epoch': 33, 'public_key': key, 'producers': keys, 'next_producers': keys[::-1],
            'consecutive_blocks': 32, 'producer_timeout_slots': 2, 'work_sample_count': 32,
            'work_mature_rounds': 32, 'work_completed': 32, 'work_iterations': 65536,
            'work_groups': 4, 'work_group_size': 8, 'election_frozen': True,
            'candidates': [{'public_key': member} for member in keys]}} for key in keys]

    def test_full_work_history_and_actual_mature_election_are_required(self):
        genesis = {'vdf': {'enabled': True, 'work_election': True, 'producer_rotation': True,
            'consecutive_blocks': 32, 'producer_timeout_slots': 2, 'work_groups': 4, 'work_group_size': 8}}
        rows = self.Rows()
        self.assertTrue(benchmark.ConsensusReady(rows, genesis))
        election = benchmark.MatureElection(rows)
        self.assertFalse(benchmark.ElectionAdopted(rows, election))
        rows[0]['vdf']['work_completed'] = 31
        self.assertFalse(benchmark.ConsensusReady(rows, genesis))
        rows[0]['vdf']['work_completed'] = 32
        rows[0]['vdf']['candidates'] = []
        self.assertIsNone(benchmark.MatureElection(rows))
        rows = self.Rows()
        for row in rows:
            row['vdf']['epoch'] = 34
            row['vdf']['producers'] = election['committee']
        self.assertTrue(benchmark.ElectionAdopted(rows, election))
        rows[-1]['vdf']['producers'] = list(reversed(election['committee']))
        self.assertFalse(benchmark.ElectionAdopted(rows, election))

    def test_sender_uses_next_dataset_segment_and_sample_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'tx.bin'
            records = [bytes([n])*176 for n in range(1, 5)]
            path.write_bytes(b''.join(records))
            connection = mock.MagicMock()
            view = SimpleNamespace(nodes=[{'host': 'unused', 'port': 1}], offline=set())
            load = Load(view, path, 100000, .00002, 'steady', 'shard', 100000,
                        batch=2, dataset_offset=2)
            with mock.patch('Experiments.socket.create_connection', return_value=connection):
                load.Run()
            self.assertIsNone(load.error)
            self.assertEqual(load.sent, 2)
            self.assertEqual(connection.sendall.call_args_list[-1].args[0][5:], b''.join(records[2:]))
            self.assertEqual([row['dataset_index'] for row in load.samples], [2, 3])
            self.assertEqual([row['hash'] for row in load.samples], [record[80:112].hex() for record in records[2:]])

    def test_series_starts_once_advances_nonce_offset_and_stops_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = {'output': str(Path(temporary)/'run'), 'rate': 8000, 'rates': [8000, 9000, 10000],
                'warmup': 2, 'duration': 30, 'drain_seconds': 5, 'poll_seconds': .1, 'sample_hz': 20,
                'batch': 128, 'wallets': 1024, 'dataset': '/unused', 'metrics_period': 1}
            cluster = mock.Mock(query_sweeps=[], cleanup_errors=[])
            plan = {'nodes': [{'id': 'one'}], 'injectors': ['one'], 'graph': {}}
            offsets = []
            def Case(cluster, case, parameters, dataset, genesis, output):
                offsets.append(parameters['dataset_offset'])
                return {'drained': len(offsets) == 1, 'unresolved_samples': 0, 'early_stop': None,
                        'sustainable': True, 'p50_ms': 500, 'sent_unique': 256000}
            with mock.patch.object(benchmark, 'Validate', return_value=(plan, {})), \
                 mock.patch.object(benchmark, 'SparseCluster', return_value=cluster), \
                 mock.patch.object(benchmark.Experiments, 'LoadCase', side_effect=Case):
                result = benchmark.Run(context)
            self.assertEqual(offsets, [0, 256000])
            cluster.Start.assert_called_once()
            cluster.Close.assert_called_once()
            self.assertFalse(result['ok'])
            self.assertEqual(result['completed_rates'], 2)
            self.assertEqual(result['cumulative_sent_unique'], 512000)

    def test_work_verification_metrics_use_measurement_counter_differences(self):
        tracker = NodeResources()
        tracker.Add(0, {'process_cpu_seconds': 10, 'vdf': {'work_proof_verify_ms': 2000, 'proof_checks': 64}})
        tracker.Add(2, {'process_cpu_seconds': 11, 'vdf': {'work_proof_verify_ms': 2500, 'proof_checks': 96}})
        result = tracker.Summary('one')['work']
        self.assertEqual(result['proof_checks'], {'delta': 32, 'per_second': 16})
        self.assertEqual(result['work_proof_verify_ms'], {'delta': 500, 'per_second': 250})


if __name__ == '__main__':
    unittest.main()
