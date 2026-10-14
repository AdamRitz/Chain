"""Read-only postflight for the completed short benchmark."""
import concurrent.futures, getpass, json, shlex
from pathlib import Path
import paramiko

ROOT=Path('/home/ubuntu/chain')
HOSTS=['10.206.0.16','10.206.0.4','10.206.0.14','10.206.0.17','10.206.0.15','10.206.0.12','10.206.0.2','10.206.0.6','10.206.0.11','10.206.0.8']
PYTHON=ROOT/'releases/2026-09-30-d4106d1/.venv/bin/python'
CODE='''
import hashlib,json,psutil,shutil,subprocess
from pathlib import Path
root=Path('/home/ubuntu/chain');binary=root/'releases/2026-10-14-six-c4a8724/build/boost'
with binary.open('rb') as file:digest=hashlib.file_digest(file,'sha256').hexdigest()
active=[{'pid':p.pid,'binary':p.info['exe']} for p in psutil.process_iter(['exe']) if (p.info['exe'] or '').endswith('/build/boost')]
port=subprocess.run(['ss','-H','-lnt','sport = :8089'],capture_output=True,text=True).stdout.strip()
databases=[]
for name in ['scratch-six-c4a8724','scratch-quick-c4a8724']:
    scratch=root/name
    if scratch.exists():databases.extend(str(p) for p in scratch.glob('*/node[0-9][0-9]/db') if p.is_dir())
print(json.dumps({'sha256':digest,'active':active,'port8089':port,'remaining_test_databases':databases,'free_gib':shutil.disk_usage(root).free/2**30}))
'''
password=getpass.getpass('CLUSTER_PASSWORD: ')

def Check(host):
    client=paramiko.SSHClient();client.load_system_host_keys();client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host,username='ubuntu',password=password,allow_agent=False,look_for_keys=False,timeout=12)
        _,out,err=client.exec_command(shlex.join([str(PYTHON),'-c',CODE]),timeout=30)
        data=out.read().decode();exit_code=out.channel.recv_exit_status();assert exit_code==0,err.read().decode()
        result={'host':host,**json.loads(data)}
        assert not result['active'] and not result['port8089'] and not result['remaining_test_databases'],host+': incomplete cleanup'
        return result
    finally:client.close()

try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:rows=list(pool.map(Check,HOSTS))
finally:password=None
assert {row['sha256'] for row in rows}=={'882d7e8607b121113cd13558914940b8c9651b07d13bc242889a4844959cd4f5'}
(ROOT/'results/quick-20261014-c4a8724/postflight.json').write_text(json.dumps(rows,indent=2))
print(json.dumps({'stopped_nodes':len(rows),'remaining_test_databases':0,'minimum_free_gib':min(row['free_gib'] for row in rows)}))
