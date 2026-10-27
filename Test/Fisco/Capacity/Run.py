"""Ten-validator native transfer capacity experiments, isolated configurations."""
import argparse
import concurrent.futures as CF
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'Aligned'))
from NativeRun import Native
from DefaultFormal import Number, Parallel, Save, Quantile


PROFILES = {
    'parallel100': dict(serial=False, seal=100, block=10000),
    'parallel50': dict(serial=False, seal=50, block=6000),
    'parallel200': dict(serial=False, seal=200, block=20000),
    'serial100': dict(serial=True, seal=100, block=10000),
    'serial50': dict(serial=True, seal=50, block=6000),
    'serial200': dict(serial=True, seal=200, block=20000),
    'serial100tree': dict(serial=True, seal=100, block=10000, tree=True),
    'serial100verify2': dict(serial=True, seal=100, block=10000, verify=2),
    'serial50lean': dict(serial=True, seal=50, block=6000, return_input=False),
}


def CompactCompleted(out, merged):
    """Losslessly retain large completed-run text records without exhausting controller disk."""
    records=[]
    for root in [out,merged]:
        boundary=root.resolve()
        for path in sorted(root.rglob('*.jsonl')):
            if path.is_symlink() or not path.resolve().is_relative_to(boundary):
                raise RuntimeError('Unexpected evidence path')
            if path.stat().st_size<1024*1024:continue
            target=path.with_suffix(path.suffix+'.gz')
            if target.exists():raise RuntimeError('Compressed evidence already exists')
            digest=hashlib.sha256();size=path.stat().st_size
            with path.open('rb') as source,gzip.open(target,'wb',compresslevel=1) as dest:
                for data in iter(lambda:source.read(1024*1024),b''):
                    digest.update(data);dest.write(data)
            verify=hashlib.sha256()
            with gzip.open(target,'rb') as source:
                for data in iter(lambda:source.read(1024*1024),b''):verify.update(data)
            if digest.digest()!=verify.digest():raise RuntimeError('Compressed evidence checksum mismatch')
            records.append(dict(path=str(path),compressed=str(target),bytes=size,
                                compressed_bytes=target.stat().st_size,sha256=digest.hexdigest()))
            path.unlink()
    Save(out/'compressed-records.json',records)


