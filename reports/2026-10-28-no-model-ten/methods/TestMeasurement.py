import unittest
from Run import EvaluateBoundaries

class MeasurementTest(unittest.TestCase):
    def Points(self):
        return [dict(target_seconds=2,common=dict(height=40,transactions=20000),common_observation_interval_seconds=[2,2.02]),
                dict(target_seconds=12,common=dict(height=240,transactions=220000),common_observation_interval_seconds=[12,12.02])]
    def test_height_interval_is_distinct_from_transaction_rate(self):
        value=EvaluateBoundaries(self.Points(),{'passed':True})
        self.assertTrue(value['passed']);self.assertAlmostEqual(value['tps'],20000);self.assertAlmostEqual(value['mean_common_block_interval_ms'],50)
    def test_missing_proof_is_not_promoted(self):
        self.assertFalse(EvaluateBoundaries(self.Points(),{'passed':False})['passed'])
    def test_slow_acquisition_and_missing_endpoint_are_ineligible(self):
        points=self.Points();points[1]['common_observation_interval_seconds']=[12,13]
        self.assertFalse(EvaluateBoundaries(points,{'passed':True})['passed'])
        self.assertFalse(EvaluateBoundaries(points[:1],{'passed':True})['passed'])
if __name__=='__main__':unittest.main()
