"""Compile once, distribute to the private cluster, compare identical native loads."""
import concurrent.futures,getpass,hashlib,json,shlex,sys,tarfile,threading,time
from pathlib import Path
import paramiko
ROOT=Path('/home/ubuntu/chain'); NEW=ROOT/'releases/2026-10-14-tps-v2'; OLD=ROOT/'releases/2026-10-13-throughput'
HOSTS=['10.206.0.16','10.206.0.4','10.206.0.14','10.206.0.17','10.206.0.15','10.206.0.12','10.206.0.2','10.206.0.6','10.206.0.11','10.206.0.8']
CLIENTS={}; LOCKS={}; WORK=ROOT/'results/throughput-20261014-round'
def Remote(host,command,input_data=None,timeout=180):
    with LOCKS[host]:
        stdin,out,err=CLIENTS[host].exec_command(command,timeout=timeout)
        if input_data is not None:stdin.write(input_data);stdin.flush()
        stdin.channel.shutdown_write(); text=out.read().decode(); error=err.read().decode(); code=out.channel.recv_exit_status()
        if code:raise RuntimeError(f'{host} exit {code}: {text[-1200:]} {error[-1200:]}')
        return text
def Worker(node,request):
    request={**request,'root':node['root'],'node':node['id']}
    response=json.loads(Remote(node['host'],shlex.join([node['python'],node['worker']]),json.dumps(request)))
    if not response.get('ok'):raise RuntimeError(response.get('error','worker failed'))
    return response['result']
def Deploy(host):
    free=float(Remote(host,"python3 -c 'import shutil;print(shutil.disk_usage(\"/home/ubuntu\").free/2**30)'"))
    if free<2:raise RuntimeError(f'{host}: need 2 GiB scratch space, have {free}')
    if Remote(host,"ss -H -lnt 'sport = :8089'").strip():raise RuntimeError(host+': port in use')
    if host!='10.206.0.2' and not Remote(host,f'test -x {NEW}/build/boost && echo present').strip():
        Remote(host,f'mkdir -p {NEW}')
        with CLIENTS[host].open_sftp() as sftp:sftp.put(str(ROOT/'throughput-runtime-20261014-v2.tar.gz'),str(ROOT/'throughput-runtime-20261014-v2.tar.gz'))
        Remote(host,f'tar -xzf {ROOT}/throughput-runtime-20261014-v2.tar.gz -C {NEW}')
    dependencies=Remote(host,f'ldd {NEW}/build/boost')
    if 'not found' in dependencies:raise RuntimeError(host+': missing dependencies')
    digest=Remote(host,f'sha256sum {NEW}/build/boost').split()[0]
    baseline=Remote(host,f'sha256sum {OLD}/build/boost').split()[0]
    print(json.dumps({'deployed':host,'sha256':digest,'free_gib':free}),flush=True)
    return {'host':host,'new_sha256':digest,'old_sha256':baseline,'free_gib':free}
