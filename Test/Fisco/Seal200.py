import getpass,json,time,configparser,io
from Deploy import ROOT,HOSTS,Connect,Remote
from Inspect import Collect
import Distributed as D
from Run import Check
password=getpass.getpass('CLUSTER_PASSWORD: ')
before=Check();assert before['valid'] and all(n['pending']==0 for n in before['nodes'])
D.All(lambda h:Collect(h,password,True));print('STOPPED',flush=True)
def Start(h):
 c=Connect(h,password);D.CLIENTS[h]=c;D.FILES[h]=c.open_sftp();s=D.FILES[h]
 Remote(c,f'cp {ROOT}/runtime-stable/node0/config.ini {ROOT}/runtime-stable/node0/config.ini.seal100')
 path=str(ROOT/'runtime-stable/node0/config.ini')
 with s.open(path) as f:ini=configparser.ConfigParser();ini.read_string(f.read().decode())
 ini['consensus']['min_seal_time']='200';buf=io.StringIO();ini.write(buf)
 with s.open(path,'w') as f:f.write(buf.getvalue())
 Remote(c,f'rm -f {ROOT}/sample.stop; (nohup python3 {ROOT}/NodeSample.py > {ROOT}/sample.log 2>&1 < /dev/null & echo $! > {ROOT}/sample.pid)')
 return h,Remote(c,f'cd {ROOT}/runtime-stable && bash node0/start.sh')
print(D.All(Start),flush=True);deadline=time.time()+90
while True:
 try:
  after=Check()
  if after['valid'] and after['nodes'][0]['transactionCount']==before['nodes'][0]['transactionCount']:break
 except Exception as e:print('WAITING_RPC',type(e).__name__,flush=True)
 if time.time()>deadline:raise RuntimeError('Readiness timeout')
 time.sleep(2)
(ROOT/'seal200-start-check.json').write_text(json.dumps({'before':before,'after':after},indent=2));print('SEAL200_READY',flush=True)
plan=[dict(mode=m,rate=35000,warm=10,seconds=60,users=2048,phase='seal200',name=f'stable-{m}-35000-seal200-r{i}') for m in ['native','solidity'] for i in range(1,4)]
(ROOT/'plan-stable-seal200.json').write_text(json.dumps(plan,indent=2))
try:
 for case in plan:
  if not D.Run(case):break
finally:
 for s in D.FILES.values():s.close()
 for c in D.CLIENTS.values():c.close()
