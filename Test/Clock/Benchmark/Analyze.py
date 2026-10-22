"""Summarize prospective runs without replacing failed endpoints or censored latency."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics


def Analyze(root):
    records=json.loads((root/'results.json').read_text())
    output=[]
    counters=['local_proposals','reorganizations','fork_reexecute_transactions','branch_append_transactions',
              'db_write_ms','db_writes','block_build_ms','block_verify_ms','candidate_execute_ms',
              'commit_execute_ms','candidate_lock_wait_ms','commit_lock_wait_ms','tx_queue_wait_ms']
    clock_counters=['rounds','joined_current_rounds','late_candidates','cancelled_candidates','stale_candidates',
                    'risk_deferrals','decision_predictions','decision_fallbacks','skipped_rounds','budget_skips']
    for record in records:
        case=record['case'];name=f"{case['group']}-{case['rate']}-r{case['repeat']}"
        directory=root/name;metrics=record.get('metrics',{})
        valid=(record['status']=='complete' and record.get('state_audit',{}).get('passed') and
               metrics.get('common_boundary_proof_passed') and metrics.get('proven_common_tps') is not None and
               not record.get('cleanup_errors') and not metrics.get('load_error'))
        row={'case':name,'group':case['group'],'target_tps':case['rate'],'eligible':bool(valid),
             'tps':metrics.get('proven_common_tps') if valid else None,
             'tps_interval':metrics.get('common_observation_tps_acquisition_interval_bounds'),
             'actual_send_tps':metrics.get('achieved_send_tps'),'measurement_seconds':metrics.get('measurement_seconds'),
             'latency_p50_ms':metrics.get('p50_ms'),'latency_p95_ms':metrics.get('p95_ms'),
             'latency_p99_after_send_ms':metrics.get('p99_after_send_ms'),
             'latency_samples':metrics.get('samples'),'latency_unresolved':metrics.get('unresolved_samples'),
             'state_audit_passed':record.get('state_audit',{}).get('passed'),
             'cleanup_errors':record.get('cleanup_errors',[]),'error':record.get('error')}
        path=directory/'timeline.jsonl'
        points=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        pair=[next((p for p in points if p['target_seconds']==t and all(p.get('nodes',[]))),None) for t in [2,12]]
        if all(pair):
            a,b=pair
            row['counters_all10']= {key:sum(y[key]-x[key] for x,y in zip(a['nodes'],b['nodes'])) for key in counters}
            row['clock_counters_all10']={key:sum(y['clock'][key]-x['clock'][key] for x,y in zip(a['nodes'],b['nodes'])) for key in clock_counters}
            cpu=[(y['process_cpu_seconds']-x['process_cpu_seconds'])/((y['clock']['raw_us']-x['clock']['raw_us'])/1e6)
                 for x,y in zip(a['nodes'],b['nodes'])]
            row['cpu_cores_per_node_median']=statistics.median(cpu)
            row['cpu_cores_per_node']=cpu
            writes=row['counters_all10']['db_writes']
            row['db_write_ms_per_call']=row['counters_all10']['db_write_ms']/writes if writes else None
            snapshots=[s['clock'] for p in points if 2<=p['target_seconds']<=12 for s in p.get('nodes',[]) if s]
            row['median_prepare_budget_ms']=statistics.median(s['selected_prepare_us']/1000 for s in snapshots)
            row['median_time_budget_ms']=statistics.median(s['selected_time_us']/1000 for s in snapshots)
        outcomes=Counter();wall=defaultdict(float);deferrals=0;coverage=[]
        for path in directory.glob('node*-clock.jsonl'):
            for line in path.read_text().splitlines():
                trace=json.loads(line)
                if trace['kind']=='candidate_defer':deferrals+=1
                if trace['kind']=='candidate':
                    outcomes[trace['outcome']]+=1;wall[trace['outcome']]+=trace['prepare_us']/1000
                    if trace.get('prepare_completed'):coverage.append(trace['prepare_us']<=trace['prepare_budget_us'])
        row['whole_run_diagnostic']={'scope':'includes warmup, measurement and drain; wall time is not CPU time',
            'outcomes':dict(outcomes),'prepare_wall_ms':dict(wall),'risk_deferrals':deferrals,
            'empirical_prepare_coverage':sum(coverage)/len(coverage) if coverage else None,
            'nonoffered_work_fraction':1-wall.get('offered',0)/sum(wall.values()) if sum(wall.values()) else None}
        output.append(row)
    eligible=[r for r in output if r['eligible']]
    return {'cases':output,'eligible_count':len(eligible),'total_count':len(output),
            'best_observed':max(eligible,key=lambda r:r['tps']) if eligible else None,
            'scope':'one short run per version/rate; observed maximum, not statistical superiority or finality'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--raw',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();summary=Analyze(args.raw)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for row in summary['cases']:
        print(row['case'],row['tps'],row['actual_send_tps'],row.get('cpu_cores_per_node_median'),row.get('clock_counters_all10'))
