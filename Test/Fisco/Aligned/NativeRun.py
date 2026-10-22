"""Ten default FISCO v3.7.3 validators, one controller, native ring transfers.

The benchmark ends at the fixed 17-second guard. Persisted-state audits restart
one validator at a time, without a PBFT quorum, after all benchmark units stop.
These audits never extend the measurement or submit further transactions.
"""
import argparse
import concurrent.futures as CF
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time

from DefaultFormal import Formal, Number, Parallel, Save, Quantile


class Native(Formal):
    def __init__(self, args):
        super().__init__(args)
        self.driver = self.root / 'native-aligned'
        self.sender = self.hosts[0]

    def Java(self, *arguments):
        return [str(self.root/'toolchain/jdk/bin/java'), '-Xms64m', '-Xmx1g',
                '-XX:ActiveProcessorCount=2', '-cp',
                f'{self.driver}/classes:{self.root}/driver/classes:{self.root}/driver/lib/*',
                'NativeAlignedBench', str(self.driver/'config.toml'), *map(str,arguments)]

    def Launch(self, name, rate, users, unit, warm=2, seconds=10):
        directory = self.driver/'runs'/name
        directory.mkdir(parents=True,exist_ok=False)
        command = self.Java('native',rate,warm,seconds,directory/'driver.json',users,
                            directory/'go',5000,'short-budget')
        args = ['systemd-run','--quiet','--unit',unit,'--uid=root','--gid=root',
                '--working-directory='+str(self.driver),'--property=TimeoutStopSec=2',
                '--property=KillMode=control-group','--property=Restart=no','--property=RuntimeMaxSec=700',
                '--property=StandardOutput=append:'+str(directory/'driver.log'),
                '--property=StandardError=append:'+str(directory/'driver.log'),*command]
        subprocess.run(args,check=True,capture_output=True,timeout=15)
        Save(directory/'launch.json',{'command':args,'sender':self.sender})
        return directory

    def RestartAudit(self, host, name, unit):
        return self.Remote(host,f'''import subprocess,json
root='/var/tmp/fisco-experiments/'+{name!r}
args=['systemd-run','--quiet','--unit',{unit!r},'--uid=fisco','--gid=fisco','--working-directory='+root,
 '--property=LimitNOFILE=1024','--property=TimeoutStopSec=5','--property=KillMode=control-group',
 '--property=RuntimeMaxSec=300','--property=StandardOutput=append:'+root+'/audit.log',
 '--property=StandardError=append:'+root+'/audit.log','/opt/fisco/bin/fisco-bcos','-c','config.ini','-g','config.genesis']
r=subprocess.run(args,capture_output=True,text=True,timeout=15);assert r.returncode==0,(r.stdout,r.stderr)
print(json.dumps({{'host':{host!r},'command':args}}))
''')

    def Audit(self, name, out, driver, setup):
        directory=self.driver/'runs'/name
        # All measured node/driver units have stopped. Keep the other nine stopped.
        manifest={}
        for line in (directory/'transaction-hashes.jsonl').open():
            r=json.loads(line);manifest[r['hash'].lower().removeprefix('0x')]=r['index']
        outcomes=(directory/'outcomes.bin').read_bytes()
        expected=[1_000_000_000]*driver['users'];ledger={};confirmed=[];unknown=[];nodes=[]
        for host in self.hosts:
            unit='fisco-audit-'+name
            self.RestartAudit(host,name,unit)
            try:
                deadline=time.monotonic()+25
                while True:
                    try:
                        count=self.Rpc(host,'getTotalTransactionCount')['result'];break
                    except Exception:
                        if time.monotonic()>deadline:raise
                        time.sleep(.15)
                height=Number(count['blockNumber'])
                header=self.Rpc(host,'getBlockByNumber',height,True,True)['result']
                state={'host':host,'count':Number(count['transactionCount']),
                    'failed':Number(count['failedTransactionCount']),'height':height,
                    'hash':header['hash'],'state_root':header['stateRoot']}
                if host==self.sender:
                    total=0
                    with (out/'canonical-blocks.jsonl').open('w') as f:
                        for h in range(height+1):
                            block=self.Rpc(host,'getBlockByNumber',h,False,True)['result']
                            entries=block.get('transactions',block.get('transactionHashes',[]))
                            hashes=[str(v.get('hash',v.get('transactionHash',''))) if isinstance(v,dict) else str(v) for v in entries]
                            total+=len(hashes);ledger[h]={'hash':block['hash'],'transactions':total}
                            f.write(json.dumps({'height':h,'hash':block['hash'],'hashes':hashes,'cumulative':total})+'\n')
                            for txhash in hashes:
                                key=txhash.lower().removeprefix('0x')
                                if key not in manifest:continue
                                i=manifest[key];confirmed.append(i)
                                expected[i%driver['users']]-=1;expected[(i+1)%driver['users']]+=1
                                if outcomes[i]!=1:unknown.append('0x'+key)
                    if total!=state['count'] or len(confirmed)+setup!=state['count']:
                        raise AssertionError('Canonical ledger count differs from setup plus known native requests')
                    # Callback errors are not final rejection. Check every included
                    # transaction whose business-success callback was absent.
                    def Receipt(txhash):
                        r=self.Rpc(host,'getTransactionReceipt',txhash,False)['result']
                        return bool(r and r['transactionHash'].lower()==txhash.lower()
                            and Number(r['status'])==0 and int(r.get('output','-1'),16)==0)
                    with CF.ThreadPoolExecutor(12) as pool:
                        if not all(pool.map(Receipt,unknown)):raise AssertionError('Native receipt business output failed')
                    balance_file=directory/'balances-audit.json'
                    subprocess.run(self.Java('balances',driver['prefix'],driver['users'],balance_file),
                        cwd=self.driver,check=True,capture_output=True,timeout=120)
                    balance=json.loads(balance_file.read_text())
                    mismatches=sum(int(actual)!=wanted for actual,wanted in zip(balance['balances'],expected))
                    if balance['errors'] or len(balance['balances'])!=driver['users'] or mismatches:
                        raise AssertionError('Native balance reconstruction failed')
                    if sum(expected)!=driver['users']*1_000_000_000:
                        raise AssertionError('Native transfer supply is not conserved')
                    Save(out/'balance-audit.json',{'passed':True,'accounts':driver['users'],'mismatches':mismatches,
                        'prefix':driver['prefix'],'expected':expected,'observed':balance['balances'],
                        'confirmed_workload_transactions':len(confirmed),'additional_receipts_checked':len(unknown),
                        'callback_business_success_reused':len(confirmed)-len(unknown),'ledger':state})
                after=self.Rpc(host,'getTotalTransactionCount')['result']
                if after!=count:raise AssertionError('Audit unexpectedly advanced ledger')
                nodes.append(state)
            finally:
                self.Remote(host,f"import subprocess\nr=subprocess.run(['systemctl','stop',{unit!r}],capture_output=True,timeout=15)\nassert r.returncode==0\n")
        agree=len({(n['count'],n['height'],n['hash'],n['state_root'],n['failed']) for n in nodes})==1
        audit={'passed':agree and all(n['failed']==0 for n in nodes),'nodes':nodes,
            'method':'each persisted validator reopened separately without PBFT quorum; one full native balance reconstruction plus identical state roots'}
        Save(out/'state-audit.json',audit)
        return audit,ledger