class Capacity(Native):
    def __init__(self, args):
        super().__init__(args)
        self.capacity = self.root/'capacity'
        self.driver = self.capacity/'merged'
        self.driver.mkdir(exist_ok=True)
        (self.driver/'runs').mkdir(exist_ok=True)
        (self.driver/'config.toml').write_text((self.root/'native-aligned/config.toml').read_text())
        self.profile = None

    def Remote(self, host, script):
        r = subprocess.run(['ssh', *self.ssh_options, 'root@'+host, 'python3 -'],
                           input=script, text=True, capture_output=True, timeout=300)
        if r.returncode:
            raise RuntimeError(host+': '+r.stdout+r.stderr)
        return r.stdout

    def Download(self, host, source, destination):
        for attempt in range(3):
            try:return super().Download(host,source,destination)
            except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
                if attempt==2:
                    detail=getattr(exc,'stderr','')
                    raise RuntimeError(f'Download {host}:{source} failed: {detail}') from exc
                time.sleep(.5*(attempt+1))

    def Java(self, *arguments, config=None):
        return [str(self.root/'toolchain/jdk/bin/java'), '-Xms64m', '-Xmx512m',
                '-XX:ActiveProcessorCount=1',
                '-Dlog4j.configurationFile='+str(self.capacity/'sdk-log4j2.xml'), '-cp',
                f'{self.capacity}/classes:{self.root}/driver/classes:{self.root}/driver/lib/*',
                'NativeAlignedBench', str(config or self.driver/'config.toml'), *map(str, arguments)]

    def PrepareNode(self, host, name, unit):
        script = f'''import pathlib,shutil,os,pwd,grp,hashlib,json,subprocess,configparser
base=pathlib.Path('/var/tmp/fisco-experiments');assert not base.is_symlink();base.mkdir(exist_ok=True)
root=base/{name!r};assert root.parent.resolve()==base.resolve() and not root.exists()
root.mkdir(mode=0o750);source=pathlib.Path('/opt/fisco/nodes/node0')
for file in ['config.ini','config.genesis','nodes.json']:shutil.copy2(source/file,root/file)
shutil.copytree(source/'conf',root/'conf')
p={self.profile!r}
def update(file,changes):
 c=configparser.ConfigParser();c.read(root/file)
 for section,values in changes.items():
  if not c.has_section(section):c.add_section(section)
  for key,value in values.items():c.set(section,key,str(value))
 with (root/file).open('w') as f:c.write(f)
update('config.ini',{{'consensus':{{'min_seal_time':p['seal']}},'txpool':{{'limit':50000,'verify_worker_num':p.get('verify',4),'notify_worker_num':2}},'rpc':{{'thread_count':4,'return_input_params':str(p.get('return_input',True)).lower()}},'log':{{'level':'warning'}},'executor':{{'enable_dag':'true'}},'sync':{{'send_txs_by_tree':str(p.get('tree',False)).lower()}}}})
update('config.genesis',{{'consensus':{{'block_tx_count_limit':p['block'],'leader_period':5}},'executor':{{'is_serial_execute':str(p['serial']).lower()}}}})
if p.get('block_limit'):update('config.genesis',{{'chain':{{'block_limit':p['block_limit']}}}})
uid=pwd.getpwnam('fisco').pw_uid;gid=grp.getgrnam('fisco').gr_gid
for path in [root,*root.rglob('*')]:os.chown(path,uid,gid)
manifest={{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in ['config.ini','config.genesis','nodes.json']}}
(root/'input-manifest.json').write_text(json.dumps(manifest))
command=['systemd-run','--quiet','--unit',{unit!r},'--uid=fisco','--gid=fisco','--working-directory='+str(root),'--property=LimitNOFILE=65536','--property=TimeoutStopSec=10','--property=KillMode=control-group','--property=RuntimeMaxSec=1800','--setenv=RUST_LOG=bcos_wasm=error','--property=StandardOutput=append:'+str(root/'stdout.log'),'--property=StandardError=append:'+str(root/'stderr.log'),'/opt/fisco/bin/fisco-bcos','-c','config.ini','-g','config.genesis']
subprocess.run(command,check=True,capture_output=True,timeout=20)
subprocess.run(['systemd-run','--quiet','--unit','fisco-sample-'+{name!r},'--property=RuntimeMaxSec=1800','python3','/opt/fisco-control/capacity/Sample.py',{name!r},str(root/'resources.jsonl')],check=True,capture_output=True)
print(json.dumps({{'host':{host!r},'profile':p,'files':manifest,'command':command}}))
'''
        return json.loads(self.Remote(host, script))

    def LaunchShard(self, host, shard, name, rate, warm, seconds, inflight):
        directory = self.capacity/'runs'/name
        command = self.Java('native', rate//10, warm, seconds, directory/'driver.json',
                            1024, directory/'go', inflight, 'short-budget', config=directory/'config.toml')
        return self.Remote(host, f'''import pathlib,subprocess,json,re
root=pathlib.Path({str(directory)!r});root.mkdir(parents=True,exist_ok=False)
source=pathlib.Path('/opt/fisco-control/driver/config.toml').read_text()
source=re.sub(r'(?m)^peers\\s*=.*$', 'peers = ["{host}:20200"]', source)
source=re.sub(r'(?m)^threadPoolSize\\s*=.*$', 'threadPoolSize = "2"', source)
(root/'config.toml').write_text(source)
args=['systemd-run','--quiet','--unit','fisco-driver-'+{name!r},'--property=TimeoutStopSec=5','--property=KillMode=control-group','--property=RuntimeMaxSec=1800','--setenv=FISCO_BENCH_SHARDS=10','--setenv=FISCO_BENCH_SHARD={shard}','--setenv=FISCO_BENCH_PREFIX='+{name!r},'--setenv=FISCO_BENCH_BLOCK_LIMIT_OFFSET={self.profile.get('sign_limit_offset',0)}','--property=StandardOutput=append:'+str(root/'driver.log'),'--property=StandardError=append:'+str(root/'driver.log'),*{command!r}]
subprocess.run(args,check=True,capture_output=True,timeout=20)
print(json.dumps({{'host':{host!r},'shard':{shard},'command':args}}))
''')

    def Gather(self, name, out):
        directory = self.driver/'runs'/name
        directory.mkdir(exist_ok=False)
        def One(host):
            local = out/host;local.mkdir(exist_ok=True)
            for file in ['driver.json','transaction-hashes.jsonl','outcomes.bin','driver.log']:
                self.Download(host, str(self.capacity/'runs'/name/file), local/file)
            return json.loads((local/'driver.json').read_text())
        rows = Parallel(One, self.hosts)
        total = sum(r['requested'] for r in rows)
        outcomes = bytearray(total)
        with (directory/'transaction-hashes.jsonl').open('w') as dest:
            for shard, host in enumerate(self.hosts):
                part=(out/host/'outcomes.bin').read_bytes()
                assert len(part)==rows[shard]['requested']
                outcomes[shard::10]=part
                with (out/host/'transaction-hashes.jsonl').open() as stream:
                    for line in stream:dest.write(line)
        (directory/'outcomes.bin').write_bytes(outcomes)
        result={'requested':total,'users':1024,'prefix':name,'shards':rows}
        Save(out/'drivers.json',rows)
        return result

    def Evidence(self, host, name, node_unit, driver_unit, out):
        # Base helper archives driver runs from self.driver; the actual sharded path is capacity.
        previous=self.driver
        self.driver=self.capacity
        try:return super().Evidence(host,name,node_unit,driver_unit,out)
        finally:self.driver=previous


