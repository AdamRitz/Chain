"""Recompute native benchmark tables from saved evidence, without contacting nodes."""
import argparse
import csv
import json
from pathlib import Path


def Read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def Analyze(case):
    summary=Read(case/'summary.json')
    audit=Read(case/'state-audit.json')
    balances=Read(case/'balance-audit.json')
    proof=Read(case/'boundary-proof.json')
    points=[json.loads(line) for line in (case/'chain.jsonl').read_text(encoding='utf-8').splitlines()]
    a=next(p for p in points if p['target_seconds']==2)
    b=next(p for p in points if p['target_seconds']==12)
    elapsed=((b['bracket_begin_monotonic']+b['bracket_end_monotonic'])-
             (a['bracket_begin_monotonic']+a['bracket_end_monotonic']))/2
    assert proof['passed'] and len(proof['checks'])==20 and all(c['matches'] for c in proof['checks'])
    assert audit['passed'] and balances['passed'] and balances['mismatches']==0
    assert len(audit['nodes'])==10 and all(n['failed']==0 and n['canonical_prefix_matches'] for n in audit['nodes'])
    assert summary['cleanup_ok'] and summary['status']=='complete'
    counts_a=sorted(n['count'] for n in a['nodes'])
    counts_b=sorted(n['count'] for n in b['nodes'])
    tps=(counts_b[0]-counts_a[0])/elapsed
    assert abs(tps-summary['tps'])<1e-5
    nodes=audit['nodes']
    pool_errors=summary['callback_error_classes'].get('sdk:10002:TxPoolIsFull',0)
    return {
        'name':summary['name'],'target_tps':summary['target_tps'],
        'actual_sdk_attempts_per_s':summary['actual_send_tps'],
        'common_10_tps':tps,'common_at_least_7_tps':(counts_b[3]-counts_a[3])/elapsed,
        'tps_acquisition_low':summary['tps_acquisition_bounds'][0],
        'tps_acquisition_high':summary['tps_acquisition_bounds'][1],
        'measurement_seconds':elapsed,'common_delta':counts_b[0]-counts_a[0],
        'receipt_p50_ms':summary['receipt_p50_ms'],'receipt_p95_ms':summary['receipt_p95_ms'],
        'receipt_p99_ms':summary['receipt_p99_ms'],'receipt_success_samples':summary['receipt_latency_samples'],
        'all_node_samples':summary['all_node_samples'],'all_node_planned_samples':summary['planned_samples'],
        'all_node_after_send_p50_ms':summary['all_node_after_send_p50_ms'],
        'all_node_after_send_p99_ms':summary['all_node_after_send_p99_ms'],
        'submitted_total_12s':summary['submitted'],'callback_success_by_17s':summary['callback_success'],
        'pool_full_callbacks':pool_errors,'pool_full_fraction':pool_errors/summary['submitted'],
        'sdk_timeout_callbacks':summary['callback_error_classes'].get('sdk:-4008:waiting for message response timed out',0),
        'unresolved_callbacks':summary['unresolved'],'unsubmitted':summary['unsubmitted'],
        'canonical_success_transfers_at_stop':balances['confirmed_workload_transactions'],
        'business_receipts_rechecked':balances['additional_receipts_checked'],
        'all_10_success_transfers_at_stop':min(n['count'] for n in nodes)-summary['setup_transactions'],
        'terminal_height_min':min(n['height'] for n in nodes),'terminal_height_max':max(n['height'] for n in nodes),
        'terminal_lag_transactions':max(n['count'] for n in nodes)-min(n['count'] for n in nodes),
        'pending_mean_at_2s':a['pending_mean'],'pending_mean_at_12s':b['pending_mean'],
        'lagging_hosts':[n['host'] for n in nodes if n['height']<max(v['height'] for v in nodes)],
        'all_heads_equal':audit['all_heads_equal'],'boundary_checks':len(proof['checks']),
        'state_audit_passed':audit['passed'],'balance_accounts':balances['accounts'],'cleanup_ok':summary['cleanup_ok'],
        'query_error_count':len(summary.get('errors',[])),
        'native_attribute':summary['native_attribute']['attribute'],
        'native_endpoint':summary['native_attribute']['to'],
    }


def Main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch',type=Path);parser.add_argument('out',type=Path)
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    rows=[Analyze(args.batch/(args.batch.name+'-r'+str(rate))) for rate in [20000,30000,40000,50000,60000]]
    (args.out/'summary.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    with (args.out/'summary.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=rows[0].keys());writer.writeheader();writer.writerows(rows)
    print(json.dumps(rows,ensure_ascii=False,indent=2))


if __name__=='__main__':Main()
