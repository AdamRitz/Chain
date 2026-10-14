"""Ten-server throughput comparison of c4a8724 and 3850727."""
import concurrent.futures
import getpass
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tarfile
import threading
import time
import paramiko

ROOT=Path('/home/ubuntu/chain')
NEW=ROOT/'releases/2026-10-14-six-c4a8724'
OLD=ROOT/'releases/2026-10-14-tps-v2'
WORK=ROOT/'results/six-20261014-c4a8724'
SCRATCH=ROOT/'scratch-six-c4a8724'
PYTHON=ROOT/'releases/2026-09-30-d4106d1/.venv/bin/python'
HOSTS=['10.206.0.16','10.206.0.4','10.206.0.14','10.206.0.17','10.206.0.15','10.206.0.12','10.206.0.2','10.206.0.6','10.206.0.11','10.206.0.8']
CLIENTS={};LOCKS={}
NEW_HASH='882d7e8607b121113cd13558914940b8c9651b07d13bc242889a4844959cd4f5'
OLD_HASH='a8fc85bf5b059504276eb4d93f77d305a922f811c5fcd882f3bd8544a584dcd7'

PROFILE=r'''
import json,time,psutil
from pathlib import Path
previous=None; stop=Path('/home/ubuntu/chain/scratch-six-c4a8724/profile.stop')
for i in range(3600):
    if stop.exists():break
    now=time.time(); cpu=psutil.cpu_times(); disks=psutil.disk_io_counters(perdisk=True)
    network=psutil.net_io_counters(pernic=True); nodes=[]
    for p in psutil.process_iter(['pid','exe','cpu_times','memory_info']):
        if '/chain/releases/' in (p.info['exe'] or '') and (p.info['exe'] or '').endswith('/build/boost'):
            try:
                nodes.append({'pid':p.pid,'binary':p.info['exe'],'cpu_seconds':p.info['cpu_times'].user+p.info['cpu_times'].system,
                    'rss':p.info['memory_info'].rss,'threads':[{'id':t.id,'cpu_seconds':t.user_time+t.system_time,
                    'wait':Path(f'/proc/{p.pid}/task/{t.id}/wchan').read_text().strip()} for t in p.threads()]})
            except (psutil.Error,OSError):pass
    sample={'utc_seconds':now,'cpu':cpu._asdict(),'disks':{k:v._asdict() for k,v in disks.items() if k.startswith(('vd','sd','nvme'))},
        'network':{k:v._asdict() for k,v in network.items() if k!='lo'},'nodes':nodes,
        'available_memory_bytes':psutil.virtual_memory().available,'swap_used_bytes':psutil.swap_memory().used}
    if previous:
        seconds=now-previous['utc_seconds']; delta={k:sample['cpu'].get(k,0)-previous['cpu'].get(k,0) for k in ['user','nice','system','idle','iowait','irq','softirq','steal']}
        total=sum(delta.values());sample['cpu_percent']={k:100*v/total if total else 0 for k,v in delta.items()}
        sample['disk_rates']={}
        for name,data in sample['disks'].items():
            before=previous['disks'].get(name,{})
            writes=data.get('write_count',0)-before.get('write_count',0)
            sample['disk_rates'][name]={'util_percent':100*(data.get('busy_time',0)-before.get('busy_time',0))/(1000*seconds),
                'write_await_ms':(data.get('write_time',0)-before.get('write_time',0))/writes if writes else None,
                'write_bytes_per_second':(data.get('write_bytes',0)-before.get('write_bytes',0))/seconds}
        sample['network_rates']={}
        for name,data in sample['network'].items():
            before=previous['network'].get(name,{})
            sample['network_rates'][name]={k:(data.get(k,0)-before.get(k,0))/seconds for k in ['bytes_sent','bytes_recv','packets_sent','packets_recv','errin','errout','dropin','dropout']}
    print(json.dumps(sample),flush=True);previous=sample;time.sleep(1)
'''

def Connect(host,password):
    client=paramiko.SSHClient();client.load_system_host_keys();client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host,username='ubuntu',password=password,allow_agent=False,look_for_keys=False,timeout=12)
    client.get_transport().set_keepalive(15);CLIENTS[host]=client;LOCKS[host]=threading.Lock()

def Remote(host,command,input_data=None,timeout=90):
    with LOCKS[host]:
        stdin,out,err=CLIENTS[host].exec_command(command,timeout=timeout)
        if input_data is not None:stdin.write(input_data);stdin.flush()
        stdin.channel.shutdown_write(); text=out.read().decode(); error=err.read().decode(); code=out.channel.recv_exit_status()
        if code:raise RuntimeError(f'{host} exit {code}: {text[-600:]} {error[-600:]}')
        return text

def Worker(node,request):
    response=json.loads(Remote(node['host'],shlex.join([node['python'],node['worker']]),
        json.dumps({**request,'root':node['root'],'node':node['id']})))
    if not response.get('ok'):raise RuntimeError(response.get('error','worker failed'))
    return response['result']

