"""Native-state verification while VDF nodes continue producing empty blocks."""
import copy
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'Test'))
from Experiments import VerifyNativeState


class Client:
    def __init__(self, cluster, index):
        self.cluster, self.index = cluster, index

    def Query(self, kind, payload=b''):
        cluster = self.cluster
        cluster.queries.append((self.index, kind, payload))
        if kind == 37:
            accounts = cluster.accounts[self.index]
            return [copy.deepcopy(accounts[payload[offset:offset+32].hex()])
                    for offset in range(0, len(payload), 32)]
        if kind == 35:
            return copy.deepcopy(cluster.states[self.index])
        if kind == 29:
            if struct.unpack('<Q', payload)[0] != 66:
                raise AssertionError('Verification must query the fixed common height')
            return copy.deepcopy(cluster.anchors[self.index])
        raise AssertionError(f'Unexpected query {kind}')


class Cluster:
    def __init__(self, accounts):
        self.nodes = [{'id': f'node{index:03}'} for index in range(3)]
        self.clients = [Client(self, index) for index in range(3)]
        self.accounts = [copy.deepcopy(accounts) for _ in self.nodes]
        state = {'state_hash': 'a' * 64, 'head': 'b' * 64,
                 'transactions': 3, 'supply': 197, 'burned': 3}
        self.states = [copy.deepcopy(state) for _ in self.nodes]
        self.common = {'height': 66, 'hash': 'c' * 64, 'transactions': 3}
        self.anchors = [copy.deepcopy(self.common) for _ in self.nodes]
        self.queries, self.common_calls = [], 0

    def Common(self):
        self.common_calls += 1
        return copy.deepcopy(self.common)

    def All(self, function):
        return [function(index) for index in range(len(self.nodes))]


class StateVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.dataset = Path(self.temporary.name) / 'transactions.bin'
        self.first, self.second = bytes([1]) * 32, bytes([2]) * 32
        def Transaction(sender, receiver, amount, nonce):
            return sender + receiver + struct.pack('<QQ', amount, nonce) + bytes(96)
        self.dataset.write_bytes(Transaction(self.first, self.second, 7, 1)
                                 + Transaction(self.second, self.first, 5, 1)
                                 + Transaction(self.first, self.second, 7, 2))
        self.genesis = {'base_fee': 1, 'vdf': {'enabled': True, 'start_time_ms': 50},
                        'accounts': [{'public_key': key.hex(), 'balance': 100}
                                     for key in (self.first, self.second)]}
        self.accounts = {self.first.hex(): {'balance': 89, 'nonce': 2},
                         self.second.hex(): {'balance': 108, 'nonce': 1}}
        self.cluster = Cluster(self.accounts)

    def Verify(self):
        return VerifyNativeState(self.cluster, self.dataset, self.genesis, 3, 2)

    def test_vdf_head_advance_preserves_raw_states_and_common_block(self):
        for index, state in enumerate(self.cluster.states):
            state['head'] = f'{index + 1:064x}'
        result = self.Verify()
        self.assertNotIn('head', result['state'])
        self.assertEqual(result['accounts_checked_per_node'], 2)
        self.assertEqual(result['common_block'], self.cluster.common)
        self.assertEqual(result['node_states'], {
            node['id']: state for node, state in zip(self.cluster.nodes, self.cluster.states)})
        self.assertEqual(self.cluster.common_calls, 1)
        self.assertEqual([kind for _, kind, _ in self.cluster.queries], [37] * 3 + [35] * 3 + [29] * 3)

    def test_vdf_enabled_defaults_to_true_when_parameters_are_present(self):
        del self.genesis['vdf']['enabled']
        self.cluster.states[1]['head'] = 'd' * 64
        self.assertIn('common_block', self.Verify())

    def test_ordinary_or_disabled_vdf_keeps_exact_head_comparison(self):
        for configuration in (None, {'enabled': False}):
            with self.subTest(configuration=configuration):
                self.cluster = Cluster(self.accounts)
                if configuration is None:
                    self.genesis.pop('vdf', None)
                else:
                    self.genesis['vdf'] = configuration
                result = self.Verify()
                self.assertEqual(result, {'accounts_checked_per_node': 2, 'state': self.cluster.states[0]})
                self.assertEqual(self.cluster.common_calls, 0)
                self.cluster.states[1]['head'] = 'd' * 64
                with self.assertRaisesRegex(AssertionError, 'Chain head'):
                    self.Verify()

    def test_vdf_still_checks_each_account_balance_and_nonce(self):
        for key in self.accounts:
            for field in ('balance', 'nonce'):
                with self.subTest(account=key, field=field):
                    self.cluster = Cluster(self.accounts)
                    self.cluster.accounts[1][key][field] += 1
                    with self.assertRaisesRegex(AssertionError, 'Account state mismatch at node001'):
                        self.Verify()

    def test_vdf_ignores_only_head_and_compares_every_other_field(self):
        for field, wrong in [('state_hash', 'd' * 64), ('transactions', 4),
                             ('supply', 198), ('burned', 4), ('future_state_field', 1)]:
            with self.subTest(field=field):
                self.cluster = Cluster(self.accounts)
                self.cluster.states[1][field] = wrong
                with self.assertRaisesRegex(AssertionError, 'Account/contract state differs'):
                    self.Verify()

    def test_vdf_rejects_consistent_but_wrong_transaction_or_fee_totals(self):
        for field in ('transactions', 'burned'):
            with self.subTest(field=field):
                self.cluster = Cluster(self.accounts)
                for state in self.cluster.states:
                    state[field] += 1
                with self.assertRaisesRegex(AssertionError, 'Unique transaction count or burned fees mismatch'):
                    self.Verify()

    def test_vdf_requires_all_transactions_in_the_common_chain(self):
        for common in (None, {'height': 66, 'hash': 'c' * 64, 'transactions': 2}):
            with self.subTest(common=common):
                self.cluster = Cluster(self.accounts)
                self.cluster.common = common
                with self.assertRaisesRegex(AssertionError, 'Common chain does not contain all sent transactions'):
                    self.Verify()
                self.assertFalse(self.cluster.queries)

    def test_vdf_rejects_common_block_replacement_or_missing_history(self):
        for anchor in (None, {'height': 66, 'hash': 'd' * 64, 'transactions': 3},
                       {'height': 66, 'hash': 'c' * 64, 'transactions': 2}):
            with self.subTest(anchor=anchor):
                self.cluster = Cluster(self.accounts)
                self.cluster.anchors[2] = anchor
                with self.assertRaisesRegex(AssertionError, 'Common transaction-containing block changed'):
                    self.Verify()


if __name__ == '__main__':
    unittest.main()
