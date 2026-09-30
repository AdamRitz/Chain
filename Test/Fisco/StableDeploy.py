"""Install ten local load drivers and start an isolated official stable chain."""
import concurrent.futures,getpass,io,json,configparser,time,hashlib
from pathlib import Path
from Deploy import ROOT,HOSTS,Connect,Remote
from Run import Check

def Stop(host,password):
    c=Connect(host,password)
    try:
        Remote(c,f'cd {ROOT}/runtime-classic && bash node0/stop.sh; touch {ROOT}/sample.stop')
    finally:c.close()

def Prepare(host,password):
    c=Connect(host,password);out=ROOT/'runtime-stable';driver=ROOT/'driver-stable'
    try:
        Remote(c,f'umask 077; test ! -e {out} && mkdir -p {out}/node0 {driver}')
        with c.open_sftp() as s:
            s.put(str(ROOT/'downloads/fisco-bcos-stable.tar.gz'),str(out/'binary.tar.gz'))
            s.put(str(ROOT/'driver-package.tar.gz'),str(driver/'package.tar.gz'))
            if host!='10.206.0.2':s.put(str(ROOT/'NodeSample.py'),str(ROOT/'NodeSample.py'))
        Remote(c,f'cd {out} && tar -xzf binary.tar.gz && cp -a {ROOT}/runtime/node0/conf {ROOT}/runtime/node0/config.ini {ROOT}/runtime/node0/config.genesis {ROOT}/runtime/node0/nodes.json {ROOT}/runtime/node0/start.sh {ROOT}/runtime/node0/stop.sh node0/ && chmod +x fisco-bcos && cd {driver} && tar -xzf package.tar.gz')
        with c.open_sftp() as s:
            for name,changes in [('config.genesis',{'version':{'compatibility_version':'3.7.3'},'executor':{'version':'0'}}),
                                 ('config.ini',{'executor':{'baseline_scheduler_parallel':'false'}})]:
                p=str(out/'node0'/name)
                with s.open(p) as f:ini=configparser.ConfigParser();ini.read_string(f.read().decode())
                for section,values in changes.items():
                    for key,value in values.items():ini[section][key]=value
                text=io.StringIO();ini.write(text)
                with s.open(p,'w') as f:f.write(text.getvalue())
            config=f'''[cryptoMaterial]
certPath = "{driver}/conf"
disableSsl = "false"
useSMCrypto = "false"
[network]
peers = ["{host}:20200"]
sendRpcRequestToHighestBlockNode = "false"
messageTimeout = "15000"
[account]
keyStoreDir = "{driver}/account"
[threadPool]
threadPoolSize = "2"
'''
            with s.open(str(driver/'config.toml'),'w') as f:f.write(config)
        Remote(c,f'command -v java >/dev/null || sudo -n apt-get install -y openjdk-17-jre-headless > {ROOT}/install-jre.log 2>&1')
        print(host,'prepared',flush=True)
        return host
    finally:c.close()

def Start(host,password):
    c=Connect(host,password)
    try:
        Remote(c,f'rm -f {ROOT}/sample.stop; (nohup python3 {ROOT}/NodeSample.py > {ROOT}/sample.log 2>&1 < /dev/null & echo $! > {ROOT}/sample.pid)')
        return host,Remote(c,f'cd {ROOT}/runtime-stable && bash node0/start.sh').strip()
    finally:c.close()

if __name__=='__main__':
    password=getpass.getpass('CLUSTER_PASSWORD: ')
    # Keep the failed overload state as evidence before stopping the old chain.
    (ROOT/'classic-overload-final.json').write_text(json.dumps(Check(),indent=2))
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda h:Stop(h,password),HOSTS));time.sleep(2)
        list(pool.map(lambda h:Prepare(h,password),HOSTS))
        for value in pool.map(lambda h:Start(h,password),HOSTS):print(value,flush=True)
    time.sleep(4);after=Check();assert after['valid'] and all(n['transactionCount']==0 for n in after['nodes'])
    (ROOT/'stable-start-check.json').write_text(json.dumps(after,indent=2))
    print('STABLE_READY',flush=True)