def Deploy(host):
    free=float(Remote(host,"python3 -c 'import shutil;print(shutil.disk_usage(\"/home/ubuntu\").free/2**30)'"))
    if free<2:raise RuntimeError(host+': insufficient scratch disk space')
    if Remote(host,"ss -H -lnt 'sport = :8089'").strip():raise RuntimeError(host+': test port already in use')
    if host!='10.206.0.2':
        Remote(host,shlex.join(['mkdir',str(NEW)]))
        with CLIENTS[host].open_sftp() as sftp:
            sftp.put(str(ROOT/'six-runtime-c4a8724.tar.gz'),str(ROOT/'six-runtime-c4a8724.tar.gz'))
        Remote(host,shlex.join(['tar','-xzf',str(ROOT/'six-runtime-c4a8724.tar.gz'),'-C',str(NEW)]))
    if 'not found' in Remote(host,shlex.join(['ldd',str(NEW/'build/boost')])):raise RuntimeError(host+': missing dependency')
    actual=Remote(host,shlex.join(['sha256sum',str(NEW/'build/boost')])).split()[0]
    baseline=Remote(host,shlex.join(['sha256sum',str(OLD/'build/boost')])).split()[0]
    if actual!=NEW_HASH or baseline!=OLD_HASH:raise RuntimeError(host+': node binary checksum mismatch')
    Remote(host,shlex.join(['mkdir','-p',str(SCRATCH)]))
    print(json.dumps({'deployed':host,'sha256':actual,'free_gib':free}),flush=True)
    return {'host':host,'new_sha256':actual,'old_sha256':baseline,'free_gib':free}

def SampleHost(index):
    host=HOSTS[index]
    _,out,err=CLIENTS[host].exec_command(shlex.join([str(PYTHON),'-u','-c',PROFILE]),timeout=3605)
    with (WORK/'host-profile'/f'node{index+1:02}.jsonl').open('w') as file:
        for line in out:file.write(line);file.flush()
    if out.channel.recv_exit_status():raise RuntimeError(host+': host sampler failed: '+err.read().decode()[-500:])

def CloseCluster(cluster):
    # Each worker validates the recorded PID, creation time, executable and directory.
    def Stop(index):
        try:
            cluster.clients[index].Close()
            Worker(cluster.nodes[index],{'action':'stop','run':cluster.run})
            data=Worker(cluster.nodes[index],{'action':'collect','run':cluster.run})
            return index,data,None
        except Exception as error:return index,None,str(error)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results=list(pool.map(Stop,sorted(cluster.started)))
    for index,data,error in results:
        if error:cluster.cleanup_errors.append(error)
        else:(cluster.output/(cluster.nodes[index]['id']+'.json')).write_text(json.dumps(data,indent=2))
    cluster.pool.shutdown(wait=True)

def RunCase(version,rate,repeat,plan,dataset,genesis,rows):
    import Cluster,Experiments
    name=f'{version}-{rate}-r{repeat}';output=WORK/name;output.mkdir();run='six-c4a8724-'+name
    release=NEW if version=='new' else OLD
    defaults={'transport':'ssh','port':8089,'binary':str(release/'build/boost'),
        'worker':str(NEW/'Test/ClusterWorker.py'),'python':str(PYTHON),'root':str(SCRATCH)}
    nodes=[{**defaults,'id':f'node{i+1:02}','host':host,'ssh':'ubuntu@'+host} for i,host in enumerate(HOSTS)]
    settings={'io-threads':4,'verify-threads':8,'sync':1,'produce':1,'experiment-index':1,'clock-sync':1,
        'round-ms':50,'candidate-ms':12,'relay-transactions':0,'run-seconds':180,'block-ms':50,'max-block-txs':6000}
    case={'suite':'throughput','mode':'throughput','rate':rate,'repeat':repeat,'nodes':10,'version':version,
        'round_ms':50,'candidate_ms':12,'max_block_txs':6000,'clock_sync':True}
    cluster=Cluster.Cluster(nodes,run,genesis,settings,output)
    row={'case':case,'status':'failed','controller_clock_origin_utc':time.time()-time.monotonic(),'case_started_utc':time.time()}
    print(json.dumps({'starting':name}),flush=True)
    try:
        cluster.Start();deadline=time.monotonic()+30
        while not all(s['peers']>=9 for s in cluster.Stats()):
            if time.monotonic()>deadline:raise TimeoutError('peer graph incomplete')
            time.sleep(.1)
        row['metrics']=Experiments.LoadCase(cluster,case,plan,dataset,genesis,output);row['status']='complete'
    except Exception as error:row['error']=str(error)
    finally:CloseCluster(cluster)
    if cluster.cleanup_errors:row['cleanup_errors']=cluster.cleanup_errors;row['status']='failed'
    (output/'result.json').write_text(json.dumps(row,indent=2));rows.append(row)
    (WORK/'results.json').write_text(json.dumps(rows,indent=2))
    print(json.dumps({'case':name,**{k:row.get('metrics',{}).get(k) for k in ['common_commit_tps','achieved_send_tps','p99_ms','drained','sustainable','backlog_growth_per_second','invalid_or_rejected_sum']},'error':row.get('error')}),flush=True)
    if row.get('metrics',{}).get('drained') and not cluster.cleanup_errors:
        script="from pathlib import Path;import shutil;root=Path("+repr(str(SCRATCH))+").resolve();run=root/"+repr(run)+";assert run.resolve().is_relative_to(root);[shutil.rmtree(p) for p in run.glob('*/db') if p.is_dir() and p.resolve().is_relative_to(root)]"
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(lambda host:Remote(host,shlex.join(['python3','-c',script])),HOSTS))
    free=float(Remote('10.206.0.2',"python3 -c 'import shutil;print(shutil.disk_usage(\"/home/ubuntu\").free/2**30)'"))
    if free<1.5:raise RuntimeError('Scratch disk budget reached; unfinished databases retained')
    if cluster.cleanup_errors:raise RuntimeError('A test node could not be stopped')
    return row

