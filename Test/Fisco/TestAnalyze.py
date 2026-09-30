"""Accounting regression checks with analytically known synthetic observations."""
import json,tempfile,unittest
from pathlib import Path
from Analyze import Analyze

class Accounting(unittest.TestCase):
    def Case(self,root,slope=0,mismatch=0,end=70000,nodes=10):
        folder=root/'results/stable-unit';folder.mkdir(parents=True)
        driver=dict(workload='solidity',target_tps=100,warmup_s=10,measure_s=60,
                    start_epoch_ms=0,end_epoch_ms=end,users=20,success=7000,
                    setup_transactions=30,actual_send_tps=100,failures=0,unresolved=0,
                    balances_checked=20,balance_mismatches=mismatch)
        (folder/'driver.json').write_text(json.dumps(driver))
        samples=[{'nodes':[dict(end_epoch=t,transactionCount=30+100*t,pending=slope*t,blockNumber=t) for _ in range(nodes)]}
                 for t in range(71)]
        (folder/'chain.jsonl').write_text('\n'.join(map(json.dumps,samples)))
        before={'nodes':[dict(transactionCount=0,failedTransactionCount=0) for _ in range(nodes)]}
        after={'valid':True,'nodes':[dict(transactionCount=7030,failedTransactionCount=0,pending=0) for _ in range(nodes)]}
        for name,data in [('before',before),('after',after)]:
            (folder/(name+'.json')).write_text(json.dumps(data))
        return Analyze(root)[0]

    def test_ten_deployments_counted(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.Case(Path(d));self.assertEqual(r['common_chain_tps'],100)
            self.assertEqual(r['expected_count_delta'],7030);self.assertTrue(r['sustainable'])

    def test_single_node_and_actual_block_size(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.Case(Path(d),nodes=1)
            self.assertEqual(r['common_chain_tps'],100);self.assertEqual(r['node_count'],1)
            self.assertEqual(r['mean_transactions_per_block'],100)
            self.assertEqual(r['mean_block_interval_ms'],1000)
            self.assertTrue(r['sustainable'])

    def test_backlog_rejects_sustainable_label(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.Case(Path(d),slope=20);self.assertEqual(r['pending_slope_per_node'],20)
            self.assertFalse(r['sustainable'])

    def test_balance_mismatch_rejects_sustainable_label(self):
        with tempfile.TemporaryDirectory() as d:self.assertFalse(self.Case(Path(d),mismatch=1)['sustainable'])

    def test_failed_final_query_retains_throughput_without_acceptance(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.Case(root)
            (root/'results/stable-unit/after.json').write_text(json.dumps({'valid':False,'nodes':[],'error':'unavailable'}))
            r=Analyze(root)[0]
            self.assertEqual(r['common_chain_tps'],100)
            self.assertFalse(r['state_valid']);self.assertFalse(r['sustainable'])

    def test_common_window_ends_at_earliest_driver(self):
        with tempfile.TemporaryDirectory() as d:
            r=self.Case(Path(d),end=65000);self.assertEqual(r['common_chain_window_s'],55)
            self.assertEqual(r['common_chain_tps'],100)

    def test_recovered_queue_burst_has_no_net_accumulation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.Case(root);p=root/'results/stable-unit/chain.jsonl';rows=[]
            for t in range(71):
                pending=0 if t<30 else (50*(t-30) if t<=60 else 150*(70-t))
                rows.append({'nodes':[dict(end_epoch=t,transactionCount=30+100*t-pending,pending=pending) for _ in range(10)]})
            p.write_text('\n'.join(map(json.dumps,rows)));r=Analyze(root)[0]
            self.assertEqual(r['common_chain_tps'],100);self.assertEqual(r['pending_net_growth_per_node'],0)
            self.assertGreater(r['pending_slope_per_node'],10);self.assertTrue(r['sustainable'])

if __name__=='__main__':unittest.main()
