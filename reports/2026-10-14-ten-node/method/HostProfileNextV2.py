"""Low-rate host profiling alongside the ten-node throughput experiment."""
import concurrent.futures,getpass,json,paramiko,shlex,time,sys
from pathlib import Path
HOSTS=['10.206.0.16','10.206.0.4','10.206.0.14','10.206.0.17','10.206.0.15','10.206.0.12','10.206.0.2','10.206.0.6','10.206.0.11','10.206.0.8']
ROOT=Path('/home/ubuntu/chain/results/throughput-host-profile-20261014-v2');ROOT.mkdir(exist_ok=False)
CODE=r'''
import json,time,psutil
from pathlib import Path
previous=None;stop=Path('/home/ubuntu/chain/results/throughput-host-profile-20261014-v2.stop')
for i in range(7200):
    if stop.exists():break
    now=time.time();cpu=psutil.cpu_times();disk=psutil.disk_io_counters(perdisk=True).get('vda')
    processes=[]
    for p in psutil.process_iter(['pid','exe','cpu_times','memory_info']):
        if '/chain/releases/' in (p.info['exe'] or '') and (p.info['exe'] or '').endswith('/build/boost'):
            try:
                processes.append({'pid':p.pid,'binary':p.info['exe'],'cpu_seconds':p.info['cpu_times'].user+p.info['cpu_times'].system,
                    'rss':p.info['memory_info'].rss,'threads':[{'id':t.id,'cpu_seconds':t.user_time+t.system_time,
                    'wait':Path(f'/proc/{p.pid}/task/{t.id}/wchan').read_text().strip()} for t in p.threads()]})
            except (psutil.Error,FileNotFoundError):pass
    sample={'utc_seconds':now,'cpu':cpu._asdict(),'disk':disk._asdict() if disk else {},'nodes':processes}
    if previous:
        totals={key:sample['cpu'].get(key,0)-previous['cpu'].get(key,0) for key in ['user','nice','system','idle','iowait','irq','softirq','steal']}
        total=sum(totals.values());sample['cpu_percent']={key:100*value/total if total else 0 for key,value in totals.items()}
        writes=sample['disk'].get('write_count',0)-previous['disk'].get('write_count',0)
        elapsed=sample['disk'].get('write_time',0)-previous['disk'].get('write_time',0)
        sample['write_await_ms']=elapsed/writes if writes else None
        sample['disk_util_percent']=100*(sample['disk'].get('busy_time',0)-previous['disk'].get('busy_time',0))/(1000*(now-previous['utc_seconds']))
    print(json.dumps(sample),flush=True);previous=sample;time.sleep(1)
'''
password=getpass.getpass('CLUSTER_PASSWORD: ') if sys.stdin.isatty() else sys.stdin.readline().rstrip();clients={}
try:
    for host in HOSTS:
        c=paramiko.SSHClient();c.load_system_host_keys();c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host,username='ubuntu',password=password,allow_agent=False,look_for_keys=False,timeout=15);c.get_transport().set_keepalive(15);clients[host]=c
    password=None
    def Sample(index):
        host=HOSTS[index];_,out,err=clients[host].exec_command('/home/ubuntu/chain/releases/2026-09-30-d4106d1/.venv/bin/python -u -c '+shlex.quote(CODE),timeout=7400)
        with (ROOT/f'node{index+1:02}.jsonl').open('w') as file:
            for i,line in enumerate(out):
                file.write(line);file.flush()
                if index==0 and i%60==0:print(json.dumps({'host_profile_samples':i}),flush=True)
        code=out.channel.recv_exit_status();assert code==0,err.read()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:list(pool.map(Sample,range(10)))
finally:
    password=None
    for c in clients.values():c.close()
