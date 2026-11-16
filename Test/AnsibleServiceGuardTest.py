"""Service restoration must preserve identity and reject accidental overwrite."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('guard', Path(__file__).resolve().parents[1] / 'ansible/scripts/service_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def Original():
    return {service: dict(load='loaded', active='active', unit_sha256='same',
                         exe='/opt/' + service, argv=['/opt/' + service, '--config', 'original'])
            for service in guard.SERVICES}


class GuardTest(unittest.TestCase):
    def test_changed_unit_is_not_started(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps(dict(before=Original())))
            changed = Original()
            changed[guard.SERVICES[0]]['unit_sha256'] = 'another-unit'
            with patch.object(guard, 'Facts', return_value=changed), patch.object(guard, 'Run') as run:
                with self.assertRaisesRegex(RuntimeError, 'definition changed'):
                    guard.Restore(path, 'test-timer')
                run.assert_not_called()

    def test_restore_only_previously_active_services(self):
        with tempfile.TemporaryDirectory() as directory:
            before = Original()
            before[guard.SERVICES[1]].update(active='inactive', exe=None, argv=[])
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps(dict(before=before)))
            with patch.object(guard, 'Facts', return_value=before), patch.object(guard, 'Run') as run:
                result = guard.Restore(path, 'test-timer')
                self.assertTrue(result['restored'])
                self.assertTrue(json.loads(path.read_text())['restored'])
                self.assertEqual(run.call_args_list[0].args[0], ['systemctl', 'start', 'chain-node.service'])
                self.assertEqual(run.call_args_list[1].args[0], ['systemctl', 'stop', 'test-timer.timer'])

    def test_existing_run_cannot_overwrite_recovery_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'collision.json'
            path.write_text('original recovery state')
            with patch.object(guard, 'STATE_ROOT', Path(directory)), \
                 patch.object(guard.sys, 'argv', ['guard.py', 'pause', '--run', 'collision']), \
                 patch.object(guard.os, 'geteuid', return_value=0, create=True), \
                 patch.object(guard, 'Facts') as facts:
                with self.assertRaisesRegex(RuntimeError, 'already used'):
                    guard.Main()
                facts.assert_not_called()
                self.assertEqual(path.read_text(), 'original recovery state')


if __name__ == '__main__':
    unittest.main()
