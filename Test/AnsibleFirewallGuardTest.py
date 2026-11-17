"""Firewall guard tests use a fake daemon; they never change the host firewall."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

SPEC = importlib.util.spec_from_file_location('firewall_guard',
    Path(__file__).resolve().parents[1] / 'ansible/scripts/firewall_guard.py')
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)
RUN, OWNER = 'isolated-test', 'a' * 32


class Daemon:
    def __init__(self):
        self.active, self.zones, self.rules = True, ['public', 'internal'], set()
        self.calls, self.fail_add_after_apply, self.fail_remove = [], False, False

    def Active(self):
        return self.active

    def Zones(self):
        return self.zones

    def Present(self, zone, rule):
        return (zone, rule) in self.rules

    def Add(self, zone, rule, seconds):
        self.calls.append(('add', zone, rule, seconds))
        self.rules.add((zone, rule))
        if self.fail_add_after_apply:
            raise RuntimeError('reply lost after apply')

    def Remove(self, zone, rule):
        self.calls.append(('remove', zone, rule))
        if self.fail_remove:
            raise RuntimeError('temporary removal failure')
        self.rules.discard((zone, rule))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path, self.daemon = Path(self.temp.name) / (RUN + '.json'), Daemon()
        self.now = 1000

    def Call(self, action, owner=OWNER):
        return G.Guard(action, RUN, owner, self.path, self.daemon, now=lambda: self.now)

    def test_only_owned_rule_removed_preexisting_and_unrelated_preserved(self):
        rule = G.Rule(RUN, OWNER)
        self.daemon.rules = {('public', rule), ('public', 'unrelated original rule')}
        result = self.Call('allow')
        self.assertTrue(result['changed'])
        self.assertIn(('internal', rule), self.daemon.rules)
        self.assertEqual(self.daemon.calls, [('add', 'internal', rule, 2400)])
        result = self.Call('restore')
        self.assertTrue(result['restored'])
        self.assertEqual(self.daemon.rules, {('public', rule), ('public', 'unrelated original rule')})
        self.assertEqual(self.daemon.calls[-1], ('remove', 'internal', rule))

    def test_open_retry_does_not_extend_timeout_and_restore_is_idempotent(self):
        self.Call('allow')
        self.now += 50
        self.assertFalse(self.Call('allow')['changed'])
        self.assertEqual(len(self.daemon.calls), 2)
        self.Call('restore')
        calls = list(self.daemon.calls)
        # A subsequent unrelated recreation must not be removed by a retry.
        self.daemon.rules.add(('public', G.Rule(RUN, OWNER)))
        self.assertFalse(self.Call('restore')['changed'])
        self.assertEqual(self.daemon.calls, calls)
        with self.assertRaisesRegex(RuntimeError, 'already restored'):
            self.Call('allow')

    def test_lost_add_reply_has_durable_intent_and_can_restore(self):
        self.daemon.fail_add_after_apply = True
        with self.assertRaisesRegex(RuntimeError, 'reply lost'):
            self.Call('allow')
        saved = json.loads(self.path.read_text())
        self.assertTrue(saved['zones'][0]['intent'])
        self.assertFalse(saved['zones'][0]['preexisting'])
        self.assertTrue(self.Call('restore')['restored'])
        self.assertFalse(self.daemon.rules)

    def test_failed_restore_is_retryable_and_already_expired_rules_are_harmless(self):
        self.Call('allow')
        self.daemon.fail_remove = True
        with self.assertRaisesRegex(RuntimeError, 'temporary removal'):
            self.Call('restore')
        self.assertFalse(json.loads(self.path.read_text())['restored'])
        self.daemon.fail_remove = False
        self.daemon.rules.clear()  # daemon timeout or reload
        self.assertTrue(self.Call('restore')['restored'])

    def test_retry_readds_missing_runtime_rule_only_for_remaining_lease(self):
        self.Call('allow')
        self.now += 60
        self.daemon.rules.clear()
        self.Call('allow')
        self.assertEqual([call[-1] for call in self.daemon.calls], [2400, 2400, 2340, 2340])
        self.now += 2400
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            self.Call('allow')
        self.assertEqual(len(self.daemon.calls), 4)

    def test_wrong_owner_and_tampered_rule_cannot_delete_rules(self):
        self.Call('allow')
        calls = list(self.daemon.calls)
        with self.assertRaisesRegex(RuntimeError, 'ownership mismatch'):
            self.Call('restore', 'b' * 32)
        self.assertEqual(self.daemon.calls, calls)
        saved = json.loads(self.path.read_text())
        saved['rule'] = 'unrelated rule'
        self.path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(RuntimeError, 'rule or timeout changed'):
            self.Call('restore')
        self.assertEqual(self.daemon.calls, calls)

    def test_inactive_daemon_is_not_started_and_unknown_run_restores_without_mutation(self):
        self.assertTrue(self.Call('restore')['never_opened'])
        self.daemon.active = False
        self.assertIn('skipped', self.Call('allow'))
        self.assertTrue(self.Call('restore')['restored'])
        self.assertFalse(self.daemon.calls)

    def test_unsafe_identifiers_rejected_before_file_creation(self):
        for run, owner in (('../escape', OWNER), (RUN, 'wrong'), ('x' * 61, OWNER)):
            with self.subTest(run=run, owner=owner), self.assertRaises(RuntimeError):
                G.Guard('allow', run, owner, self.path, self.daemon)
        self.assertFalse(self.path.exists())


class CommandTests(unittest.TestCase):
    def test_exact_runtime_scope_timeout_and_active_zone_parsing(self):
        calls = []
        def Runner(argv):
            calls.append(argv)
            output = 'public\n  interfaces: eth0\ninternal\n  sources: 192.168.0.0/24\n' if '--get-active-zones' in argv else ''
            return SimpleNamespace(returncode=0, stdout=output, stderr='')
        firewall = G.Firewall(runner=Runner, executable='/usr/bin/firewall-cmd')
        self.assertEqual(firewall.Zones(), ['internal', 'public'])
        rule = G.Rule(RUN, OWNER)
        self.assertIn('family="ipv4" source address="192.168.0.0/24" port port="18089" protocol="tcp" accept', rule)
        firewall.Add('public', rule, 2400)
        self.assertEqual(calls[-1], ['/usr/bin/firewall-cmd', '--zone=public', '--add-rich-rule=' + rule, '--timeout=2400s'])
        firewall.Remove('public', rule)
        self.assertEqual(calls[-1], ['/usr/bin/firewall-cmd', '--zone=public', '--remove-rich-rule=' + rule])
        self.assertTrue(all('--permanent' not in argv and '--reload' not in argv for argv in calls))

    def test_firewalld_errors_are_not_misreported_as_inactive(self):
        for code, expected in ((0, True), (252, False), (1, None)):
            firewall = G.Firewall(runner=lambda argv: SimpleNamespace(returncode=code, stdout='', stderr='error'),
                                  executable='/usr/bin/firewall-cmd')
            if expected is None:
                with self.assertRaises(RuntimeError):
                    firewall.Active()
            else:
                self.assertIs(firewall.Active(), expected)


if __name__ == '__main__':
    unittest.main()
