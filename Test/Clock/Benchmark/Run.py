"""Ten physical nodes, fixed shared data, four clock/scheduler ablations, short cases."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import struct
import sys
import time
import Observe


def Save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def Main(args):
    sys.path.insert(0,str(args.source/'Test'))
    import Experiments as E
    import Cluster as C
    _,inventory=C.ReadInventory(args.inventory)
    plan=json.loads(args.plan.read_text())
    plan['sync_period_ms']=250  # Current application default, identical in all groups.
    E.ValidatePlan(plan)
    groups={'N95':('robust',.95,0,0,'preparation-v1.json'), 'M75':('learned',.75,0,0,'preparation-v1.json'),
            'A':('learned',.95,1,0,'preparation-v2.json'), 'B':('robust',.95,0,1,'preparation-v1.json'),
            'AB':('learned',.95,1,1,'preparation-v2.json')}

    def PartialAudit(cluster,run,output,genesis,dataset,maximum):
        # Nodes have already stopped at the fixed 17s guard. Reopen the same database
        # without production or peers; no further transactions can advance during audit.
        routes={}
        with dataset.open('rb') as source:
            for _ in range(1024):
                tx=source.read(176);sender,receiver=tx[:32].hex(),tx[32:64].hex()
                amount,nonce=struct.unpack('<QQ',tx[64:80]);assert nonce==1
                routes[sender]=(receiver,amount)
        keys=list(routes);fee=genesis.get('base_fee',0)
        initial={row['public_key']:row['balance'] for row in genesis['accounts']}
        def Start(index):
            node=cluster.nodes[index]
            Save(output/(node['id']+'-load-stop.json'),C.Worker(node,{'action':'collect','run':run}))
            return C.Worker(node,{'action':'start','run':run,'restart':True,'binary':node['binary'],'genesis':genesis,
                'arguments':['--port',node['port'],'--bind',node['host'],'--produce',0,'--sync',1,'--run-seconds',30]})
        cluster.All(Start)
        def Verify(index):
            node=cluster.nodes[index];client=C.NodeClient(node)
            try:
                deadline=time.monotonic()+15
                while True:
                    try:before=client.Query(13);break
                    except (OSError,ValueError,ConnectionError):
                        if time.monotonic()>deadline:raise
                        time.sleep(.05)
                accounts={}
                for begin in range(0,len(keys),128):
                    batch=keys[begin:begin+128]
                    values=client.Query(37,b''.join(bytes.fromhex(key) for key in batch))
                    if len(values)!=len(batch) or any(value is None for value in values):raise AssertionError('Missing funded audit account')
                    accounts.update(zip(batch,values))
                expected=dict(initial);confirmed=0
                for position,key in enumerate(keys):
                    value=accounts[key]
                    nonce=value['nonce'];receiver,amount=routes[key]
                    if not 0<=nonce<=maximum//1024+int(position<maximum%1024):raise AssertionError('Nonce exceeds the attempted workload')
                    expected[key]-=nonce*(amount+fee);expected[receiver]+=nonce*amount;confirmed+=nonce
                if any(accounts[key]['balance']!=expected[key] for key in keys):raise AssertionError('Partial native balance mismatch')
                after=client.Query(13)
                if before['head']!=after['head'] or confirmed!=after['chain_transactions']:raise AssertionError('Audit chain changed or nonce count differs')
                if sum(expected.values())!=after['user_supply'] or confirmed*fee!=after['user_burned']:raise AssertionError('Partial supply or fee mismatch')
                return {'node':node['id'],'accounts_checked':len(keys),'confirmed':confirmed,'head':after['head'],'passed':True}
            finally:client.Close()
        result=cluster.All(Verify)
        Save(output/'partial-state-audit.json',result)
        return {'method':'isolated persisted state, balances reconstructed from each sender nonce and fixed dataset routes',
                'passed':True,'nodes':result,'measurement_or_drain_extended':False}

    def OfflineProof(cluster,output,metrics):
        # Traffic and all production have stopped. Reopened nodes have no peers.
        # This is evidence validation only; it never extends the measured window.
        points=[json.loads(line) for line in (output/'timeline.jsonl').read_text().splitlines()]
        pair=[next((p for p in points if p['target_seconds']==t and p.get('common')),None) for t in [2,12]]
        proof={'passed':False,'checks':[],'method':'two captured common anchors rechecked on all ten stopped persistent chains'}
        if all(pair):
            try:
                for point in pair:
                    anchor=point['common']
                    def Check(i):
                        client=C.NodeClient(cluster.nodes[i])
                        try:return client.Query(29,struct.pack('<Q',anchor['height']))==anchor
                        finally:client.Close()
                    matches=cluster.All(Check)
                    proof['checks'].append({'target':point['target_seconds'],'anchor':anchor,'matches':matches})
                a0,a1=pair[0]['common_observation_interval_seconds'];b0,b1=pair[1]['common_observation_interval_seconds']
                count=pair[1]['common']['transactions']-pair[0]['common']['transactions']
                proof['acquisition_intervals']=[[a0,a1],[b0,b1]]
                proof['passed']=all(all(c['matches']) for c in proof['checks']) and count>=0 and b0>a1 and max(a1-a0,b1-b0)<=.5
                if proof['passed']:
                    metrics['proven_common_tps']=count/((b0+b1-a0-a1)/2)
                    metrics['common_observation_tps_acquisition_interval_bounds']=[count/(b1-a0),count/(b0-a1)]
                    metrics['measurement_seconds']=(b0+b1-a0-a1)/2
            except Exception as error:proof['error']=str(error)
        metrics['common_boundary_proof_passed']=proof['passed']
        if not proof['passed']:metrics['proven_common_tps']=None
        Save(output/'offline-common-proof.json',proof)

    def ReadTrace(node,run,output):
        path=Path(node['root'])/run/node['id']/'clock.jsonl'
        command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
                 '-o','UserKnownHostsFile='+str(args.known_hosts),'-i',node['ssh_key'],node['ssh'],
                 shlex.join(['cat',str(path)])]
        result=subprocess.run(command,capture_output=True,timeout=30,check=True)
        if len(result.stdout)>32*1024*1024:raise RuntimeError('Unexpected clock trace size')
        (output/(node['id']+'-clock.jsonl')).write_bytes(result.stdout)

    def GridMain(unused):
        if args.dry_run:
            print(json.dumps({'groups':groups,'cycles':args.cycles,'rates':args.rates,
                'cases':len(groups)*len(args.rates)*args.cycles,'warmup':2,'measurement':10,'drain':5,'contacts_servers':False}))
            return
        args.work.mkdir(parents=True,exist_ok=False)
        Save(args.work/'plan.json',plan); Save(args.work/'groups.json',groups)
        Save(args.work/'driver.json',{'application_revision':args.source_revision,'phase':args.phase,'cycles':args.cycles,
            'rates':args.rates,'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'observer_sha256':hashlib.sha256(Path(Observe.__file__).read_bytes()).hexdigest()})
        Save(args.work/'inventory.json',inventory)
        dataset=args.work/'transactions.bin'
        E.Run([args.bin/'sender','--prepare',dataset,'--count',720000,'--wallets',1024],timeout=180)
        genesis=json.loads(Path(str(dataset)+'.genesis.json').read_text())
        dataset_hash=hashlib.sha256(dataset.read_bytes()).hexdigest()
        Save(args.work/'dataset.json',{'sha256':dataset_hash,'count':720000,'wallets':1024,'shared_across_all_cases':True})
        results=[]
        for rate_index,(repeat,rate) in enumerate((repeat,rate) for repeat in range(1,args.cycles+1) for rate in args.rates):
            order=list(groups);order=order[rate_index%len(groups):]+order[:rate_index%len(groups)]
            for group in order:
                control,quantile,timing,risk,model=groups[group];join=1
                name=f'{group}-{rate}-r{repeat}'
                output=args.work/name;output.mkdir()
                run=f'clock-{args.phase}-{args.source_revision[:7]}-{group}-{rate}-r{repeat}'
                nodes=[{**node,'binary':str(args.bin/'boost'),'worker':str(args.source/'Test/ClusterWorker.py'),
                        'options':{'clock-trace':str(Path(node['root'])/run/node['id']/'clock.jsonl')}} for node in inventory]
                settings={'io-threads':4,'verify-threads':8,'query-threads':2,'sync':1,'produce':1,'experiment-index':1,
                          'clock-sync':1,'sync-period-ms':250,'sync-gain':.25,'round-ms':50,'candidate-ms':12,
                          'candidate-timing':timing,'candidate-risk':risk,'round-scheduler':'bounded','join-current-round':join,'clock-control':control,
                          'clock-model':str(args.source/'Time/Models'/model),'clock-prepare-quantile':quantile,'clock-min-peers':3,
                          'clock-model-period-ms':500,'clock-model-ttl-ms':2000,'clock-max-slew-ppm':1000,
                          'relay-transactions':0,'block-ms':50,'max-block-txs':6000,'run-seconds':90}
                case={'suite':'throughput','mode':'throughput','rate':rate,'nodes':10,'repeat':repeat,'pattern':'steady',
                      'group':group,'clock_control':control,'join_current':bool(join),'prepare_quantile':quantile,'revision':args.source_revision,'phase':args.phase,'candidate_timing':timing,'candidate_risk':risk,'model':model}
                row={'case':case,'status':'failed','dataset_sha256':dataset_hash,'started_utc':time.time()}
                Save(output/'case.json',case)
                cluster=C.Cluster(nodes,run,genesis,settings,output)
                print(json.dumps({'starting':name}),flush=True)
                try:
                    cluster.Start()
                    began=time.monotonic();deadline=began+30
                    while True:
                        stats=cluster.Stats()
                        if all(s['peers']>=9 and s['clock']['fresh_sources']>=3 and s['clock']['model_loaded'] for s in stats):break
                        if time.monotonic()>deadline:raise RuntimeError('Full graph and fresh clock sources did not become ready')
                        time.sleep(.2)
                    if any(s['clock']['model_sha256']!=hashlib.sha256((args.source/'Time/Models'/model).read_bytes()).hexdigest() for s in stats):raise RuntimeError('Model differs across nodes')
                    if any(s['clock']['control']!=control or s['clock']['join_current_round']!=bool(join) or s['clock']['candidate_timing']!=bool(timing) or s['clock']['candidate_risk']!=bool(risk) for s in stats):raise RuntimeError('Ablation flags differ from the case')
                    Save(output/'calibration.json',{'seconds':time.monotonic()-began,'nodes':stats,
                        'note':'Startup calibration is outside the fixed two-second traffic warmup'})
                    row['metrics']=E.LoadCase(cluster,case,plan,dataset,genesis,output)
                    row['state_audit']=PartialAudit(cluster,run,output,genesis,dataset,row['metrics']['sent_unique_attempted_upper_bound'])
                    OfflineProof(cluster,output,row['metrics'])
                    row['status']='complete'
                except Exception as error:
                    row['error']=str(error)
                finally:
                    cluster.Close()
                if cluster.cleanup_errors:
                    row['cleanup_errors']=cluster.cleanup_errors
                try:
                    with concurrent.futures.ThreadPoolExecutor(10) as pool:
                        list(pool.map(lambda node:ReadTrace(node,run,output),nodes))
                except Exception as error:row['trace_error']=str(error)
                row['finished_utc']=time.time()
                Save(output/'result.json',row);results.append(row);Save(args.work/'results.json',results)
                metrics=row.get('metrics',{})
                print(json.dumps({'finished':name,'status':row['status'],'error':row.get('error'),
                    **{key:metrics.get(key) for key in ['proven_common_tps','common_commit_tps','achieved_send_tps','p99_after_send_ms','drained','unresolved_samples']}}),flush=True)
                if cluster.cleanup_errors:raise RuntimeError('Stop and database cleanup must succeed before the next case')
        print('CLOCK_GRID_FINISHED',flush=True)

    E.Main=GridMain
    args.suite='throughput';args.local=None;args.keep_going=True
    Observe.Main(args)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for field in ['source','bin','inventory','plan','work','known-hosts']:
        parser.add_argument('--'+field,type=Path,required=True)
    parser.add_argument('--cycles',type=int,choices=[1,2,3],default=1)
    parser.add_argument('--rates',type=int,nargs='+',choices=[20000,30000,40000,50000,60000],default=[20000,30000,40000,50000,60000])
    parser.add_argument('--phase',choices=['grid','endpoint'],default='grid')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--source-revision',required=True)
    parser.add_argument('--expected-binary-sha256',required=True)
    parser.add_argument('--expected-model-sha256',required=True)
    Main(parser.parse_args())
