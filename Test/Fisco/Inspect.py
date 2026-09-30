"""Collect benchmark evidence; optionally stop only this benchmark's processes."""
import argparse,concurrent.futures,getpass,json,shlex
from Deploy import ROOT,HOSTS,Connect,Remote

def Collect(host,password,stop=False):
    c=Connect(host,password)
    try:
        dest=ROOT/'collected'/host;dest.mkdir(parents=True,exist_ok=True)
        if stop:
            script=f'''import os,signal,time,pathlib
root={str(ROOT)!r}
pathlib.Path(root+'/sample.stop').touch()
targets=[]
for p in pathlib.Path('/proc').glob('[0-9]*'):
 try:
  args=(p/'cmdline').read_bytes().split(b'\\0')
  owned=any(a.startswith(root.encode()+b'/') for a in args)
  node=bool(args and args[0].endswith(b'/fisco-bcos'))
  driver=b'FiscoBench' in args
  if owned and (node or driver):
   targets.append((p,args));os.kill(int(p.name),signal.SIGTERM)
 except OSError:pass
deadline=time.monotonic()+10
while any(p.exists() for p,a in targets) and time.monotonic()<deadline:time.sleep(.2)
for p,args in targets:
 try:
  if (p/'cmdline').read_bytes().split(b'\\0')==args:os.kill(int(p.name),signal.SIGKILL)
 except OSError:pass
time.sleep(1)
print('stopped',len(targets),'benchmark processes')
'''
            (dest/'stop.txt').write_text(Remote(c,'python3 -c '+shlex.quote(script)))
            verify=f'''import pathlib,json
root={str(ROOT)!r}.encode()+b'/'
alive=[]
for p in pathlib.Path('/proc').glob('[0-9]*'):
 try:
  args=(p/'cmdline').read_bytes().split(b'\\0')
  owned=any(a.startswith(root) for a in args)
  target=bool(args and args[0].endswith(b'/fisco-bcos')) or b'FiscoBench' in args or any(a==root+b'NodeSample.py' for a in args)
  if owned and target:alive.append(int(p.name))
 except OSError:pass
print(json.dumps({{'owned_processes':alive}}))
'''
            (dest/'cleanup.json').write_text(Remote(c,'python3 -c '+shlex.quote(verify)))
        with c.open_sftp() as s:s.get(str(ROOT/'resources.jsonl'),str(dest/'resources.jsonl'))
        status=Remote(c,f'''df -B1 {ROOT}
ps -ww -C fisco-bcos,java -o pid,pcpu,rss,nlwp,args
ss -lnt 'sport = :20200 or sport = :30300'
chronyc tracking
sha256sum {ROOT}/runtime-stable/fisco-bcos {ROOT}/runtime-stable/node0/config.genesis {ROOT}/driver-stable/classes/FiscoBench.class {ROOT}/driver-stable/FiscoBench.java
tail -n 30 {ROOT}/runtime-stable/node0/log/*log
''')
        (dest/'status.txt').write_text(status)
        return host
    finally:c.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--stop',action='store_true');a=p.parse_args()
    password=getpass.getpass('CLUSTER_PASSWORD: ')
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        print(list(pool.map(lambda h:Collect(h,password,a.stop),HOSTS)))