def Case(c, plan, batch):
    name=batch.name+'-'+plan['profile']+'-'+str(plan['rate'])+'-r'+str(plan.get('repeat',1))
    out=batch/name;out.mkdir()
    c.profile=dict(PROFILES[plan['profile']])
    for key in ['block_limit','sign_limit_offset']:
        if key in plan:c.profile[key]=plan[key]
    rate=plan['rate'];warm=plan.get('warm',2);seconds=plan.get('seconds',10)
    end=warm+seconds;cutoff=end+5
    node_unit='fisco-node-'+name;driver_unit='fisco-driver-'+name
    c.rpc_log=(out/'rpc.jsonl').open('w',buffering=1)
    points=[];errors=[];timers=[];driver=None;summary={'name':name,'plan':plan,'profile':c.profile}
    start=None
    def StopNodes():
        Save(out/'guard.json',Parallel(lambda h:c.WatchdogStopUnit(h,node_unit),c.hosts))
    try:
        if shutil.disk_usage(batch).free<2*1024**3:raise RuntimeError('Controller free disk below 2 GiB reserve')
        Save(out/'node-launches.json',Parallel(lambda h:c.PrepareNode(h,name,node_unit),c.hosts))
        fresh=c.Ready(60);assert all(Number(n['transactionCount'])==0 for n in fresh['nodes'])
        Save(out/'fresh.json',fresh)
        launches=Parallel(lambda v:c.LaunchShard(v[1],v[0],name,rate,warm,seconds,plan.get('inflight',3000)),list(enumerate(c.hosts)))
        Save(out/'driver-launches.json',launches)
        def Wait(host):
            return c.Remote(host,f'''import pathlib,time,subprocess,json
root=pathlib.Path('/opt/fisco-control/capacity/runs')/{name!r}
deadline=time.monotonic()+1000
while not (root/'go.ready').exists():
 state=subprocess.run(['systemctl','show','fisco-driver-'+{name!r},'--property=ActiveState'],capture_output=True,text=True).stdout
 if 'ActiveState=failed' in state or time.monotonic()>deadline:raise RuntimeError(state+' '+(root/'driver.log').read_text()[-2000:])
 time.sleep(.5)
print(json.dumps({{'host':{host!r},'ready':True,'prepared':json.loads((root/'prepared.json').read_text())}}))
''')
        # Remote calls are bounded; preparation normally completes well within five minutes.
        prepared=[json.loads(v) for v in Parallel(Wait,c.hosts)]
        Save(out/'prepared.json',prepared)
        ready=c.Ready(60);setup=Number(ready['nodes'][0]['transactionCount']);assert setup==1024
        if plan.get('sign_limit_offset'):
            heights=[Number(n['blockNumber']) for n in ready['nodes']]
            limits=[Number(v['prepared']['signed_transaction_inspection']['blockLimit']) for v in prepared]
            assert min(limits)>max(heights)+(warm+seconds+5)*1000/c.profile['seal']+100,'Signing horizon too short'
        Save(out/'setup.json',ready)
        Save(out/'system-settings.json',Parallel(lambda h:{'host':h,'block_limit':c.Rpc(h,'getSystemConfigByKey','tx_count_limit')['result']},c.hosts))
        gate=time.time()+4;start=time.monotonic()+gate-time.time()
        Save(out/'timing.json',dict(gate_epoch=gate,warm=warm,seconds=seconds,drain=5))
        Parallel(lambda h:c.Remote(h,f"from pathlib import Path\np=Path('/opt/fisco-control/capacity/runs/{name}/go')\np.with_suffix('.tmp').write_text('{round(gate*1000)}')\np.with_suffix('.tmp').replace(p)\n"),c.hosts)
        timer=threading.Timer(max(0,start+cutoff-time.monotonic()),StopNodes);timer.daemon=True;timer.start();timers.append(timer)
        pool=CF.ThreadPoolExecutor(3);pending={};target=0
        with (out/'chain.jsonl').open('w',buffering=1) as f:
            while time.monotonic()<start+cutoff:
                if target<=end+4 and time.monotonic()>=start+target:
                    pending[pool.submit(c.SnapshotTimed,start+cutoff)]=target;target+=1
                for future,t in list(pending.items()):
                    if future.done():
                        del pending[future]
                        try:
                            row=future.result();row['target_seconds']=t;points.append(row);f.write(json.dumps(row)+'\n')
                        except Exception as exc:errors.append({'target':t,'error':repr(exc)})
                time.sleep(.02)
        pool.shutdown(wait=True,cancel_futures=True)
        for timer in timers:timer.join(30)
        # Give result serialization a bounded allowance after measured callbacks freeze at end+5.
        limit=time.monotonic()+20
        while time.monotonic()<limit:
            states=Parallel(lambda h:c.Remote(h,f"from pathlib import Path\nprint(Path('/opt/fisco-control/capacity/runs/{name}/driver.json').exists())\n").strip(),c.hosts)
            if all(v=='True' for v in states):break
            time.sleep(.5)
        Save(out/'stopped.json',Parallel(lambda h:c.StopOwned(h,name,node_unit,driver_unit),c.hosts))
        driver=c.Gather(name,out)
        audit,ledger=c.Audit(name,out,driver,setup)
        endpoints=[next((p for p in points if p['target_seconds']==t),None) for t in [warm,end]]
        proof=[]
        for p in endpoints:
            if p:
                for n in p['nodes']:
                    anchor=ledger.get(n['height'],{})
                    proof.append(anchor.get('hash')==n['head_hash'] and anchor.get('transactions')==n['count'])
        valid=len(proof)==20 and all(proof) and audit['passed']
        acquisition_valid=all(p and 0<=p['bracket_end_monotonic']-p['bracket_begin_monotonic']<=.5 for p in endpoints)
        valid=valid and acquisition_valid
        Save(out/'boundary-proof.json',{'passed':valid,'checks':proof,'acquisition_valid':acquisition_valid})
        rows=driver['shards'];hist={};callback_errors={}
        for r in rows:
            for k,v in r['latency_histogram_ms'].items():hist[int(k)]=hist.get(int(k),0)+v
            for k,v in r['errors'].items():callback_errors[k]=callback_errors.get(k,0)+v
        def P(q):
            total=sum(hist.values());seen=0
            for key in sorted(hist):
                seen+=hist[key]
                if total and seen>=q*total:return key
        summary.update(status='complete',state_audit_passed=audit['passed'],all_heads_equal=audit['all_heads_equal'],
            boundary_proof_passed=valid,actual_send_tps=sum(r['actual_send_tps'] for r in rows),
            callback_success=sum(r['success'] for r in rows),callback_errors=callback_errors,
            submitted=sum(r['submitted'] for r in rows),unsubmitted=sum(r['unsubmitted'] for r in rows),
            unresolved=sum(r['unresolved'] for r in rows),receipt_p50_ms=P(.5),receipt_p95_ms=P(.95),receipt_p99_ms=P(.99),
            latency_samples=sum(hist.values()),receipt_histogram_ms=hist,
            sender_start_spread_ms=max(r['start_epoch_ms'] for r in rows)-min(r['start_epoch_ms'] for r in rows),
            errors=errors)
        if valid:
            a,b=endpoints;delta=b['count_min']-a['count_min']
            lo=b['bracket_begin_monotonic']-a['bracket_end_monotonic'];hi=b['bracket_end_monotonic']-a['bracket_begin_monotonic']
            summary.update(tps=delta/((lo+hi)/2),tps_bounds=[delta/hi,delta/lo],measurement_seconds=(lo+hi)/2,
                pending_start=a['pending_mean'],pending_end=b['pending_mean'],pending_growth_per_s=(b['pending_mean']-a['pending_mean'])/seconds)
            summary['window_screen_passed']=bool(seconds>=60 and summary['actual_send_tps']>=.99*rate and summary['tps']>=.99*rate
                and not callback_errors and summary['unresolved']==0 and summary['unsubmitted']==0 and audit['all_heads_equal']
                and summary['pending_growth_per_s']<=max(10,.001*rate))
            summary['capacity_claim']='Single-window screen only; repeated runs and queue time series must also be evaluated.'
        print(json.dumps({k:summary.get(k) for k in ['name','tps','actual_send_tps','receipt_p95_ms','unresolved','window_screen_passed']}),flush=True)
    except Exception as exc:
        summary.update(status='failed',error=repr(exc),errors=errors)
        print(json.dumps(summary),flush=True)
    finally:
        for timer in timers:timer.cancel();timer.join(30)
        Save(out/'final-stopped.json',Parallel(lambda h:c.StopOwned(h,name,node_unit,driver_unit),c.hosts))
        Parallel(lambda h:c.Remote(h,f"import subprocess\nsubprocess.run(['systemctl','stop','fisco-sample-{name}'],check=True,capture_output=True)\n"),c.hosts)
        cleanup=[]
        for h in c.hosts:
            try:
                evidence=c.Evidence(h,name,node_unit,driver_unit,out)
                cleanup.append({'host':h,'evidence':evidence,'cleanup':c.CleanupData(h,name,node_unit,driver_unit)})
            except Exception as exc:cleanup.append({'host':h,'error':repr(exc)})
        Save(out/'cleanup.json',cleanup);summary['cleanup_ok']=all('error' not in v for v in cleanup)
        c.rpc_log.close();c.rpc_log=None;Save(out/'summary.json',summary)
    return summary