def Case(c, rate, batch, smoke=False):
    name=f'{batch.name}-r{rate}';out=batch/name;out.mkdir()
    node_unit='fisco-node-'+name;driver_unit='fisco-driver-'+name
    users=4 if smoke else 1024;warm=0 if smoke else 2;seconds=10
    points=[];observations=[];errors=[];timers=[];stopped=[];directory=None;gate=None;ledger={};audit={}
    sender_end=warm+seconds;cutoff=sender_end+5
    c.rpc_log=(out/'rpc.jsonl').open('w',buffering=1)
    stop_lock=threading.Lock()
    def Stop():
        with stop_lock:
            if stopped:return
            records=Parallel(lambda h:c.StopOwned(h,name,node_unit,driver_unit),c.hosts)
            stopped.extend(records);Save(out/'stopped.json',records)
    def StopNodes():
        records=Parallel(lambda h:c.WatchdogStopUnit(h,node_unit),c.hosts)
        Save(out/'node-guard.json',records)
    try:
        Save(out/'node-launches.json',Parallel(lambda h:c.PrepareNode(h,name,node_unit),c.hosts))
        before=c.Ready(30);Save(out/'fresh-chain.json',before)
        if any(Number(n['transactionCount']) for n in before['nodes']):raise AssertionError('Fresh ledger must be empty')
        directory=c.Launch(name,rate,users,driver_unit,warm,seconds)
        deadline=time.monotonic()+600
        while not (directory/'go.ready').exists():
            state=subprocess.run(['systemctl','show',driver_unit,'--property=ActiveState'],capture_output=True,text=True).stdout
            if 'ActiveState=failed' in state or time.monotonic()>deadline:raise RuntimeError('Native driver preparation failed: '+state)
            time.sleep(.25)
        ready=c.Ready(30);setup=Number(ready['nodes'][0]['transactionCount']);Save(out/'setup.json',ready)
        Save(out/'onchain-settings.json',Parallel(lambda h:{'host':h,'tx_count_limit':c.Rpc(h,'getSystemConfigByKey','tx_count_limit')['result']},c.hosts))
        selected=json.loads((directory/'go.samples.json').read_text());Save(out/'sample-plan.json',selected)
        gate=time.time()+2;start=time.monotonic()+gate-time.time();deadline=start+cutoff
        timer=threading.Timer(cutoff+start-time.monotonic(),StopNodes);timer.daemon=True;timer.start();timers.append(timer)
        timer=threading.Timer(cutoff+1+start-time.monotonic(),lambda:c.WatchdogStopUnit(c.sender,driver_unit));timer.daemon=True;timer.start();timers.append(timer)
        (directory/'go.tmp').write_text(str(round(gate*1000)));(directory/'go.tmp').replace(directory/'go')
        Save(out/'timing.json',{'gate_epoch':gate,'gate_monotonic':start,'warmup':warm,'measurement':seconds,
            'send_cutoff':sender_end,'maximum_drain':5,'sender':c.sender,'clock_scope':'sender and observer share one host'})
        # Independent snapshot scheduling preserves explicit 2s/12s endpoints.
        snapshot_pool=CF.ThreadPoolExecutor(3);sample_pool=CF.ThreadPoolExecutor(4)
        snapshots={};pending={};confirmed=set();cursor=0;target=0
        with (out/'chain.jsonl').open('w',buffering=1) as timeline,(out/'observations.jsonl').open('w',buffering=1) as trace:
            while time.monotonic()<deadline:
                now=time.monotonic()
                if target<=sender_end+4 and now>=start+target:
                    snapshots[snapshot_pool.submit(c.SnapshotTimed,deadline)]=target;target+=1
                for future,t in list(snapshots.items()):
                    if not future.done():continue
                    del snapshots[future]
                    try:
                        p=future.result();p['target_seconds']=t;points.append(p);timeline.write(json.dumps(p)+'\n')
                    except Exception as exc:errors.append({'phase':'snapshot','target':t,'error':repr(exc)})
                for future,sample in list(pending.items()):
                    if not future.done():continue
                    del pending[future]
                    try:
                        r=future.result();trace.write(json.dumps(r)+'\n')
                        if r['all_nodes_same_position'] and r['within_observation_budget']:
                            observations.append(r);confirmed.add(sample['hash'])
                    except Exception as exc:errors.append({'phase':'sample','error':repr(exc),'hash':sample['hash']})
                active={s['hash'] for s in pending.values()}
                for _ in range(len(selected)):
                    if len(pending)>=4:break
                    if not selected:break
                    sample=selected[cursor%len(selected)];cursor+=1
                    if sample['hash'] in confirmed or sample['hash'] in active or start+sample['scheduled_offset_ms']/1000>now:continue
                    sample={**sample,'sender':c.sender};pending[sample_pool.submit(c.Observe,sample,deadline)]=sample;active.add(sample['hash'])
                time.sleep(.05)
        snapshot_pool.shutdown(wait=True,cancel_futures=True);sample_pool.shutdown(wait=True,cancel_futures=True)
        while time.monotonic()<deadline+1:time.sleep(.05)
        Stop()
        for timer in timers:timer.join(timeout=30)
        driver=json.loads((directory/'driver.json').read_text());Save(out/'driver.json',driver)
        if smoke and (driver['success']!=10 or driver['failures'] or driver['unresolved']):
            raise AssertionError('Native smoke callbacks failed')
        audit,ledger=c.Audit(name,out,driver,setup)
        endpoints=[next((p for p in points if p['target_seconds']==t),None) for t in [warm,sender_end]]
        proof={'passed':False,'checks':[]}
        if all(endpoints):
            for p in endpoints:
                for n in p['nodes']:
                    anchor=ledger.get(n['height']);match=bool(anchor and anchor['hash']==n['head_hash'] and anchor['transactions']==n['count'])
                    proof['checks'].append({'target':p['target_seconds'],'host':n['host'],'height':n['height'],'matches':match})
            proof['passed']=len(proof['checks'])==20 and all(r['matches'] for r in proof['checks'])
        Save(out/'boundary-proof.json',proof)
        summary={'name':name,'target_tps':rate,'mode':'native','users':users,'amount':1,'routing':'ring',
            'setup_transactions':setup,'actual_send_tps':driver['actual_send_tps'],'submitted':driver['submitted'],
            'callback_success':driver['success'],'callback_errors':driver['failures'],'unresolved':driver['unresolved'],
            'unsubmitted':driver['unsubmitted'],'callback_error_classes':driver['errors'],
            'receipt_p50_ms':driver['p50_ms'],'receipt_p95_ms':driver['p95_ms'],'receipt_p99_ms':driver['p99_ms'],
            'receipt_latency_samples':driver['latency_success_samples'],'native_attribute':driver['signed_transaction_inspection'],
            'state_audit_passed':audit.get('passed',False),'boundary_proof_passed':proof['passed'],
            'sender_placement':'single controller','node_defaults_preserved':True,'errors':errors}
        if proof['passed']:
            a,b=endpoints;delta=b['count_min']-a['count_min']
            lo=b['bracket_begin_monotonic']-a['bracket_end_monotonic'];hi=b['bracket_end_monotonic']-a['bracket_begin_monotonic']
            summary.update(tps=delta/((lo+hi)/2),tps_acquisition_bounds=[delta/hi,delta/lo],
                committed_delta=delta,measurement_seconds=(lo+hi)/2)
        lat=[];after_send=[]
        sent={r['hash']:r for r in driver['sampled_transactions']}
        for r in observations:
            offset=sent[r['hash']].get('sent_offset_ms')
            lat.append((r['observed_epoch']-driver['start_epoch_ms']/1000)*1000-r['scheduled_offset_ms'])
            if offset is not None:after_send.append((r['observed_epoch']-driver['start_epoch_ms']/1000)*1000-offset)
        summary.update(all_node_samples=len(observations),planned_samples=len(selected),
            all_node_scheduled_p50_ms=Quantile(lat,.5),all_node_scheduled_p99_ms=Quantile(lat,.99),
            all_node_after_send_p50_ms=Quantile(after_send,.5),all_node_after_send_p99_ms=Quantile(after_send,.99),
            all_node_latency_scope='query observation upper latency on the same controller clock; not pure PBFT execution time')
        if not summary['state_audit_passed']:raise AssertionError('Persisted validators do not agree')
    except Exception as exc:
        errors.append({'phase':'case','error':repr(exc)});summary={'name':name,'target_tps':rate,'status':'failed','errors':errors}
    finally:
        Stop()
        for timer in timers:timer.cancel();timer.join(timeout=30)
        # Evidence and audits precede guarded removal of only this run's databases.
        cleanup=[]
        for host in c.hosts:
            try:
                evidence=c.Evidence(host,name,node_unit,driver_unit,out)
                removed=c.CleanupData(host,name,node_unit,driver_unit)
                cleanup.append({'host':host,'evidence':evidence,'cleanup':removed})
            except Exception as exc:cleanup.append({'host':host,'error':repr(exc)})
        Save(out/'cleanup.json',cleanup)
        c.rpc_log.close();c.rpc_log=None
        summary['cleanup_ok']=all('error' not in r for r in cleanup)
        summary.setdefault('status','complete');Save(out/'summary.json',summary)
        print(json.dumps(summary),flush=True)
    return summary


