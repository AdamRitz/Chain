"""Five short baseline cases. Audit stopped ledgers before guarded database cleanup."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import time
import Observe

REVISION='1504583cda142c8f50fcc6215864160ae469f333'
DATASET_HASH='101b045a90e95dca1ffab93f0dcac0e85486a9c03ba67cf9e06bc44fb61717a0'

def Save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def EvaluateBoundaries(points,proof):
    result=dict(passed=False,reasons=[])
    endpoints=[next((p for p in points if p['target_seconds']==target),None) for target in [2,12]]
    if not proof.get('passed'):result['reasons'].append('persisted canonical proof failed')
    if not all(p and p.get('common') for p in endpoints):
        result['reasons'].append('missing exact measurement endpoint');return result
    a,b=endpoints
    a0,a1=a['common_observation_interval_seconds'];b0,b1=b['common_observation_interval_seconds']
    spans=[a1-a0,b1-b0];height=b['common']['height']-a['common']['height']
    count=b['common']['transactions']-a['common']['transactions']
    if any(not 0<=v<=.5 for v in spans):result['reasons'].append('acquisition span exceeds 0.5s')
    if height<=0 or count<0 or b0<=a1:result['reasons'].append('nonpositive height/time or negative transaction progress')
    result.update(endpoint_spans=spans,height_delta=height,transaction_delta=count)
    if result['reasons']:return result
    dt=(b0+b1-a0-a1)/2
    result.update(passed=True,measurement_seconds=dt,tps=count/dt,
                  tps_bounds=[count/(b1-a0),count/(b0-a1)],
                  mean_common_block_interval_ms=1000*dt/height,
                  block_interval_bounds_ms=[1000*(b0-a1)/height,1000*(b1-a0)/height])
    return result

def Main(args):
    sys.path.insert(0,str(args.source/'Test'))
    import Experiments as E
    import Cluster as C
    _,inventory=C.ReadInventory(args.inventory)
    plan=json.loads(args.plan.read_text());E.ValidatePlan(plan)

    def Audit(cluster,run,output,genesis,dataset,maximum):
        routes={}
        with dataset.open('rb') as f:
            for _ in range(1024):
                tx=f.read(176);amount,nonce=struct.unpack('<QQ',tx[64:80]);assert nonce==1
                routes[tx[:32].hex()]=(tx[32:64].hex(),amount)
        keys=list(routes);fee=genesis.get('base_fee',0)
        initial={r['public_key']:r['balance'] for r in genesis['accounts']}
        points=[json.loads(line) for line in (output/'timeline.jsonl').read_text().splitlines()]
        anchors=[p['common'] for p in points if p['target_seconds'] in [2,12] and p.get('common')]
        def Start(index):
            node=cluster.nodes[index]
            Save(output/(node['id']+'-load-stop.json'),C.Worker(node,{'action':'collect','run':run}))
            return C.Worker(node,{'action':'start','run':run,'restart':True,'binary':node['binary'],'genesis':genesis,
                'arguments':['--port',node['port'],'--bind',node['host'],'--produce',0,'--sync',1,'--run-seconds',60]})
        cluster.All(Start)
        def Check(index):
            node=cluster.nodes[index];client=C.NodeClient(node)
            try:
                deadline=time.monotonic()+20
                while True:
                    try:before=client.Query(13);break
                    except (OSError,ValueError,ConnectionError):
                        if time.monotonic()>deadline:raise
                        time.sleep(.05)
                accounts={}
                for begin in range(0,1024,128):
                    batch=keys[begin:begin+128]
                    values=client.Query(37,b''.join(bytes.fromhex(k) for k in batch))
                    assert len(values)==len(batch) and all(v is not None for v in values)
                    accounts.update(zip(batch,values))
                expected=dict(initial);confirmed=0
                for position,key in enumerate(keys):
                    n=accounts[key]['nonce'];receiver,amount=routes[key]
                    assert 0<=n<=maximum//1024+int(position<maximum%1024)
                    expected[key]-=n*(amount+fee);expected[receiver]+=n*amount;confirmed+=n
                assert all(accounts[k]['balance']==expected[k] for k in keys)
                after=client.Query(13);state=client.Query(35)
                assert not after['failed'] and before['head']==after['head'] and confirmed==after['chain_transactions']
                assert sum(expected.values())==after['user_supply'] and confirmed*fee==after['user_burned']
                checks=[dict(anchor=a,matches=a['height']<=after['height'] and client.Query(29,struct.pack('<Q',a['height']))==a) for a in anchors]
                # Keep the persisted canonical height/count index for independent block-progress analysis.
                ledger=[client.Query(29,struct.pack('<Q',h)) for h in range(1,after['height']+1)]
                assert client.Query(13)['head']==after['head']
                Save(output/(node['id']+'-canonical-index.json'),ledger)
                return dict(node=node['id'],accounts_checked=1024,confirmed=confirmed,height=after['height'],
                            head=after['head'],state=state,passed=True,anchor_checks=checks)
            finally:client.Close()
        nodes=cluster.All(Check)
        proof=dict(passed=len(anchors)==2 and all(len(n['anchor_checks'])==2 and all(v['matches'] for v in n['anchor_checks']) for n in nodes),nodes=nodes)
        audit=dict(passed=all(n['passed'] for n in nodes),nodes=nodes,
                   all_heads_equal=len({n['head'] for n in nodes})==1,
                   method='stopped nodes reopened without production or peers; each balance reconstructed from dataset routes and nonce; endpoint hashes checked on persisted canonical chains',
                   measurement_or_drain_extended=False)
        Save(output/'persisted-state-audit.json',audit);Save(output/'persisted-boundary-proof.json',proof)
        return audit,EvaluateBoundaries(points,proof)

    def GridMain(unused):
        if args.dry_run:
            print(json.dumps(dict(revision=REVISION,cases=5,rates=plan['throughput_rates'],warm=2,seconds=10,drain=5,contacts_servers=False)));return
        args.work.mkdir(parents=True,exist_ok=False)
        dataset=args.dataset;assert dataset.stat().st_size==720000*176
        digest=hashlib.sha256(dataset.read_bytes()).hexdigest();assert digest==DATASET_HASH
        genesis=json.loads(Path(str(dataset)+'.genesis.json').read_text())
        Save(args.work/'dataset.json',dict(path=str(dataset),sha256=digest,count=720000,shared_with_9339054_comparison=True))
        Save(args.work/'protocol.json',dict(revision=REVISION,plan=plan,inventory=inventory,
            driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),observer_sha256=hashlib.sha256(Path(Observe.__file__).read_bytes()).hexdigest(),
            block_time_definition='observation midpoint elapsed divided by net canonical common-prefix height growth; not individual construction time or transaction finality latency'))
        rows=[]
        for rate in plan['throughput_rates']:
            name=f'baseline-{rate}-r1';output=args.work/name;output.mkdir()
            run=f'no-model-1504583-20261028-{rate}-r1'
            nodes=[{**n,'binary':str(args.bin/'boost'),'worker':str(args.source/'Test/ClusterWorker.py')} for n in inventory]
            settings={'io-threads':4,'verify-threads':8,'sync':1,'produce':1,'experiment-index':1,
                      'clock-sync':1,'sync-period-ms':100,'sync-gain':.25,'round-ms':50,'candidate-ms':12,
                      'relay-transactions':0,'block-ms':50,'max-block-txs':6000,'run-seconds':90}
            case=dict(suite='throughput',mode='throughput',rate=rate,nodes=10,repeat=1,pattern='steady',group='baseline',revision=REVISION)
            row=dict(case=case,status='failed',dataset_sha256=digest)
            cluster=C.Cluster(nodes,run,genesis,settings,output)
            print(json.dumps({'starting':name}),flush=True)
            try:
                cluster.Start();deadline=time.monotonic()+30
                while True:
                    stats=cluster.Stats()
                    if all(not s['failed'] and s['peers']>=9 for s in stats):break
                    if time.monotonic()>deadline:raise RuntimeError('Full graph not ready')
                    time.sleep(.2)
                Save(output/'readiness.json',stats);Save(output/'load-starts.json',cluster.starts)
                row['metrics']=E.LoadCase(cluster,case,plan,dataset,genesis,output)
                maximum=row['metrics']['sent_unique_attempted_upper_bound']
                row['state_audit'],row['audited_measurement']=Audit(cluster,run,output,genesis,dataset,maximum)
                row['status']='complete'
            except Exception as error:row['error']=repr(error)
            finally:cluster.Close()
            row['cleanup_errors']=cluster.cleanup_errors
            Save(output/'result.json',row);rows.append(row);Save(args.work/'results.json',rows)
            print(json.dumps(dict(finished=name,status=row['status'],measurement=row.get('audited_measurement'),error=row.get('error'))),flush=True)
            if row['status']!='complete' or cluster.cleanup_errors:raise RuntimeError('Case failed or cleanup incomplete')
    E.Main=GridMain
    args.suite='throughput';args.local=None;args.keep_going=True
    Observe.Main(args)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for field in ['source','bin','inventory','plan','work','known-hosts','dataset']:parser.add_argument('--'+field,type=Path,required=True)
    parser.add_argument('--source-revision',default=REVISION);parser.add_argument('--expected-binary-sha256',required=True)
    parser.add_argument('--dry-run',action='store_true');Main(parser.parse_args())