def Main():
    password=getpass.getpass('CLUSTER_PASSWORD: ') if sys.stdin.isatty() else sys.stdin.readline().rstrip()
    profiles=None
    try:
        WORK.mkdir(parents=True,exist_ok=False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(lambda host:Connect(host,password),HOSTS))
        password=None
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:deployment=list(pool.map(Deploy,HOSTS))
        (WORK/'deployment.json').write_text(json.dumps(deployment,indent=2))
        (WORK/'host-profile').mkdir();profiles=concurrent.futures.ThreadPoolExecutor(max_workers=10)
        samplers=[profiles.submit(SampleHost,i) for i in range(10)]
        sys.path.insert(0,str(NEW/'Test'));import Cluster
        Cluster.Worker=Worker
        plan=json.loads((NEW/'Experiments/plan.json').read_text())
        plan.update({'nodes':10,'repeats':3,'warmup':5,'duration':30,'drain_seconds':20,'poll_seconds':.05,
            'sample_hz':20,'max_backlog_growth':10,'batch':128,'ingress':'broadcast','clock_sync':True,'round_ms':50,'candidate_ms':12})
        (WORK/'plan.json').write_text(json.dumps(plan,indent=2))
        dataset=WORK/'transactions.bin';source=ROOT/'results/throughput-20261014-next/transactions.bin'
        dataset.hardlink_to(source)
        Path(str(dataset)+'.genesis.json').hardlink_to(Path(str(source)+'.genesis.json'))
        genesis=json.loads(Path(str(dataset)+'.genesis.json').read_text())
        (WORK/'dataset-sha256.txt').write_text(hashlib.file_digest(dataset.open('rb'),'sha256').hexdigest())
        rows=[]
        initial=[('new',30000,1),('old',50000,1),('new',50000,1),('new',60000,1),('old',60000,1),('new',80000,1),('new',100000,1)]
        for task in initial:RunCase(*task,plan,dataset,genesis,rows)
        qualifies=[r['case']['rate'] for r in rows if r['case']['version']=='new' and r.get('metrics',{}).get('drained')
            and (r['metrics'].get('achieved_send_tps') or 0)>=.95*r['case']['rate']
            and (r['metrics'].get('common_commit_tps') or 0)>=.95*r['case']['rate']]
        boundary=max(qualifies,default=30000)
        adjacent=next((rate for rate in [30000,50000,60000,80000,100000] if rate>boundary),80000)
        confirmation=[]
        for repeat in [2,3]:
            for rate in sorted({50000,60000,boundary,adjacent}):
                order=['new','old'] if repeat==2 else ['old','new']
                for version in order:
                    if version=='old' and rate not in [50000,60000]:continue
                    if version=='new' and rate not in {boundary,adjacent}:continue
                    confirmation.append((version,rate,repeat))
        (WORK/'case-selection.json').write_text(json.dumps({'initial':initial,'boundary':boundary,'adjacent':adjacent,'confirmation':confirmation},indent=2))
        for task in confirmation:RunCase(*task,plan,dataset,genesis,rows)
    finally:
        password=None
        for host in CLIENTS:
            try:Remote(host,shlex.join(['touch',str(SCRATCH/'profile.stop')]),timeout=15)
            except Exception as error:print('PROFILE_STOP_ERROR',host,type(error).__name__,flush=True)
        if profiles:
            profiles.shutdown(wait=True)
            for future in samplers:
                try:future.result()
                except Exception as error:print('PROFILE_ERROR',str(error),flush=True)
        if WORK.exists():
            with tarfile.open(ROOT/'six-c4a8724-results.tar.gz','w:gz') as archive:
                for file in WORK.rglob('*'):
                    if file.is_file() and file.name!='transactions.bin':archive.add(file,arcname=str(file.relative_to(WORK)),recursive=False)
        for client in CLIENTS.values():client.close()
        print('EXPERIMENT_FINISHED',flush=True)

if __name__=='__main__':Main()
