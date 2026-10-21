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
    groups={'A':('robust',0),'B':('robust',1),'C':('learned',0),'D':('learned',1)}

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

    def ReadTrace(node,run,output):
        path=Path(node['root'])/run/node['id']/'clock.jsonl'
        command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
                 '-o','UserKnownHostsFile='+str(args.known_hosts),'-i',node['ssh_key'],node['ssh'],
                 shlex.join(['cat',str(path)])]
        result=subprocess.run(command,capture_output=True,timeout=30,check=True)
        if len(result.stdout)>32*1024*1024:raise RuntimeError('Unexpected clock trace size')
        (output/(node['id']+'-clock.jsonl')).write_bytes(result.stdout)

    def GridMain(unused):
        args.work.mkdir(parents=True,exist_ok=False)
        Save(args.work/'plan.json',plan); Save(args.work/'groups.json',groups)
        Save(args.work/'inventory.json',inventory)
        dataset=args.work/'transactions.bin'
        E.Run([args.bin/'sender','--prepare',dataset,'--count',720000,'--wallets',1024],timeout=180)
        genesis=json.loads(Path(str(dataset)+'.genesis.json').read_text())
        dataset_hash=hashlib.sha256(dataset.read_bytes()).hexdigest()
        Save(args.work/'dataset.json',{'sha256':dataset_hash,'count':720000,'wallets':1024,'shared_across_all_cases':True})
        results=[]
        for rate_index,rate in enumerate([20000,30000,40000,50000,60000]):
            order=list(groups);order=order[rate_index%4:]+order[:rate_index%4]
            for group in order:
                control,join=groups[group]
                name=f'{group}-{rate}'
                output=args.work/name;output.mkdir()
                run=f'clock-{args.source_revision[:7]}-{group}-{rate}'
                nodes=[{**node,'binary':str(args.bin/'boost'),'worker':str(args.source/'Test/ClusterWorker.py'),
                        'options':{'clock-trace':str(Path(node['root'])/run/node['id']/'clock.jsonl')}} for node in inventory]
                settings={'io-threads':4,'verify-threads':8,'sync':1,'produce':1,'experiment-index':1,
                          'clock-sync':1,'sync-period-ms':250,'sync-gain':.25,'round-ms':50,'candidate-ms':12,
                          'round-scheduler':'bounded','join-current-round':join,'clock-control':control,
                          'clock-model':str(args.source/'Time/Models/simulation-v1.json'),'clock-min-peers':3,
                          'clock-model-period-ms':500,'clock-model-ttl-ms':2000,'clock-max-slew-ppm':1000,
                          'relay-transactions':0,'block-ms':50,'max-block-txs':6000,'run-seconds':90}
                case={'suite':'throughput','mode':'throughput','rate':rate,'nodes':10,'repeat':1,'pattern':'steady',
                      'group':group,'clock_control':control,'join_current':bool(join),'revision':args.source_revision}
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
                    if any(s['clock']['model_sha256']!=args.expected_model_sha256 for s in stats):raise RuntimeError('Model differs across nodes')
                    if any(s['clock']['control']!=control or s['clock']['join_current_round']!=bool(join) for s in stats):raise RuntimeError('Ablation flags differ from the case')
                    Save(output/'calibration.json',{'seconds':time.monotonic()-began,'nodes':stats,
                        'note':'Startup calibration is outside the fixed two-second traffic warmup'})
                    row['metrics']=E.LoadCase(cluster,case,plan,dataset,genesis,output)
                    if row['metrics'].get('checks'):
                        row['state_audit']={'method':'live fully drained shared dataset','passed':True,'checks':row['metrics']['checks']}
                    else:
                        row['state_audit']=PartialAudit(cluster,run,output,genesis,dataset,row['metrics']['sent_unique_attempted_upper_bound'])
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
                    **{key:metrics.get(key) for key in ['common_commit_tps','achieved_send_tps','p99_after_send_ms','drained','unresolved_samples']}}),flush=True)
                if cluster.cleanup_errors:raise RuntimeError('Stop and database cleanup must succeed before the next case')
        print('CLOCK_GRID_FINISHED',flush=True)

    E.Main=GridMain
    args.suite='throughput';args.local=None;args.keep_going=True;args.dry_run=False
    Observe.Main(args)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for field in ['source','bin','inventory','plan','work','known-hosts']:
        parser.add_argument('--'+field,type=Path,required=True)
    parser.add_argument('--source-revision',required=True)
    parser.add_argument('--expected-binary-sha256',required=True)
    parser.add_argument('--expected-model-sha256',required=True)
    Main(parser.parse_args())
