"""Distributed pre-signed senders, a common start gate and all-validator accounting."""
import concurrent.futures,getpass,json,math,shlex,time,argparse,os
from pathlib import Path
from Deploy import ROOT,HOSTS,Connect,Remote
from Run import Check,Snapshot

SENDER_HOSTS=os.environ.get('FISCO_BENCH_SENDERS',','.join(HOSTS)).split(',')
CLIENTS={};FILES={}
def All(function,hosts=SENDER_HOSTS):
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:return list(pool.map(function,hosts))
def Exists(sftp,path):
    try:sftp.stat(str(path));return True
    except FileNotFoundError:return False
def Write(sftp,path,data):
    with sftp.open(str(path),'w') as f:f.write(data)
def Read(sftp,path):
    with sftp.open(str(path)) as f:return f.read().decode()

def Abort(remote):
    script=f'''import os,signal,pathlib
for p in pathlib.Path('/proc').glob('[0-9]*'):
 try:
  args=(p/'cmdline').read_bytes().split(b'\\0')
  if b'FiscoBench' in args and {str(remote/'driver.json').encode()!r} in args:os.kill(int(p.name),signal.SIGTERM)
 except OSError:pass
'''
    All(lambda h:Remote(CLIENTS[h],"python3 -c "+shlex.quote(script)))

