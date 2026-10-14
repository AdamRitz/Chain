"""Deploy one verified release and run the five authorized ten-server short cases."""
import concurrent.futures
import getpass
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tarfile
import time
import CloudSixC4 as remote

ROOT=Path('/home/ubuntu/chain')
RELEASE=ROOT/'releases/2026-10-14-tps-a5cf8a2'
WORK=ROOT/'results/tps-a5cf8a2/run-1'
SCRATCH=ROOT/'scratch-tps-a5cf8a2'
PYTHON=remote.PYTHON
COMMIT='a5cf8a2dc2585cccf321dd08691b1f09ceef4628'
HOSTS=remote.HOSTS
BUNDLE=ROOT/'runtime-a5cf8a2.tar.gz'
remote.NEW=RELEASE;remote.WORK=WORK;remote.SCRATCH=SCRATCH
remote.PROFILE=remote.PROFILE.replace('scratch-six-c4a8724','scratch-tps-a5cf8a2')

def Save(path,data):
    path.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')

def Deploy(host,validation,bundle_hash):
    free=float(remote.Remote(host,"python3 -c 'import shutil;print(shutil.disk_usage(\"/home/ubuntu\").free/2**30)'"))
    if free<2:raise RuntimeError(host+': insufficient scratch space')
    if remote.Remote(host,"ss -H -lnt 'sport = :8089'").strip():raise RuntimeError(host+': test port is in use')
    if host!='10.206.0.2':
        remote.Remote(host,shlex.join(['mkdir',str(RELEASE)]))
        with remote.CLIENTS[host].open_sftp() as sftp:sftp.put(str(BUNDLE),str(BUNDLE))
        actual=remote.Remote(host,shlex.join(['sha256sum',str(BUNDLE)])).split()[0]
        assert actual==bundle_hash,host+': runtime archive checksum differs'
        remote.Remote(host,shlex.join(['tar','-xzf',str(BUNDLE),'-C',str(RELEASE)]))
    binaries={}
    for name,expected in validation['binaries'].items():
        binary=RELEASE/'build'/name
        if 'not found' in remote.Remote(host,shlex.join(['ldd',str(binary)])):raise RuntimeError(host+': missing library for '+name)
        digest=remote.Remote(host,shlex.join(['sha256sum',str(binary)])).split()[0]
        assert digest==expected,host+': binary checksum differs for '+name
        binaries[name]=digest
    remote.Remote(host,shlex.join(['mkdir','-p',str(SCRATCH)]))
    row={'host':host,'free_gib':free,'binaries':binaries,'commit':COMMIT,'bundle_sha256':bundle_hash}
    print(json.dumps({'deployed':host,'sha256':binaries['boost']}),flush=True)
    return row

def RunCase(rate,plan,dataset,genesis,rows):
    import Cluster,Experiments
    output=WORK/f'rate-{rate}';output.mkdir()
    run=f'a5cf8a2-{rate}-r1'
    defaults={'transport':'ssh','port':8089,'binary':str(RELEASE/'build/boost'),
              'worker':str(RELEASE/'Test/ClusterWorker.py'),'python':str(PYTHON),'root':str(SCRATCH)}
    nodes=[{**defaults,'id':f'node{i+1:02}','host':host,'ssh':'ubuntu@'+host} for i,host in enumerate(HOSTS)]
    settings={'io-threads':4,'verify-threads':8,'sync':1,'produce':1,'experiment-index':1,
              'clock-sync':1,'round-ms':50,'candidate-ms':12,'relay-transactions':0,
              'run-seconds':180,'block-ms':50,'max-block-txs':6000,'db-profile':0}
    case={'suite':'throughput','mode':'throughput','rate':rate,'repeat':1,'nodes':10,'version':'a5cf8a2',
          'pattern':'steady','round_ms':50,'candidate_ms':12,'max_block_txs':6000,'clock_sync':True}
    cluster=Cluster.Cluster(nodes,run,genesis,settings,output)
    row={'case':case,'status':'failed','case_started_utc':time.time(),
         'controller_clock_origin_utc':time.time()-time.monotonic()}
    print(json.dumps({'starting':rate,'nodes':10,'measurement_seconds':10}),flush=True)
    try:
        cluster.Start();deadline=time.monotonic()+30
        while not all(s['peers']>=9 for s in cluster.Stats()):
            if time.monotonic()>deadline:raise TimeoutError('Peer graph incomplete')
            time.sleep(.1)
        row['metrics']=Experiments.LoadCase(cluster,case,plan,dataset,genesis,output)
        row['status']='complete'
    except Exception as error:row['error']=str(error)
    finally:cluster.Close()
    row['case_finished_utc']=time.time()
    if cluster.cleanup_errors:
        row['cleanup_errors']=cluster.cleanup_errors;row['status']='failed'
    Save(output/'result.json',row);rows.append(row);Save(WORK/'results.json',rows)
    print(json.dumps({'finished':rate,'status':row['status'],**{key:row.get('metrics',{}).get(key) for key in
          ['common_commit_tps','achieved_send_tps','p99_ms','drained','sustainable','tx_sustainable',
           'logical_backlog_end','tx_backlog_growth_per_second','measurement_seconds','measurement_samples']},
          'error':row.get('error'),'cleanup_errors':row.get('cleanup_errors')}),flush=True)
    if cluster.cleanup_errors:raise RuntimeError('Finish node stop/database cleanup before the next case')

