"""Read-only preflight of the ten authorized experiment servers."""
import concurrent.futures
import getpass
import json
import shlex
from pathlib import Path
import paramiko

ROOT = Path('/home/ubuntu/chain')
HOSTS = ['10.206.0.16','10.206.0.4','10.206.0.14','10.206.0.17','10.206.0.15','10.206.0.12','10.206.0.2','10.206.0.6','10.206.0.11','10.206.0.8']
PYTHON = ROOT/'releases/2026-09-30-d4106d1/.venv/bin/python'
PROBE = '''
import hashlib,json,os,psutil,shutil,subprocess
from pathlib import Path
root=Path('/home/ubuntu/chain')
processes=[]
for p in psutil.process_iter(['pid','exe','create_time']):
    try:
        exe=p.info['exe'] or ''
        if '/chain/' in exe and Path(exe).name in ['boost','fisco-bcos']:
            processes.append({**p.info,'cwd':p.cwd()})
    except (psutil.AccessDenied,psutil.NoSuchProcess):pass
releases=[]
for release in sorted((root/'releases').iterdir()):
    binary=release/'build/boost'
    if binary.exists():
        with binary.open('rb') as file:digest=hashlib.file_digest(file,'sha256').hexdigest()
        releases.append({'name':release.name,'sha256':digest})
port=subprocess.run(['ss','-H','-lnt','sport = :8089'],capture_output=True,text=True).stdout.strip()
print(json.dumps({'host_name':os.uname().nodename,'cpu_count':psutil.cpu_count(),
    'memory_gib':psutil.virtual_memory().total/2**30,'free_gib':shutil.disk_usage(root).free/2**30,
    'processes':processes,'port8089':port,'releases':releases,
    'datasets':[{'path':str(p),'bytes':p.stat().st_size} for p in (root/'results').glob('*/transactions.bin')],
    'root_files':[p.name for p in root.iterdir() if p.is_file()]}))
'''

def Check(host,password):
    client=paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host,username='ubuntu',password=password,allow_agent=False,look_for_keys=False,timeout=12)
        _,out,err=client.exec_command(shlex.join([str(PYTHON),'-c',PROBE]),timeout=30)
        data=out.read().decode(); error=err.read().decode(); code=out.channel.recv_exit_status()
        if code:raise RuntimeError(error[-500:])
        return {'host':host,'ok':True,**json.loads(data)}
    except Exception as error:return {'host':host,'ok':False,'error':str(error).replace(password,'[redacted]')}
    finally:client.close()

password=getpass.getpass('CLUSTER_PASSWORD: ')
try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        rows=list(pool.map(lambda host:Check(host,password),HOSTS))
finally:password=None
destination=ROOT/'results/tps-a5cf8a2/preflight.json'
destination.parent.mkdir(parents=True,exist_ok=True)
destination.write_text(json.dumps(rows,indent=2))
for row in rows:
    print(json.dumps({key:row.get(key) for key in ['host','ok','error','cpu_count','free_gib','processes','port8089','releases']}),flush=True)
print('PREFLIGHT',sum(row['ok'] for row in rows),len(rows),flush=True)
