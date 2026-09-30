"""Interactive, password-only tuning of the isolated FISCO benchmark."""
import configparser,getpass,io,json,os,shlex,time,threading,subprocess,concurrent.futures
from pathlib import Path
import Distributed as D
from Deploy import ROOT,HOSTS,Connect,Remote
from Run import Check,Rpc

ORIGINAL=Path('/home/ubuntu/fisco-bench/20260930')
MACHINES=list(dict.fromkeys(HOSTS+D.SENDER_HOSTS))

def RunRemote(host,command):return Remote(D.CLIENTS[host],command)
def Save(name,data):
    path=ROOT/'tuning'/name;path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2))

def Ready():
    deadline=time.time()+90
    while time.time()<deadline:
        try:
            data=Check()
            if data['valid'] and all(n['pending']==0 for n in data['nodes']):return data
        except Exception:pass
        time.sleep(1)
    raise RuntimeError('Nodes did not reach the same drained chain')

def Stop():
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        list(pool.map(lambda h:RunRemote(h,f'cd {ROOT}/runtime-stable && bash node0/stop.sh'),HOSTS))

def Configure(spec):
    before=Check() if spec.get('recover') else Ready()
    assert all(n['pending']==0 for n in before['nodes'])
    Stop()
    def One(host):
        path=ROOT/'runtime-stable/node0/config.ini'
        ini=configparser.ConfigParser();ini.read_string(D.Read(D.FILES[host],path))
        for section,items in spec['ini'].items():
            for key,value in items.items():ini[section][key]=str(value)
        buf=io.StringIO();ini.write(buf);D.Write(D.FILES[host],path,buf.getvalue())
        RunRemote(host,f'cd {ROOT}/runtime-stable && bash node0/start.sh')
        return {'host':host,'ini':buf.getvalue()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:configs=list(pool.map(One,HOSTS))
    after=Ready()
    assert after['nodes'][0]['transactionCount']==max(n['transactionCount'] for n in before['nodes'])
    # A committed configuration transaction also initializes the execution path after restart.
    block=spec.get('block') or Rpc(HOSTS[0],'getSystemConfigByKey','tx_count_limit')['value']
    if block:
        host=HOSTS[0]
        subprocess.run(['javac','-proc:none','-cp','lib/*','-d','classes',str(Path(__file__).resolve().parent/'ConfigTx.java')],cwd=ROOT/'driver-stable',check=True)
        if host!='10.206.0.2':
            D.FILES[host].put(str(ROOT/'driver-stable/classes/ConfigTx.class'),str(ROOT/'driver-stable/classes/ConfigTx.class'))
        print(RunRemote(host,f"cd {ROOT}/driver-stable && java -cp 'classes:lib/*' ConfigTx config.toml {int(block)}"),flush=True)
        Ready()
        assert all(str(Rpc(h,'getSystemConfigByKey','tx_count_limit')['value'])==str(block) for h in HOSTS)
    Save(spec['name']+'-config.json',{'spec':spec,'nodes':configs,'before':before,'after':after})

def Single():
    assert len(HOSTS)==1 and ROOT!=ORIGINAL and ROOT.parent==ORIGINAL.parent
    node=HOSTS[0]
    # Existing databases stay in ORIGINAL. Create a separate one-validator chain.
    def One(host):
        script=f'''import pathlib,shutil,configparser,json,os
old=pathlib.Path({str(ORIGINAL)!r});root=pathlib.Path({str(ROOT)!r});host={host!r}
root.mkdir(exist_ok=True)
driver=root/'driver-stable';assert not driver.exists()
driver.mkdir()
for name in ['classes','conf']:shutil.copytree(old/'driver-stable'/name,driver/name)
for name in ['FiscoBench.java','pom.xml']:shutil.copy2(old/'driver-stable'/name,driver/name)
os.symlink(old/'driver-stable/lib',driver/'lib')
shutil.copy2(old/'driver-stable/config.toml',driver/'config.toml')
p=driver/'config.toml';s=p.read_text();import re
s=re.sub(r'peers\\s*=\\s*\\[[^]]*\\]', 'peers = ["{node}:20200"]',s);p.write_text(s)
if not (root/'nodes').exists():os.symlink(old/'nodes',root/'nodes')
if host=={node!r}:
 runtime=root/'runtime-stable';runtime.mkdir()
 shutil.copy2(old/'runtime-stable/fisco-bcos',runtime/'fisco-bcos')
 shutil.copytree(old/'runtime-stable/node0',runtime/'node0',ignore=shutil.ignore_patterns('data','log','nohup.out','*.pid'))
 p=runtime/'node0/config.genesis';g=configparser.ConfigParser();g.read(p)
 key=(runtime/'node0/conf/node.nodeid').read_text().strip()
 for k in list(g['consensus']):
  if k.startswith('node.'):del g['consensus'][k]
 g['consensus']['node.0']=key+':1'
 with p.open('w') as f:g.write(f)
 (runtime/'node0/nodes.json').write_text(json.dumps({{'nodes':[]}}))
'''
        RunRemote(host,'python3 -c '+shlex.quote(script))
    D.All(One,MACHINES)
    print(RunRemote(node,f'cd {ROOT}/runtime-stable && bash node0/start.sh'),flush=True)
    Save('single-genesis-check.json',Ready())

def Sample():
    def One(h):
        RunRemote(h,f'touch {ROOT}/sample.stop; sleep 2')
        s=D.FILES[h];s.put(str(Path(__file__).parent/'NodeSample.py'),str(ROOT/'NodeSample.py'))
        # Each tuning session starts one sampler and records its PID.
        return RunRemote(h,f'rm -f {ROOT}/sample.stop; (FISCO_BENCH_ROOT={ROOT} nohup python3 {ROOT}/NodeSample.py > {ROOT}/sample.log 2>&1 < /dev/null & echo $! > {ROOT}/sample.pid)')
    D.All(One,MACHINES)

def Collect():
    for h in MACHINES:
        dest=ROOT/'collected'/h;dest.mkdir(parents=True,exist_ok=True)
        D.FILES[h].get(str(ROOT/'resources.jsonl'),str(dest/'resources.jsonl'))
    from Analyze import Analyze
    data=Analyze(ROOT);Save('results.json',data)
    return data

def Profile(case,finished):
    folder=ROOT/'profiles'/case['name'];folder.mkdir(parents=True,exist_ok=True)
    gate=ROOT/'driver-stable/runs'/case['name']/'go'
    deadline=time.time()+200
    while not gate.exists() and time.time()<deadline and not finished.is_set():
        if (gate.parent/'exit').exists():return
        time.sleep(.2)
    if not gate.exists():return
    time.sleep(max(0,int(gate.read_text())/1000+case['warm']+2-time.time()))
    pid=int(subprocess.check_output(['pgrep','-x','fisco-bcos']).strip())
    r=subprocess.run(['sudo','-n','perf','record','-F','99','-g','-p',str(pid),'-o',str(folder/'perf.data'),'--','sleep','12'],capture_output=True,text=True)
    (folder/'record.txt').write_text(r.stdout+r.stderr)
    for name,sort in [('threads','comm'),('symbols','dso,symbol')]:
        r=subprocess.run(['sudo','-n','perf','report','--stdio','--no-children','--call-graph','none','--sort',sort,'-i',str(folder/'perf.data'),'--percent-limit','0.2'],capture_output=True,text=True)
        (folder/(name+'.txt')).write_text(r.stdout+r.stderr)

def Run(case):
    # Reserve space for the incoming ledger and one GiB of system headroom.
    for h in HOSTS:
        available=int(RunRemote(h,'df -B1 --output=avail / | tail -1').strip())
        assert available>1024**3+case['rate']*(case['warm']+case['seconds'])*500, (h,available)
    t=None;finished=threading.Event()
    if case.get('profile'):
        assert '10.206.0.2' in HOSTS
        t=threading.Thread(target=Profile,args=(case,finished));t.start()
    try:ok=D.Run(case)
    finally:
        finished.set()
        if t:t.join()
    data=Collect();row=next(x for x in data if x['name']==case['name'])
    print(json.dumps({k:row.get(k) for k in ['name','common_chain_tps','actual_send_tps','p99_ms','pending_net_growth_per_node','state_valid','count_valid','failures','sustainable']}),flush=True)
    return ok

if __name__=='__main__':
    ROOT.mkdir(parents=True,exist_ok=True)
    password=getpass.getpass('CLUSTER_PASSWORD: ')
    def Open(h):
        D.CLIENTS[h]=Connect(h,password);D.CLIENTS[h].get_transport().set_keepalive(20)
        D.FILES[h]=D.CLIENTS[h].open_sftp()
    D.All(Open,MACHINES);password=None
    try:
        while True:
            spec=json.loads(input('TUNE> '));action=spec['action']
            if action=='exit':break
            if action=='configure':Configure(spec)
            elif action=='single':Single()
            elif action=='sample':Sample()
            elif action=='run':Run(spec['case'])
            elif action=='check':print(json.dumps(Ready()),flush=True)
            elif action=='collect':Collect()
            elif action=='stop':
                Save('final-check.json',Ready());Stop()
                D.All(lambda h:RunRemote(h,f'touch {ROOT}/sample.stop'),MACHINES)
                Collect()
            print('ACTION_DONE',action,flush=True)
    finally:
        for s in D.FILES.values():s.close()
        for c in D.CLIENTS.values():c.close()
