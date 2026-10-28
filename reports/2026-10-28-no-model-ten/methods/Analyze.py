"""Summarize five fixed cases without promoting failed measurement windows."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import tarfile

ROOT=Path(__file__).resolve().parents[1]
def Read(name):return json.loads((ROOT/'data'/name).read_text(encoding='utf-8'))
def Save(name,value):
    (ROOT/'data'/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def EndpointEstimate(points):
    a=next((p for p in points if p['target_seconds']==2),None)
    b=next((p for p in points if p['target_seconds']==12),None)
    if not a or not b or not a.get('common') or not b.get('common'):return None
    a0,a1=a['common_observation_interval_seconds'];b0,b1=b['common_observation_interval_seconds']
    dt=(b0+b1-a0-a1)/2;dh=b['common']['height']-a['common']['height']
    tx=b['common']['transactions']-a['common']['transactions']
    if b0<=a1 or dh<=0 or tx<0:return None
    return dict(tps=tx/dt,seconds=dt,height_delta=dh,transaction_delta=tx,
        interval_ms=1000*dt/dh,interval_bounds_ms=[1000*(b0-a1)/dh,1000*(b1-a0)/dh],
        tps_bounds=[tx/(b1-a0),tx/(b0-a1)],endpoint_spans=[a1-a0,b1-b0])

def Main(archive):
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==Read('raw-archive.json')['sha256']
    rows=Read('results.json');assert [r['case']['rate'] for r in rows]==[20000,30000,40000,50000,60000]
    evidence=[];summary=[]
    with tarfile.open(archive) as raw:
        for row in rows:
            rate=row['case']['rate'];name=f'./baseline-{rate}-r1/'
            def Entry(filename):return json.load(raw.extractfile(name+filename))
            points=[json.loads(l) for l in raw.extractfile(name+'timeline.jsonl').read().decode().splitlines()]
            proof=Entry('persisted-boundary-proof.json')
            cleanup=[Entry(f'node{i:02}-database-cleanup.json') for i in range(1,11)]
            assert row['status']=='complete' and not row['cleanup_errors']
            assert all(c['removed'] for c in cleanup)
            audit=row['state_audit'];assert audit['passed'] and audit['all_heads_equal']
            assert len({n['state']['state_hash'] for n in audit['nodes']})==1
            endpoints=[{k:p.get(k) for k in ['target_seconds','common','common_observation_interval_seconds','prefix_rpc']} for p in points if p['target_seconds'] in [2,12]]
            evidence.append(dict(rate=rate,endpoints=endpoints,persisted_proof=proof,cleanup=cleanup,
                stop_records=Entry('stop-records.json'),latency_samples=Entry('latency-samples.json')))
            m=row['metrics'];estimate=EndpointEstimate(points)
            if row['audited_measurement']['passed']:
                assert abs(estimate['tps']-row['audited_measurement']['tps'])<1e-6
            summary.append(dict(rate=rate,send_tps=m['achieved_send_tps'],estimate=estimate,
                qualified=row['audited_measurement']['passed'],reasons=row['audited_measurement']['reasons'],
                persisted_anchor_proof=proof['passed'],p50_ms=m['p50_ms'],p95_ms=m['p95_ms'],p99_ms=m['p99_ms'],
                sampled=m['samples'],unresolved=m['unresolved_samples'],confirmed=audit['nodes'][0]['confirmed'],
                completed_all_ingress=m['sent_unique_completed_all_ingress'],attempted_upper=m['sent_unique_attempted_upper_bound']))
    Save('measurement-evidence.json',evidence)
    old=[]
    for row in Read('historical-baseline.json'):
        m=row['metrics'];old.append(dict(rate=row['case']['rate'],estimate=EndpointEstimate(row['endpoints']),
            observed_tps=m['common_observation_tps'],strict_head_proof=m['boundary_canonical_proof_passed'],
            p50_ms=m['p50_ms'],p95_ms=m['p95_ms'],p99_ms=m['p99_ms']))
    model=[]
    for group in ['N95','M75']:
        for rate in [20000,30000,40000,50000,60000]:
            subset=[r for r in Read('historical-model.json') if r['case']['group']==group and r['case']['rate']==rate]
            valid=[r for r in subset if r['metrics'].get('common_boundary_proof_passed') and EndpointEstimate(r['endpoints'])]
            model.append(dict(group=group,rate=rate,valid=len(valid),total=len(subset),
                median_tps=statistics.median([r['metrics']['proven_common_tps'] for r in valid]) if valid else None,
                median_interval_ms=statistics.median([EndpointEstimate(r['endpoints'])['interval_ms'] for r in valid]) if valid else None,
                median_p95_ms=statistics.median([r['metrics']['p95_ms'] for r in subset])))
    Save('summary.json',dict(current=summary,historical_baseline=old,historical_model=model,
        note='diagnostic estimates do not become qualified results when persisted proof or timing quality fails'))
    for cur,prev in zip(summary,old):
        n=cur['estimate'];g=next(m for m in model if m['group']=='M75' and m['rate']==cur['rate'])
        print(json.dumps(dict(rate=cur['rate'],qualified=cur['qualified'],new=n,old=prev,model=g),ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--archive',type=Path,required=True);Main(p.parse_args().archive)
