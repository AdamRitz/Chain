"""Enable the v1 scheduler's explicit parallel option on all ten isolated nodes."""
import concurrent.futures,getpass,io,json,configparser,time
from Deploy import ROOT,HOSTS,Connect,Remote
from Run import Check

def Configure(host,password):
    c=Connect(host,password)
    try:
        Remote(c,f'cd {ROOT}/runtime && bash node0/stop.sh')
        p=str(ROOT/'runtime/node0/config.ini')
        with c.open_sftp() as s:
            with s.open(p) as f:raw=f.read().decode()
            with s.open(p+'.serial-v1','w') as f:f.write(raw)
            ini=configparser.ConfigParser();ini.read_string(raw)
            ini['executor']['baseline_scheduler_parallel']='true'
            ini['executor']['baseline_scheduler_maxthread']='8'
            ini['executor']['baseline_scheduler_chunksize']='100'
            b=io.StringIO();ini.write(b)
            with s.open(p,'w') as f:f.write(b.getvalue())
        print(host,Remote(c,f'cd {ROOT}/runtime && bash node0/start.sh').strip(),flush=True)
    finally:c.close()

if __name__=='__main__':
    before=Check()
    assert before['valid'] and all(n['pending']==0 for n in before['nodes'])
    password=getpass.getpass('CLUSTER_PASSWORD: ')
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(lambda h:Configure(h,password),HOSTS))
    time.sleep(4)
    after=Check();assert after['valid']
    assert before['nodes'][0]['transactionCount']==after['nodes'][0]['transactionCount']
    assert before['blocks'][0]['hash']==after['blocks'][0]['hash']
    (ROOT/'parallel-config-check.json').write_text(json.dumps({'before':before,'after':after},indent=2))
    print('PARALLEL_CONFIGURED',flush=True)