def Main():
    password=getpass.getpass('CLUSTER_PASSWORD: ') if sys.stdin.isatty() else sys.stdin.readline().rstrip()
    try:
        for host in HOSTS:
            client=paramiko.SSHClient();client.load_system_host_keys();client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(host,username='ubuntu',password=password,allow_agent=False,look_for_keys=False,timeout=12)
            client.get_transport().set_keepalive(15);CLIENTS[host]=client;LOCKS[host]=threading.Lock()
        password=None
        WORK.mkdir(parents=True,exist_ok=False)
        with tarfile.open(ROOT/'throughput-runtime-20261014-v2.tar.gz','w:gz') as archive:
            for relative in ['Test','Contracts','Experiments','config.yaml','validation.json','build/boost','build/sender','build/evm_sender']:
                archive.add(NEW/relative,arcname=relative,filter=lambda info:None if '__pycache__' in info.name else info)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:machines=list(pool.map(Deploy,HOSTS))
        if len({row['new_sha256'] for row in machines})!=1 or len({row['old_sha256'] for row in machines})!=1:raise RuntimeError('Mixed node builds')
        (WORK/'deployment.json').write_text(json.dumps(machines,indent=2))
        sys.path.insert(0,str(NEW/'Test')); import Cluster,Experiments
        Cluster.Worker=Worker
        plan=json.loads((NEW/'Experiments/plan.json').read_text());plan.update({'repeats':3,'warmup':5,'duration':30,
                'drain_seconds':20,'poll_seconds':.05,'sample_hz':20,'max_backlog_growth':10,'batch':128,'ingress':'broadcast',
                'clock_sync':True,'round_ms':50,'candidate_ms':12,'throughput_rates':[50000,60000]})
        (WORK/'plan.json').write_text(json.dumps(plan,indent=2))
        dataset=WORK/'transactions.bin'
        dataset.hardlink_to(ROOT/'results/throughput-20261014-next/transactions.bin')
        Path(str(dataset)+'.genesis.json').hardlink_to(ROOT/'results/throughput-20261014-next/transactions.bin.genesis.json')
        genesis=json.loads(Path(str(dataset)+'.genesis.json').read_text())
        (WORK/'dataset-sha256.txt').write_text(hashlib.sha256(dataset.read_bytes()).hexdigest())
        rows=[]
        for rate in [50000,60000]:
          for repeat in range(1,4):
            order=[('new',NEW)]
            for version,release in order:
                name=f'{version}-{rate}-r{repeat}'; output=WORK/name;output.mkdir();run='throughput20261014-round-'+name
                defaults={'transport':'ssh','port':8089,'binary':str(release/'build/boost'),
                          'worker':str(NEW/'Test/ClusterWorker.py'),'python':str(ROOT/'releases/2026-09-30-d4106d1/.venv/bin/python'),'root':str(ROOT/'throughput-scratch-20261014')}
                nodes=[{**defaults,'id':f'node{i+1:02}','host':host,'ssh':'ubuntu@'+host} for i,host in enumerate(HOSTS)]
                settings={'io-threads':4,'verify-threads':8,'sync':1,'produce':1,'experiment-index':1,'clock-sync':1,
                          'round-ms':50,'candidate-ms':12,'relay-transactions':0,'run-seconds':240,'block-ms':50,'max-block-txs':6000}
                case={'suite':'throughput','mode':'throughput','rate':rate,'repeat':repeat,'nodes':10,'version':version,'round_ms':50,'candidate_ms':12,'clock_sync':True}
                cluster=Cluster.Cluster(nodes,run,genesis,settings,output);row={'case':case,'status':'failed',
                    'controller_clock_origin_utc':time.time()-time.monotonic(),'case_started_utc':time.time()}
                print(json.dumps({'starting':name}),flush=True)
                progress_stop=threading.Event()
                def Progress():
                    while not progress_stop.wait(10):
                        path=output/'timeline.jsonl'
                        if path.exists():
                            try:
                                with path.open('rb') as file:
                                    file.seek(max(0,path.stat().st_size-200000)); lines=file.read().splitlines()
                                point=json.loads(lines[-1]); print(json.dumps({'progress':name,'seconds':round(point['seconds'],1),'common':(point['common'] or {}).get('transactions'),'sent':point['sent'],'pool_sum':sum(n['pool'] for n in point['nodes'] if n)}),flush=True)
                            except (ValueError,IndexError,OSError):pass
                watcher=threading.Thread(target=Progress,daemon=True);watcher.start()
                try:
                    cluster.Start(); deadline=time.monotonic()+30
                    while not all(s['peers']>=9 for s in cluster.Stats()):
                        if time.monotonic()>deadline:raise TimeoutError('peer graph incomplete')
                        time.sleep(.1)
                    row['metrics']=Experiments.LoadCase(cluster,case,plan,dataset,genesis,output);row['status']='complete'
                except Exception as error:row['error']=str(error)
                finally:
                    cluster.Close();progress_stop.set();watcher.join(timeout=2)
                if cluster.cleanup_errors:row['cleanup_errors']=cluster.cleanup_errors;row['status']='failed'
                (output/'result.json').write_text(json.dumps(row,indent=2));rows.append(row)
                (WORK/'results.json').write_text(json.dumps(rows,indent=2))
                print(json.dumps({'case':name,**{key:row.get('metrics',{}).get(key) for key in ['common_commit_tps','achieved_send_tps','p99_ms','drained','sustainable','backlog_growth_per_second']},'invalid':row.get('metrics',{}).get('invalid_or_rejected_sum'),'error':row.get('error')}),flush=True)
                # Delete only this run's database after every account and state digest has been verified.
                if row.get('metrics',{}).get('drained'):
                    script="import pathlib,shutil; root=pathlib.Path('/home/ubuntu/chain/throughput-scratch-20261014').resolve(); run=root/"+repr(run)+"; assert run.resolve().is_relative_to(root); [shutil.rmtree(p) for p in run.glob('*/db') if p.is_dir()]"
                    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(lambda host:Remote(host,'python3 -c '+shlex.quote(script)),HOSTS))
                else:
                    print('Retained unfinished databases for analysis.',flush=True)
                    free=float(Remote('10.206.0.2',"python3 -c 'import shutil;print(shutil.disk_usage(\"/home/ubuntu\").free/2**30)'"))
                    if free<.45:print('Scratch disk budget reached.',flush=True);return
    finally:
        if WORK.exists():
            with tarfile.open(ROOT/'throughput-round-results-20261014.tar.gz','w:gz') as archive:
                for file in WORK.rglob('*'):
                    if file.is_file() and file.name!='transactions.bin':archive.add(file,arcname=str(file.relative_to(WORK)),recursive=False)
        password=None
        for client in CLIENTS.values():client.close()
if __name__=='__main__':Main()
