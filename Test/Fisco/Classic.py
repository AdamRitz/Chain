"""Start a fresh executor-v0 benchmark chain, preserving the v1 databases."""
import concurrent.futures,getpass,io,json,configparser,time
from Deploy import ROOT,HOSTS,Connect,Remote
from Run import Check

def Stop(host,password):
    c=Connect(host,password)
    try:return Remote(c,f'cd {ROOT}/runtime && bash node0/stop.sh')
    finally:c.close()

def Switch(host,password):
    c=Connect(host,password)
    try:
        out=ROOT/'runtime-classic'
        Remote(c,f'test ! -e {out} && cd {ROOT}/runtime && mkdir -p {out}/node0 && cp -a node0/conf node0/config.ini node0/config.genesis node0/nodes.json node0/start.sh node0/stop.sh {out}/node0/ && ln -s {ROOT}/runtime/fisco-bcos {out}/fisco-bcos')
        p=str(out/'node0/config.genesis')
        with c.open_sftp() as s:
            with s.open(p) as f:raw=f.read().decode()
            ini=configparser.ConfigParser();ini.read_string(raw);ini['executor']['version']='0'
            b=io.StringIO();ini.write(b)
            with s.open(p,'w') as f:f.write(b.getvalue())
        return host,Remote(c,f'cd {out} && bash node0/start.sh').strip()
    finally:c.close()

if __name__=='__main__':
    before=Check();assert before['valid'] and all(n['pending']==0 for n in before['nodes'])
    (ROOT/'v1-final-check.json').write_text(json.dumps(before,indent=2))
    password=getpass.getpass('CLUSTER_PASSWORD: ')
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda h:Stop(h,password),HOSTS))
        for r in pool.map(lambda h:Switch(h,password),HOSTS):print(r,flush=True)
    time.sleep(4);after=Check()
    assert after['valid'] and all(n['transactionCount']==0 for n in after['nodes'])
    (ROOT/'classic-start-check.json').write_text(json.dumps(after,indent=2))
    print('CLASSIC_READY',flush=True)
