"""Rate sweeps, common-chain checks and resource capture; run on the controller."""
import argparse,concurrent.futures,getpass,http.client,json,ssl,subprocess,time,hashlib,configparser
from pathlib import Path
from Deploy import ROOT,HOSTS,Connect,Remote

CERT=ROOT/'nodes/10.206.0.2/sdk'
CTX=ssl.create_default_context(cafile=str(CERT/'ca.crt'))
# Official private-chain certs authenticate membership through this generated CA.
CTX.check_hostname=False
CTX.load_cert_chain(str(CERT/'sdk.crt'),str(CERT/'sdk.key'))

def Rpc(host,method,*params):
    conn=http.client.HTTPSConnection(host,20200,context=CTX,timeout=15)
    try:
        body=json.dumps({'jsonrpc':'2.0','method':method,'params':['group0','',*params],'id':1})
        conn.request('POST','/',body,{'Content-Type':'application/json'})
        result=json.loads(conn.getresponse().read())
        if 'error' in result:raise RuntimeError(result['error'])
        return result['result']
    finally:conn.close()

def Snapshot():
    def One(host):
        begin=time.time()
        c=Rpc(host,'getTotalTransactionCount')
        c.update(host=host,pending=Rpc(host,'getPendingTxSize'),begin_epoch=begin,end_epoch=time.time())
        return c
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:return list(pool.map(One,HOSTS))

def Check():
    rows=Snapshot();height=min(x['blockNumber'] for x in rows)
    def One(host):
        block=Rpc(host,'getBlockByNumber',height,True,True)
        return {'host':host,'height':height,'hash':block['hash'],'stateRoot':block.get('stateRoot'),
                'sealers':len(Rpc(host,'getSealerList')),'observers':len(Rpc(host,'getObserverList'))}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:checks=list(pool.map(One,HOSTS))
    valid=(len({x['blockNumber'] for x in rows})==1 and len({x['transactionCount'] for x in rows})==1 and
        len({x['hash'] for x in checks})==1 and len({x['stateRoot'] for x in checks})==1 and
        all(x['sealers']==len(HOSTS) and x['observers']==0 for x in checks))
    return {'valid':valid,'nodes':rows,'blocks':checks}

def Samplers(password):
    def One(host):
        c=Connect(host,password)
        try:
            if host!='10.206.0.2':
                with c.open_sftp() as s:s.put(str(ROOT/'NodeSample.py'),str(ROOT/'NodeSample.py'))
            return Remote(c,f'test ! -e {ROOT}/sample.stop && (nohup python3 {ROOT}/NodeSample.py > {ROOT}/sample.log 2>&1 < /dev/null & echo $! > {ROOT}/sample.pid)')
        finally:c.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(One,HOSTS))

def Run(mode,rate,warm,seconds,name,users=1024):
    out=ROOT/'results'/name;out.mkdir(parents=True,exist_ok=False)
    before=Check();(out/'before.json').write_text(json.dumps(before,indent=2))
    if not before['valid']:raise RuntimeError('Initial chain consistency failed')
    command=['java','-Xms512m','-Xmx5g','-XX:ActiveProcessorCount=8','-cp','classes:lib/*','FiscoBench','config.toml',mode,str(rate),str(warm),str(seconds),str(out/'driver.json'),str(users)]
    genesis=ROOT/('runtime-classic' if name.startswith('classic-') else 'runtime')/'node0/config.genesis'
    config=configparser.ConfigParser();config.read(genesis)
    metadata={'command':command,'executor_version_config':config['executor']['version'],
        'files':{str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest()
            for f in [ROOT/'driver/FiscoBench.java',ROOT/'driver/classes/FiscoBench.class',ROOT/'driver/config.toml']}}
    (out/'manifest.json').write_text(json.dumps(metadata,indent=2))
    print('RUN',name,mode,rate,warm,seconds,flush=True)
    with (out/'driver.log').open('w') as log, (out/'chain.jsonl').open('w',buffering=1) as samples:
        process=subprocess.Popen(command,cwd=ROOT/'driver',stdout=log,stderr=subprocess.STDOUT)
        while process.poll() is None:
            start=time.time()
            try:samples.write(json.dumps({'epoch':start,'nodes':Snapshot()})+'\n')
            except Exception as e:samples.write(json.dumps({'epoch':start,'error':repr(e)})+'\n')
            time.sleep(max(.05,1-(time.time()-start)))
    time.sleep(2);after=Check();deadline=time.time()+60
    while (not after['valid'] or any(n['pending'] for n in after['nodes'])) and time.time()<deadline:
        time.sleep(1);after=Check()
    (out/'after.json').write_text(json.dumps(after,indent=2))
    result={'name':name,'exit':process.returncode,'consistent':after['valid']}
    if (out/'driver.json').exists():
        data=json.loads((out/'driver.json').read_text())
        for key in ['target_tps','receipt_tps','actual_send_tps','overall_tps','p99_ms','failures','unresolved','balance_mismatches','backlog_growth_per_s']:result[key]=data.get(key)
    print('DONE',json.dumps(result),flush=True)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--sample',action='store_true');p.add_argument('--plan');p.add_argument('--check',action='store_true');args=p.parse_args()
    if args.sample:Samplers(getpass.getpass('CLUSTER_PASSWORD: '))
    if args.check:print(json.dumps(Check(),indent=2))
    if args.plan:
        plan=json.loads(Path(args.plan).read_text())
        for case in plan:
            result=Run(**case)
            if result['exit'] or not result['consistent'] or result.get('failures') or result.get('balance_mismatches'):
                print('STOP: verify the unsuccessful case before raising load',flush=True);break
