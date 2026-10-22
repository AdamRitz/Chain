import json,subprocess,sys
from pathlib import Path
base=Path('/opt/chain-control/results/candidate-policy-0e3c139-20261023-b')
assert Path('/opt/chain-control/logs/candidate-policy-grid2.exit').read_text().strip()=='0'
records=json.loads((base/'results.json').read_text());assert len(records)==25
failed=[r for r in records if r.get('metrics',{}).get('proven_common_tps') is None]
assert all(r['status']=='complete' and r['state_audit']['passed'] and not r.get('cleanup_errors') for r in records)
out=base.parent/'candidate-policy-0e3c139-20261023-retry';out.mkdir(exist_ok=False)
(out/'manifest.json').write_text(json.dumps({'reason':'one same-parameter fresh-database retry per missing/rejected measurement endpoint','source_batch':str(base),'cases':[r['case'] for r in failed]},indent=2))
release=Path('/opt/chain/releases/0e3c1397fcd53fe6104ba4d240406bb4028696d5')
validation=json.loads((release/'validation.json').read_text())
all_results=[]
for record in failed:
    case=record['case'];name=f"{case['group']}-{case['rate']}"
    cmd=[sys.executable,'/opt/chain-control/candidate-policy/methods-retry/Run.py','--source',str(release/'source'),'--bin',str(release/'build'),'--inventory','/opt/chain-control/automation/cluster.local.json','--known-hosts','/opt/chain-control/known_hosts','--plan',str(release/'source/Test/Clock/Benchmark/plan.json'),'--work',str(out/name),'--source-revision',validation['revision'],'--expected-binary-sha256',validation['binaries']['boost'],'--expected-model-sha256',validation['model_sha256'],'--phase','endpoint','--cycles','1','--groups',case['group'],'--rates',str(case['rate']),'--run-tag','repair1','--dataset',str(base/'transactions.bin')]
    subprocess.run(cmd,check=True)
    rows=json.loads((out/name/'results.json').read_text())
    all_results.extend(rows);(out/'results.json').write_text(json.dumps(all_results,indent=2))
print('RETRIES_FINISHED',len(all_results),flush=True)
