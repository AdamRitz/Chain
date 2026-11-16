"""Offline boundary checks only; importing these helpers never starts a node."""
import unittest

from RetestCases import EvaluateBoundaries


class MeasurementTest(unittest.TestCase):
    def Points(self):
        return [dict(target_seconds=2, common=dict(height=40, transactions=20000),
                     common_observation_interval_seconds=[2, 2.02]),
                dict(target_seconds=12, common=dict(height=240, transactions=220000),
                     common_observation_interval_seconds=[12, 12.02])]

    def Proof(self):
        return dict(passed=True, state_audit_passed=True, node_count=20)

    def test_normal_boundary_formula(self):
        value = EvaluateBoundaries(self.Points(), self.Proof())
        self.assertTrue(value['passed'])
        self.assertAlmostEqual(value['tps'], 20000)
        self.assertAlmostEqual(value['mean_common_block_interval_ms'], 50)

    def test_late_narrow_acquisition_is_rejected(self):
        points = self.Points()
        points[0]['common_observation_interval_seconds'] = [3, 3.02]
        value = EvaluateBoundaries(points, self.Proof())
        self.assertFalse(value['passed'])
        self.assertTrue(any('outside' in reason for reason in value['reasons']))
        self.assertNotIn('tps', value)

    def test_early_acquisition_is_rejected(self):
        points = self.Points()
        points[0]['common_observation_interval_seconds'] = [1.99, 2.02]
        self.assertFalse(EvaluateBoundaries(points, self.Proof())['passed'])

    def test_slow_acquisition_is_rejected(self):
        points = self.Points()
        points[1]['common_observation_interval_seconds'] = [12, 13]
        self.assertFalse(EvaluateBoundaries(points, self.Proof())['passed'])

    def test_missing_canonical_proof_is_rejected(self):
        proof = self.Proof()
        proof['passed'] = False
        self.assertFalse(EvaluateBoundaries(self.Points(), proof)['passed'])

    def test_missing_or_failed_final_state_audit_is_rejected(self):
        for proof in [dict(passed=True), dict(passed=True, state_audit_passed=False)]:
            self.assertFalse(EvaluateBoundaries(self.Points(), proof)['passed'])

    def test_missing_endpoint_is_rejected(self):
        self.assertFalse(EvaluateBoundaries(self.Points()[:1], self.Proof())['passed'])


if __name__ == '__main__':
    unittest.main()