def Postflight():
    code='''
import json,psutil,shutil,subprocess
from pathlib import Path
scratch=Path('/home/ubuntu/chain/scratch-tps-a5cf8a2')
active=[{'pid':p.pid,'binary':p.info['exe']} for p in psutil.process_iter(['exe'])
        if (p.info['exe'] or '').endswith('/build/boost')]
databases=[str(p) for p in scratch.glob('*/node[0-9][0-9]/db') if p.is_dir()]
port=subprocess.run(['ss','-H','-lnt','sport = :8089'],capture_output=True,text=True).stdout.strip()
print(json.dumps({'active':active,'remaining_databases':databases,'port8089':port,
                 'free_gib':shutil.disk_usage(scratch).free/2**30}))
'''
    def Check(host):return {'host':host,**json.loads(remote.Remote(host,shlex.join([str(PYTHON),'-c',code])))}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:rows=list(pool.map(Check,HOSTS))
    Save(WORK/'postflight.json',rows)
    assert all(not row['active'] and not row['remaining_databases'] and not row['port8089'] for row in rows),'Postflight cleanup incomplete'
    print(json.dumps({'postflight_nodes':10,'remaining_databases':0,'minimum_free_gib':min(row['free_gib'] for row in rows)}),flush=True)

def Main():
    password=getpass.getpass('CLUSTER_PASSWORD: ')
    profiles=None;samplers=[]
    WORK.mkdir(parents=True,exist_ok=False)
    try:
        validation=json.loads((RELEASE/'validation.json').read_text())
        assert validation['commit']==COMMIT and validation['ctest_passed']
        with BUNDLE.open('rb') as stream:bundle_hash=hashlib.file_digest(stream,'sha256').hexdigest()
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(lambda host:remote.Connect(host,password),HOSTS))
        password=None
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            deployment=list(pool.map(lambda host:Deploy(host,validation,bundle_hash),HOSTS))
        Save(WORK/'deployment.json',deployment);Save(WORK/'validation.json',validation)
        (WORK/'preflight.json').write_bytes((WORK.parent/'preflight.json').read_bytes())
        sys.path.insert(0,str(RELEASE/'Test'));import Cluster,Experiments
        Cluster.Worker=remote.Worker
        plan=json.loads((RELEASE/'Experiments/plan.json').read_text())
        plan.update({'nodes':10,'repeats':1,'warmup':2,'duration':10,'drain_seconds':5,'poll_seconds':.05,
                     'sample_hz':20,'batch':128,'ingress':'broadcast','parallel_ingress':False,
                     'clock_sync':True,'round_ms':50,'throughput_rates':[20000,30000,40000,50000,60000]})
        Experiments.ValidatePlan(plan);Save(WORK/'plan.json',plan)
        source=ROOT/'results/six-20261014-c4a8724/transactions.bin'
        dataset=WORK/'transactions.bin';dataset.hardlink_to(source)
        genesis_path=Path(str(dataset)+'.genesis.json');genesis_path.hardlink_to(Path(str(source)+'.genesis.json'))
        genesis=json.loads(genesis_path.read_text())
        with dataset.open('rb') as stream:dataset_hash=hashlib.file_digest(stream,'sha256').hexdigest()
        Save(WORK/'dataset.json',{'bytes':dataset.stat().st_size,'sha256':dataset_hash,'source':str(source),
                                'wallets':plan['wallets'],'commit':COMMIT})
        (WORK/'host-profile').mkdir();profiles=concurrent.futures.ThreadPoolExecutor(max_workers=10)
        samplers=[profiles.submit(remote.SampleHost,i) for i in range(10)]
        rows=[]
        for rate in plan['throughput_rates']:RunCase(rate,plan,dataset,genesis,rows)
        Postflight()
    finally:
        password=None
        for host in remote.CLIENTS:
            try:remote.Remote(host,shlex.join(['touch',str(SCRATCH/'profile.stop')]),timeout=15)
            except Exception as error:print('PROFILE_STOP_ERROR',host,str(error),flush=True)
        if profiles:
            profiles.shutdown(wait=True)
            for future in samplers:
                try:future.result()
                except Exception as error:print('PROFILE_ERROR',str(error),flush=True)
        archive_path=ROOT/'a5cf8a2-ten-results.tar.gz'
        with tarfile.open(archive_path,'w:gz') as archive:
            for path in WORK.rglob('*'):
                if path.is_file() and path.name!='transactions.bin':archive.add(path,arcname=str(path.relative_to(WORK)),recursive=False)
        with archive_path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        print(json.dumps({'results_archive':str(archive_path),'bytes':archive_path.stat().st_size,'sha256':digest}),flush=True)
        for client in remote.CLIENTS.values():client.close()

if __name__=='__main__':Main()
