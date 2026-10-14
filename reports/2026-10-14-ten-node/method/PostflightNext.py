"""Check released node identities and disk space after all experiments."""
import concurrent.futures,getpass,json,paramiko,shlex
from pathlib import Path
ROOT=Path('/home/ubuntu/chain');HOSTS=['10.206.0.16','10.206.0.4','10.206.0.14','10.206.0.17','10.206.0.15','10.206.0.12','10.206.0.2','10.206.0.6','10.206.0.11','10.206.0.8']
CODE='''
import hashlib,json,psutil,shutil,subprocess
from pathlib import Path
root=Path('/home/ubuntu/chain');binary=root/'releases/2026-10-14-tps-v2/build/boost'
with binary.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
processes=[{'pid':p.pid,'exe':p.info['exe']} for p in psutil.process_iter(['exe']) if (p.info['exe'] or '').endswith('/build/boost')]
print(json.dumps({'sha256':digest,'processes':processes,'free_gib':shutil.disk_usage(root).free/2**30,
    'port_8089':subprocess.run(['ss','-H','-lnt','sport = :8089'],capture_output=True,text=True).stdout}))
'''
password=getpass.getpass('CLUSTER_PASSWORD: ')
def Check(host):
    c=paramiko.SSHClient();c.load_system_host_keys();c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host,username='ubuntu',password=password,allow_agent=False,look_for_keys=False,timeout=15)
        _,out,err=c.exec_command(shlex.join([str(ROOT/'releases/2026-09-30-d4106d1/.venv/bin/python'),'-c',CODE]),timeout=30)
        text=out.read().decode();assert out.channel.recv_exit_status()==0,err.read().decode();row=json.loads(text);row['host']=host
        assert not row['processes'] and not row['port_8089'],host+' has an active experiment node';return row
    finally:c.close()
try:
    with concurrent.futures.ThreadPoolExecutor(10) as pool:rows=list(pool.map(Check,HOSTS))
finally:password=None
assert len({r['sha256'] for r in rows})==1
(ROOT/'results/postflight-20261014.json').write_text(json.dumps(rows,indent=2))
print(json.dumps({'stopped_nodes':len(rows),'sha256':rows[0]['sha256'],'minimum_free_gib':min(r['free_gib'] for r in rows)}),flush=True)