def Main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default='/opt/fisco-control');p.add_argument('--hosts',required=True)
    p.add_argument('--generated',default='default-v3.7.3');p.add_argument('--key',default='/root/.ssh/chain_ansible_ed25519')
    p.add_argument('--known-hosts',default='/opt/chain-control/known_hosts');p.add_argument('--name',required=True)
    p.add_argument('--smoke',action='store_true');args=p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',args.name):raise ValueError('Safe unique batch name required')
    c=Native(args);batch=c.root/'results'/args.name;batch.mkdir(exist_ok=False)
    Save(batch/'protocol.json',{'nodes':10,'sender':c.sender,'native_precompiled':'0x100c','accounts':1024,
        'initial_balance':1_000_000_000,'amount':1,'routing':'ring','warmup':2,'measurement':10,'drain':5,
        'rates':[20000,30000,40000,50000,60000],'node_defaults_preserved':True,'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    rows=[]
    try:
        Save(batch/'defaults-stopped.json',c.SetDefaults('stop'))
        for rate in ([1] if args.smoke else [20000,30000,40000,50000,60000]):
            r=Case(c,rate,batch,args.smoke);rows.append(r);Save(batch/'results.json',rows)
            if not r['cleanup_ok'] or r['status']!='complete':break
    finally:Save(batch/'defaults-restored.json',c.SetDefaults('start'))
    if any(r.get('status')!='complete' or not r.get('state_audit_passed') for r in rows):raise SystemExit(2)


if __name__=='__main__':Main()
