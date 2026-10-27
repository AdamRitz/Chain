"""Aggregate three fixed repeats; retain failed, backpressured and missing observations."""
import argparse
import json
from pathlib import Path
import statistics
import sys

REPO=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(REPO/'Test/Fisco/Capacity'))
from Analyze import Analyze, ReadLines


def Slope(points):
    if len(points)<2:return None
    xs=[x for x,y in points];ys=[y for x,y in points]
    a=statistics.mean(xs);b=statistics.mean(ys)
    denominator=sum((x-a)**2 for x in xs)
    return sum((x-a)*(y-b) for x,y in points)/denominator if denominator else None


def Main(raw,output):
    result=Analyze(raw)
    formal=[r for r in result['cases'] if r['stage']=='formal']
    for row in formal:
        directory=raw/row['batch']/row['name'];plan=row['plan']
        timeline=[json.loads(line) for line in ReadLines(directory/'chain.jsonl')]
        by_time={p['target_seconds']:p for p in timeline}
        def Eligible(p):return p and 0<=p['bracket_end_monotonic']-p['bracket_begin_monotonic']<=.5
        def Midpoint(p):return (p['bracket_begin_monotonic']+p['bracket_end_monotonic'])/2
        windows=[]
        for begin in range(plan['warm'],plan['warm']+plan['seconds'],10):
            a,b=by_time.get(begin),by_time.get(begin+10)
            if not (Eligible(a) and Eligible(b)):
                windows.append(dict(begin=begin,eligible=False));continue
            windows.append(dict(begin=begin,eligible=True,tps=(b['count_min']-a['count_min'])/(Midpoint(b)-Midpoint(a)),
                                pending_begin=a['pending_mean'],pending_end=b['pending_mean']))
        row['ten_second_windows']=windows
        start=plan['warm'];end=start+plan['seconds']
        first,last=by_time.get(start),by_time.get(end)
        if Eligible(first) and Eligible(last):
            delta=sorted(p['count'] for p in last['nodes'])[3]-sorted(p['count'] for p in first['nodes'])[3]
            row['at_least_seven_progress_tps']=delta/(Midpoint(last)-Midpoint(first))
            row['seven_progress_caveat']='Auxiliary progress counter, not a separately observed PBFT commit certificate; does not replace all-ten main TPS.'
        sample=[p for p in timeline if start<=p['target_seconds']<=end and Eligible(p)]
        tail=[p for p in sample if p['target_seconds']>=end-30]
        row['pending_ols_slope_per_s']=Slope([(Midpoint(p),p['pending_mean']) for p in sample])
        row['pending_tail30_ols_slope_per_s']=Slope([(Midpoint(p),p['pending_mean']) for p in tail])
        row['pending_samples']=len(sample)
        row['pending_max']=max((p['pending_mean'] for p in sample),default=None)
        row['final_sample_pending']=max(timeline,key=lambda p:p['target_seconds'])['pending_mean']
        requested=plan['rate']*(plan['warm']+plan['seconds'])
        row['unsubmitted_fraction']=row['unsubmitted']/requested
        row['admission_ratio']=row['actual_send_tps']/plan['rate']
        prepared=json.loads((directory/'prepared.json').read_text())
        limits=[p['prepared']['signed_transaction_inspection']['blockLimit'] for p in prepared]
        row['signing_limit_min']=min(limits)
        row['signing_limit_above_terminal_height']=min(limits)>max(row['terminal_heights'])
        row['sustained_window_criteria']={
            'correct_and_eligible':row['eligible'] and row['all_heads_equal'] and row['signing_limit_above_terminal_height'],
            'actual_calls_at_least_99_9_percent_target':row['actual_send_tps']>=.999*plan['rate'],
            'confirmed_tps_at_least_99_percent_target':row.get('tps',0)>=.99*plan['rate'],
            'unsubmitted_at_most_0_1_percent':row['unsubmitted_fraction']<=.001,
            'zero_callback_errors_and_unresolved':not row['callback_errors'] and row['unresolved']==0,
            'queue_full_slope_at_most_0_1_percent_rate':row['pending_ols_slope_per_s'] is not None and row['pending_ols_slope_per_s']<=.001*plan['rate'],
            'queue_tail_slope_at_most_0_1_percent_rate':row['pending_tail30_ols_slope_per_s'] is not None and row['pending_tail30_ols_slope_per_s']<=.001*plan['rate'],
            'at_least_58_queue_samples':len(sample)>=58,
        }
        row['passes_sustained_window']=all(row['sustained_window_criteria'].values())
    groups=[]
    for rate in [20000,30000,40000,50000,60000]:
        rows=[r for r in formal if r['plan']['rate']==rate]
        group=dict(rate=rate,completed=len(rows),repeats=[r['plan']['repeat'] for r in rows],
                   complete_three=len(rows)==3 and {r['plan']['repeat'] for r in rows}=={1,2,3})
        for field in ['tps','at_least_seven_progress_tps','actual_send_tps','receipt_p95_ms','receipt_p99_ms','scheduled_p95_ms',
                      'pending_ols_slope_per_s','pending_tail30_ols_slope_per_s','unsubmitted_fraction','cpu_node_mean','cpu_sender_mean','host_idle_mean']:
            values=[r[field] for r in rows if r.get(field) is not None]
            group[field]=dict(median=statistics.median(values),min=min(values),max=max(values)) if values else None
        group['all_repeats_pass_window']=group['complete_three'] and all(r['passes_sustained_window'] for r in rows)
        group['all_correct']=group['complete_three'] and all(r['eligible'] and r['all_heads_equal'] for r in rows)
        groups.append(group)
    result['groups']=groups
    result['highest_passing_tested_target']=max((g['rate'] for g in groups if g['all_repeats_pass_window']),default=None)
    result['finite_window_caveat']='These are declared tolerances for 60-second repeats, not proof of infinite-duration stability or a precise maximum between tested rates.'
    result.pop('best_formal',None)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for g in groups:print(json.dumps(g,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--raw',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();Main(args.raw,args.output)