def Main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default='/opt/fisco-control');p.add_argument('--hosts',required=True)
    p.add_argument('--generated',default='default-v3.7.3');p.add_argument('--key',default='/root/.ssh/chain_ansible_ed25519')
    p.add_argument('--known-hosts',default='/opt/chain-control/known_hosts');p.add_argument('--name',required=True)
    p.add_argument('--plan',type=Path,required=True);args=p.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',args.name):raise ValueError('Unique safe run name required')
    plan=json.loads(args.plan.read_text());assert plan
    for case in plan:
        assert case['profile'] in PROFILES and case['rate']>0 and case['rate']%10==0
        assert 0<=case.get('warm',2)<=10 and 1<=case.get('seconds',10)<=60
        if case.get('sign_limit_offset'):
            assert 0<case['sign_limit_offset']<case.get('block_limit',1000)<=5000
    c=Capacity(args);batch=c.root/'results'/args.name;batch.mkdir(exist_ok=False)
    source_dir=batch/'sources';source_dir.mkdir()
    source_manifest={}
    for file in [Path(__file__),Path(__file__).with_name('Sample.py'),c.capacity/'NativeAlignedBench.java',
                 *[Path(__file__).parents[1]/'Aligned'/f for f in ['NativeRun.py','DefaultFormal.py','DriverStage.py']]]:
        content=file.read_bytes();(source_dir/file.name).write_bytes(content)
        source_manifest[file.name]=hashlib.sha256(content).hexdigest()
    source_manifest['NativeAlignedBench.class']=hashlib.sha256((c.capacity/'classes/NativeAlignedBench.class').read_bytes()).hexdigest()
    Save(batch/'protocol.json',dict(plan=plan,profiles=PROFILES,accounts=1024,nodes=10,senders=10,
        source_manifest=source_manifest,
        workload='0x100c userTransfer; shared 1024-account ring; amount 1',binary='official 3.7.3',
        receipt_latency='actual SDK call to business-success callback; monotonic per sender; censored samples reported',
        timing='controller common-chain endpoint brackets; 5 second fixed drain; signatures prepared before gate'))
    rows=[]
    try:
        Save(batch/'services-before.json',Parallel(lambda h:c.Remote(h,"import subprocess\nprint(subprocess.run(['systemctl','is-active','chain-node','fisco-bcos'],capture_output=True,text=True).stdout)\n"),c.hosts))
        Save(batch/'services-stopped.json',Parallel(lambda h:c.Remote(h,"import subprocess\nsubprocess.run(['systemctl','stop','chain-node','fisco-bcos'],check=True,capture_output=True,timeout=30)\n"),c.hosts))
        for case in plan:
            row=Case(c,case,batch);rows.append(row);Save(batch/'results.json',rows)
            if case.get('compact_records') and row['status']=='complete' and row['cleanup_ok']:
                CompactCompleted(batch/row['name'],c.driver/'runs'/row['name'])
            if row['status']!='complete' or not row['cleanup_ok']:break
    finally:
        Save(batch/'services-restored.json',Parallel(lambda h:c.Remote(h,"import subprocess\nsubprocess.run(['systemctl','start','chain-node','fisco-bcos'],check=True,capture_output=True,timeout=30)\nprint(subprocess.run(['systemctl','is-active','chain-node','fisco-bcos'],capture_output=True,text=True).stdout)\n"),c.hosts))
    if len(rows)!=len(plan) or any(r['status']!='complete' or not r.get('boundary_proof_passed') or not r['cleanup_ok'] for r in rows):raise SystemExit(2)


if __name__=='__main__':Main()
