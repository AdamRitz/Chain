"""Run authorized experiments independently of the interactive SSH connection."""
import getpass,json,subprocess,sys,time
from pathlib import Path
import psutil
root=Path('/home/ubuntu/chain');name=sys.argv[1]
assert name in ['CloudThroughputNextV2.py','HostProfileNextV2.py','RoundNextV2.py','Round100NextV2.py']
password=getpass.getpass('CLUSTER_PASSWORD: ')
dest=root/'results/launch-20261014-v2';dest.mkdir(exist_ok=True)
record=dest/(name+'.json');assert not record.exists(),'Run already launched'
with (dest/(name+'.log')).open('wb') as log:
    child=subprocess.Popen([str(root/'releases/2026-09-30-d4106d1/.venv/bin/python'),'-u',str(root/name)],
        stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,cwd=root)
    child.stdin.write((password+'\n').encode());child.stdin.flush();child.stdin.close()
password=None
process=psutil.Process(child.pid)
data={'pid':child.pid,'created':process.create_time(),'command':process.cmdline(),'script':name,'started_utc':time.time()}
record.write_text(json.dumps(data,indent=2));print(json.dumps(data),flush=True)
