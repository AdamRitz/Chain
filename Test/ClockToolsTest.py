"""Reference joins must reject absent/remote truth; split isolation is by whole run."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent/'Clock'))
from Simulate import Generate, FEATURES
from LabelTrace import Label


class ClockToolsTest(unittest.TestCase):
    def test_reference_join(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); trace, reference, output = [root/name for name in ['trace', 'reference', 'output']]
            records = [{'kind': 'observation', 'feature_version': 1, 'steady_us': 10000, 'logical_us': 50000},
                       {'kind': 'candidate', 'feature_version': 1, 'features': [0]*32, 'prepare_us': 3000,
                        'censored': True, 'outcome': 'cancelled'}]
            trace.write_text('\n'.join(json.dumps(row) for row in records))
            reference.write_text(json.dumps({'steady_us': 11000, 'reference_logical_us': 49000, 'reference_uncertainty_us': 100}))
            self.assertEqual(Label(trace, reference, output, 'whole-run', 'node1', 'mixed'), 1)
            labeled = json.loads(output.read_text())
            self.assertEqual(labeled['time_error_ms'], 3.1)
            self.assertTrue(labeled['prepare_censored'])
            self.assertEqual(labeled['label_source'], 'controlled-reference')
            reference.write_text(json.dumps({'steady_us': 90000, 'reference_logical_us': 49000, 'reference_uncertainty_us': 100}))
            self.assertEqual(Label(trace, reference, output, 'whole-run', 'node1', 'mixed'), 0)

    def test_simulation_reference_and_censoring(self):
        rows = list(Generate(77, 6, 35))
        self.assertEqual(len(FEATURES), 32)
        self.assertTrue(any(row['prepare_censored'] for row in rows))
        self.assertEqual(rows, list(Generate(77, 6, 35)))
        # Work history is causal: the first frame cannot contain the future duration label.
        self.assertTrue(all(row['features'][14] == 0 for row in rows if row['step'] == 0))
        self.assertTrue(all(row['label_source'] == 'simulation' and len(row['features']) == 32 for row in rows))

    @unittest.skipUnless(importlib.util.find_spec('numpy'), 'NumPy only required for offline training checks')
    def test_split_and_label_guard(self):
        from Train import Split, Train
        memberships = {name: Split(name) for name in ['run1', 'run2', 'run3']}
        self.assertTrue(all(Split(name) == memberships[name] for name in memberships for _ in range(20)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'bad.jsonl'
            source.write_text(json.dumps({'label_source': 'neighbor_median'}))
            with self.assertRaisesRegex(ValueError, 'controlled reference'):
                Train(source, root/'pack.json', root/'metrics.json')


if __name__ == '__main__':
    unittest.main()
