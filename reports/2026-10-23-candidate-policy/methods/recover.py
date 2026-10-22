import concurrent.futures,json,subprocess,shlex
from pathlib import Path
import sys
sys.path.insert(0,'/opt/chain/releases/0e3c1397fcd53fe6104ba4d240406bb4028696d5/source/Test')
from Cluster import ReadInventory
_,inventory=ReadInventory('/opt/chain-control/automation/cluster.local.json')
run='clock-grid-0e3c139-N95-20000-r1'
source='/opt/chain/releases/0e3c1397fcd53fe6104ba4d240406bb4028696d5/source'
def Work(node):
    cmd=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile=/opt/chain-control/known_hosts','-o','ControlMaster=auto','-o','ControlPersist=600','-o','ControlPath=/opt/chain-control/policy-ssh-%C','-i',node['ssh_key'],node['ssh']]
    result=subprocess.run(cmd+['true'],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,(node['id'],result.stderr)
    rows=[]
    for action in ['stop','collect','delete-db']:
        request={'root':node['root'],'run':run,'node':node['id'],'action':action}
        result=subprocess.run(cmd+[shlex.join([node.get('python','python3'),source+'/Test/ClusterWorker.py'])],input=json.dumps(request),capture_output=True,text=True,timeout=45)
        assert result.returncode==0,(node['id'],action,result.stderr,result.stdout)
        rows.append({'action':action,'result':json.loads(result.stdout)})
    return {'node':node['id'],'records':rows}
with concurrent.futures.ThreadPoolExecutor(10) as pool:results=list(pool.map(Work,inventory))
Path('/opt/chain-control/candidate-policy/recovery-cleanup.json').write_text(json.dumps(results,indent=2))
print(json.dumps({'preconnected':len(results),'old_test_cleanup':'complete'}))