def Run(case):
    name=case['name'];assert name.replace('-','').replace('_','').isalnum()
    out=ROOT/'results'/name;out.mkdir(parents=True,exist_ok=False)
    before=Check();(out/'before.json').write_text(json.dumps(before,indent=2))
    assert before['valid'] and all(n['pending']==0 for n in before['nodes'])
    rate=case['rate'];assert rate%len(SENDER_HOSTS)==0
    remote=ROOT/'driver-stable/runs'/name
    def Launch(host):
        Remote(CLIENTS[host],f'umask 077; mkdir -p {remote}; test ! -e {remote}/run.sh')
        command=['java','-Xms128m','-Xmx'+case.get('heap','2g'),'-XX:ActiveProcessorCount='+str(case.get('driver_cpus',2)),
            '-cp','classes:lib/*','FiscoBench','config.toml',case['mode'],str(rate//len(SENDER_HOSTS)),str(case['warm']),str(case['seconds']),str(remote/'driver.json'),str(case.get('users',2048)),str(remote/'go'),str(case.get('inflight',100000//len(SENDER_HOSTS)))]
        script='#!/bin/bash\numask 077\ncd '+str(ROOT/'driver-stable')+'\n'+shlex.join(command)+'\nprintf "%s\\n" "$?" > '+str(remote/'exit')+'\n'
        Write(FILES[host],remote/'run.sh',script)
        Remote(CLIENTS[host],f'(nohup bash {remote}/run.sh > {remote}/driver.log 2>&1 < /dev/null & echo $! > {remote}/launcher.pid)')
        return {'host':host,'command':command}
    manifest={'case':case,'release':'v3.7.3','drivers':All(Launch)}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print('PREPARING',name,flush=True)
    deadline=time.time()+180
    while True:
        states=All(lambda h:(Exists(FILES[h],remote/'go.ready'),Exists(FILES[h],remote/'exit')))
        if all(r for r,e in states):break
        if any(e for r,e in states) or time.time()>deadline:
            Abort(remote)
            (out/'failure.json').write_text(json.dumps({'reason':'Preparation failed','states':states}))
            for host in SENDER_HOSTS:
                dest=out/'drivers'/host;dest.mkdir(parents=True,exist_ok=True)
                for file in ['driver.log','exit','run.sh']:
                    if Exists(FILES[host],remote/file):FILES[host].get(str(remote/file),str(dest/file))
            raise RuntimeError(('Preparation failed',name,states))
        time.sleep(1)
    prefixes=All(lambda h:Read(FILES[h],remote/'go.ready'));assert len(set(prefixes))==len(SENDER_HOSTS)
    target=int((time.time()+3)*1000)
    def Gate(host):
        Write(FILES[host],remote/'go.tmp',str(target))
        FILES[host].rename(str(remote/'go.tmp'),str(remote/'go'))
    All(Gate)
    print('START',name,target,flush=True)
    deadline=time.time()+case['warm']+case['seconds']+180
    with (out/'chain.jsonl').open('w',buffering=1) as f:
        while True:
            t=time.time()
            try:f.write(json.dumps({'epoch':t,'nodes':Snapshot()})+'\n')
            except Exception as e:f.write(json.dumps({'epoch':t,'error':repr(e)})+'\n')
            if all(All(lambda h:Exists(FILES[h],remote/'exit'))):break
            if time.time()>deadline:Abort(remote);break
            time.sleep(max(.02,1-(time.time()-t)))
    def Download(host):
        dest=out/'drivers'/host;dest.mkdir(parents=True,exist_ok=True)
        for file in ['driver.json','driver.log','exit','go.ready','run.sh']:
            if Exists(FILES[host],remote/file):FILES[host].get(str(remote/file),str(dest/file))
        return json.loads((dest/'driver.json').read_text()) if (dest/'driver.json').exists() else None
    drivers=All(Download)
    after={'valid':False,'nodes':[]};deadline=time.time()+60
    while True:
        try:after=Check()
        except Exception as error:after={'valid':False,'nodes':[],'error':repr(error)}
        if (after['valid'] and not any(n['pending'] for n in after['nodes'])) or time.time()>=deadline:break
        time.sleep(1)
    (out/'after.json').write_text(json.dumps(after,indent=2))
    if any(d is None or 'balances_checked' not in d for d in drivers):
        (out/'failure.json').write_text(json.dumps({'reason':'At least one driver incomplete'}));return False
    d={'workload':case['mode'],'target_tps':rate,'warmup_s':case['warm'],'measure_s':case['seconds'],
       'phase':case.get('phase','initial'),
       'start_epoch_ms':max(x['start_epoch_ms'] for x in drivers),
       'end_epoch_ms':min(x['start_epoch_ms'] for x in drivers)+(case['warm']+case['seconds'])*1000,
       'start_skew_ms':max(x['start_epoch_ms'] for x in drivers)-min(x['start_epoch_ms'] for x in drivers),
       'presigned':True,'distributed':True,'driver_count':len(drivers)}
    for key in ['users','requested','submitted','success','failures','unresolved','actual_send_tps','receipt_tps',
                'queried_receipts','recovered_callback_failures','funding_delayed_receipts','balance_mismatches','balances_checked']:
        d[key]=sum(x.get(key,0) for x in drivers)
    d['setup_transactions']=sum(x['users']+(case['mode']=='solidity')+bool(x.get('created_native_table')) for x in drivers)
    d['send_s']=max(x['send_s'] for x in drivers);d['drain_s']=max(x['drain_s'] for x in drivers)
    hist={}
    for x in drivers:
        for k,v in x['latency_histogram_ms'].items():hist[int(k)]=hist.get(int(k),0)+v
    d['latency_histogram_ms']=hist
    for q,key in [(.5,'p50_ms'),(.95,'p95_ms'),(.99,'p99_ms')]:
        threshold=math.ceil(sum(hist.values())*q);count=0
        for ms,n in sorted(hist.items()):
            count+=n
            if count>=threshold:d[key]=ms;break
    d['max_driver_scheduled_p99_ms']=max(x['scheduled_p99_ms'] for x in drivers)
    (out/'driver.json').write_text(json.dumps(d,indent=2))
    ok=after['valid'] and not d['failures'] and not d['unresolved'] and not d['balance_mismatches']
    print('DONE',name,'receipt_tps',round(d['receipt_tps']),'P99',d.get('p99_ms'),'valid',ok,'skew_ms',d['start_skew_ms'],flush=True)
    if case['seconds']>=60:
        from Analyze import Analyze
        summary=next(r for r in Analyze(ROOT) if r['name']==name)
        print('SUSTAINABLE',summary['sustainable'],'chain_tps',round(summary['common_chain_tps'] or 0),
              'pending_net_growth',summary.get('pending_net_growth_per_node'),
              'pending_slope',summary.get('pending_slope_per_node'),flush=True)
        ok=ok and summary['sustainable']
    return ok

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('plan',type=Path);a=p.parse_args()
    password=getpass.getpass('CLUSTER_PASSWORD: ')
    def Open(host):CLIENTS[host]=Connect(host,password);FILES[host]=CLIENTS[host].open_sftp()
    All(Open);password=None
    try:
        for case in json.loads(a.plan.read_text()):
            if not Run(case):break
    finally:
        for s in FILES.values():s.close()
        for c in CLIENTS.values():c.close()
