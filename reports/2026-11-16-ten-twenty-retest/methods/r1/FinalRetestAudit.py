import json,sys,shlex
from pathlib import Path
from types import SimpleNamespace
import tps_runner as t

ROOT=Path('/opt/chain/deploy-tools/20261116-retest1')
RELEASE=Path('/opt/chain/releases')/t.COMMIT
CODE=r'''
import json,os,socket,subprocess,sys
from pathlib import Path
request=json.load(sys.stdin)
pid=int(subprocess.check_output(['systemctl','show','chain-node.service','-p','MainPID','--value'],text=True))
base=Path('/proc')/str(pid)
argv=[x.decode() for x in (base/'cmdline').read_bytes().split(b'\0') if x]
flags=dict(zip(argv[1::2],argv[2::2]))
inodes=set()
for p in (base/'fd').iterdir():
 try:
  target=os.readlink(p)
  if target.startswith('socket:['):inodes.add(target[8:-1])
 except FileNotFoundError:pass
neighbors=set()
for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
 fields=line.split()
 if fields[3]!='01' or fields[9] not in inodes:continue
 local,remote=fields[1].split(':'),fields[2].split(':')
 if int(local[1],16)!=8089 and int(remote[1],16)!=8089:continue
 ip=socket.inet_ntoa(bytes.fromhex(remote[0])[::-1])
 if ip in request['ips'] and ip!=request['bind']:neighbors.add(ip)
boost=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:
  if (p/'comm').read_text().strip()=='boost':boost.append(int(p.name))
 except FileNotFoundError:pass
enabled=subprocess.run(['systemctl','is-enabled','chain-node.service'],capture_output=True,text=True).stdout.strip()
timer=subprocess.run(['systemctl','is-active','chain-retest1-20261116-recover.timer'],capture_output=True,text=True).stdout.strip()
dbs=[str(p) for p in Path('/var/tmp/chain-retest1-20261116').glob('*/'+request['name']+'/db')]
data=flags.get('--data','')
checks={'exactly_one_service_boost':boost==[pid],'all_19_distinct_neighbors':len(neighbors)==19,'enabled':enabled=='enabled','watchdog_inactive':timer!='active','no_test_databases':not dbs,'permanent_data_path':data.startswith('/var/lib/chain/deploy-9d34b55')}
print(json.dumps({'passed':all(checks.values()),'checks':checks,'pid':pid,'data':data,'neighbors':sorted(neighbors),'remaining_test_databases':dbs,'watchdog':timer}))
'''

def main():
    t.SECRET=sys.stdin.readline().rstrip('\r\n')
    t.Require(bool(t.SECRET),'Empty credential input')
    nodes=t.Rows(ROOT/'inventory.json')
    args=SimpleNamespace(known_hosts=ROOT/'known_hosts',ssh_user='root',remote_python='/opt/chain/deploy-tools/20261116-9d34b55/worker-python',release=RELEASE,run_names=[],remote_root='/var/tmp/chain-retest1-20261116')
    remote=t.Remotes(nodes,args)
    ids={n['id']:n for n in nodes}
    ips=[n['host'] for n in nodes]
    def check(name):
        facts=remote.Facts(name)
        request={'name':name,'bind':ids[name]['host'],'ips':ips}
        extra=json.loads(remote.Run(name,shlex.join([args.remote_python,'-c',CODE]),request)['stdout'])
        return {'facts':facts,'audit':extra}
    try:
        values,errors=remote.All(check)
        result={'passed':not errors and len(values)==20 and all(v['audit']['passed'] for v in values.values()),'nodes':values,'errors':errors}
        t.Save(ROOT/'final-audit.json',result)
        print(json.dumps({'passed':result['passed'],'nodes':len(values),'errors':errors}))
        return 0 if result['passed'] else 1
    finally:
        remote.Close();t.SECRET=None

if __name__=='__main__':sys.exit(main())
