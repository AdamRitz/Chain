"""Summarize captured RPC intervals and model telemetry without rerunning a workload."""
import argparse
import collections
import csv
import json
from pathlib import Path
import statistics


def Main(root,model_path):
    results=json.loads((root/'results.json').read_text())
    model=json.loads(model_path.read_text())
    summary=[]
    for result in results:
        case=result['case'];directory=root/f'{case["group"]}-{case["rate"]}'
        metrics=result.get('metrics',{})
        timeline=directory/'timeline.jsonl'
        points=[json.loads(line) for line in timeline.read_text().splitlines()] if timeline.exists() else []
        measured=[point for point in points if 2<=point['target_seconds']<=12 and all(point.get('nodes',[]))]
        common_points=[point for point in measured if point.get('common')]
        proof_path=directory/'boundary-canonical-proof.json'
        proof=json.loads(proof_path.read_text()) if proof_path.exists() else {}
        canonical_common_tps=None;common_bounds=None;common_anchors=[]
        if len(common_points)>=2:
            pair=[common_points[0],common_points[-1]]
            for point in pair:
                anchor=point['common'];supported=False
                for check in proof.get('checks',[]):
                    if check['target']!=point['target_seconds'] or not check['matches_final_chain']:continue
                    rpc=next((r for r in point['stats_rpc'] if r['node']==check['node']),None)
                    if rpc and rpc['value']['height']==anchor['height'] and rpc['value']['head']==anchor['hash'] and rpc['value']['chain_transactions']==anchor['transactions']:
                        supported=True;break
                common_anchors.append({'target':point['target_seconds'],'height':anchor['height'],'transactions':anchor['transactions'],'supported':supported})
            if all(row['supported'] for row in common_anchors):
                a,b=pair;count=b['common']['transactions']-a['common']['transactions']
                a0,a1=a['common_observation_interval_seconds'];b0,b1=b['common_observation_interval_seconds']
                elapsed=(b0+b1-a0-a1)/2
                if elapsed>0:canonical_common_tps=count/elapsed
                if count>=0 and b0>a1:common_bounds=[count/(b1-a0),count/(b0-a1)]
        counters=collections.Counter();predictions=collections.Counter();domains=collections.Counter()
        inference_max=[];active=[]
        phase={}
        if len(measured)>=2:
            elapsed=measured[-1]['snapshot_seconds']-measured[0]['snapshot_seconds']
            totals=collections.Counter()
            for before,after in zip(measured[0]['nodes'],measured[-1]['nodes']):
                for key in ['model_runs','model_fallbacks','rounds','joined_current_rounds','skipped_rounds','late_candidates','stale_candidates','cancelled_candidates']:
                    counters[key]+=after['clock'][key]-before['clock'][key]
                for key in ['fork_reexecute_transactions','reorganizations','local_proposals','db_write_ms','db_writes']:
                    totals[key]+=after.get(key,0)-before.get(key,0)
            if elapsed>0:
                phase.update({key+'_per_node_second':totals[key]/10/elapsed for key in ['fork_reexecute_transactions','reorganizations','local_proposals']})
            phase['mean_db_write_ms']=totals['db_write_ms']/totals['db_writes'] if totals['db_writes'] else None
            for point in measured:
                for node in point['nodes']:
                    clock=node['clock'];inference_max.append(clock['max_inference_us'])
                    active.append(clock['control']=='learned' and clock.get('prediction_fresh',False) and clock['fallback']=='ok')
            phase['prepare_budget_ms_median']=statistics.median(node['clock']['prepare_budget_us']/1000 for point in measured for node in point['nodes'])
            phase['time_uncertainty_ms_median']=statistics.median(node['clock']['empirical_uncertainty_us']/1000 for point in measured for node in point['nodes'])
        attempts=offers=duplicates=invalid_deadlines=0
        for path in directory.glob('node*-clock.jsonl'):
            seen=set()
            for line in path.read_text().splitlines():
                row=json.loads(line)
                if row['kind']=='candidate':
                    attempts+=1
                    duplicates+=row['round'] in seen;seen.add(row['round'])
                    if row['outcome']=='offered':
                        offers+=1
                        invalid_deadlines+=not row['start_us']<=row['offered_us']<row['proposal_deadline_us']
                elif row['kind']=='observation':
                    predictions['all']+=1;predictions['valid']+=row['prediction_valid']
                    predictions[row['reason']]+=1
                    for i,value in enumerate(row['features']):
                        if not model['minimum'][i]<=value<=model['maximum'][i]:domains[model['features'][i]]+=1
        row={'group':case['group'],'offered_tps':case['rate'],'status':result['status'],
             'state_audit_passed':bool(result.get('state_audit',{}).get('passed')),
             'canonical_proof':metrics.get('boundary_canonical_proof_passed',False),
             'common_commit_tps':metrics.get('common_commit_tps'),'achieved_send_tps':metrics.get('achieved_send_tps'),
             'canonical_common_tps':canonical_common_tps,'canonical_common_tps_bounds':common_bounds,'common_anchor_checks':common_anchors,
             'common_observation_tps':metrics.get('common_observation_tps'),
             'p50_ms':metrics.get('p50_ms'),'p95_ms':metrics.get('p95_ms'),'p99_ms':metrics.get('p99_ms'),
             'p99_after_send_ms':metrics.get('p99_after_send_ms'),'samples':metrics.get('samples'),
             'unresolved_samples':metrics.get('unresolved_samples'),'drained':metrics.get('drained'),
             'measurement_seconds':metrics.get('measurement_seconds'),
             'model_valid_fraction':(1-counters['model_fallbacks']/counters['model_runs']) if counters['model_runs'] else None,
             'learned_active_observation_fraction':statistics.mean(active) if active and case['clock_control']=='learned' else None,
             'observed_max_inference_us':max(inference_max,default=None),
             'cpu_core_equivalents_mean':statistics.mean([r['cpu_core_equivalents'] for r in metrics.get('resources',[]) if r['cpu_core_equivalents'] is not None]) if metrics.get('resources') else None,
             'round_counters_window_sum':dict(counters),'trace_attempts':attempts,'trace_offers':offers,
             'phase':phase,
             'duplicate_rounds':duplicates,'offered_outside_deadline':invalid_deadlines,
             'whole_trace_predictions':dict(predictions),'whole_trace_out_of_domain':dict(domains),
             'error':result.get('error'),'cleanup_errors':result.get('cleanup_errors'),'trace_error':result.get('trace_error')}
        summary.append(row)
    deltas=[]
    for rate in [20000,30000,40000,50000,60000]:
        rows={r['group']:r for r in summary if r['offered_tps']==rate}
        delta={'rate':rate}
        for target,base,label in [('B','A','scheduler'),('C','A','model_future_only'),('D','B','model_current_enabled'),('D','A','combined')]:
            a,b=rows.get(base,{}).get('canonical_common_tps'),rows.get(target,{}).get('canonical_common_tps')
            delta[label+'_percent']=(b/a-1)*100 if a and b is not None else None
        deltas.append(delta)
    data={'cases':summary,'deltas':deltas,'case_count':len(summary),
          'state_audits_passed':sum(row['state_audit_passed'] for row in summary),
          'canonical_proofs_passed':sum(row['canonical_proof'] for row in summary),
          'common_anchor_proofs_passed':sum(row['canonical_common_tps'] is not None for row in summary),
          'dataset_hashes':sorted({row['dataset_sha256'] for row in results}),
          'scope':'one 10s measurement per group and rate; no injected faults; latency is common-position observation, not finality'}
    (root/'analysis.json').write_text(json.dumps(data,indent=2)+'\n')
    columns=['group','offered_tps','status','common_commit_tps','canonical_common_tps','achieved_send_tps','p50_ms','p95_ms','p99_after_send_ms','unresolved_samples','drained','state_audit_passed','model_valid_fraction','cpu_core_equivalents_mean']
    with (root/'summary.csv').open('w',newline='') as output:
        writer=csv.DictWriter(output,fieldnames=columns);writer.writeheader()
        for row in summary:writer.writerow({key:row[key] for key in columns})
    print(json.dumps({'cases':len(summary),'state_audits':data['state_audits_passed'],'canonical_proofs':data['canonical_proofs_passed'],
                      'duplicates':sum(row['duplicate_rounds'] for row in summary),'late_offers':sum(row['offered_outside_deadline'] for row in summary)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--model',type=Path,required=True);args=parser.parse_args();Main(args.work,args.model)
