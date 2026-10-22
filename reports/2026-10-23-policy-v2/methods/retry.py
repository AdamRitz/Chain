from pathlib import Path
import json,subprocess
root=Path('/opt/chain-control/results/policy-v2-d28a2fb-20261023-b')
rows=json.loads((root/'results.json').read_text())
assert len(rows)==25 and all(r['status']=='complete' and r['state_audit']['passed'] and not r.get('cleanup_errors') for r in rows)
bad=[r for r in rows if not r['metrics'].get('common_boundary_proof_passed')]
runtime='d28a2fb2146a9afcff4f8f0758cd7c248e1023b6'
release=Path('/opt/chain/releases')/runtime
source=Path('/opt/chain-control/policy-v2/driver-c2c0c38536768f2d8a596c4c4f215e89470555a6')
validation=json.loads((release/'validation.json').read_text())
out=Path('/opt/chain-control/results/policy-v2-d28a2fb-20261023-retry');out.mkdir(exist_ok=False)
(out/'selection.json').write_text(json.dumps([r['case'] for r in bad],indent=2))
for r in bad:
    c=r['case'];name=f"{c['group']}-{c['rate']}"
    args=['/opt/chain/.venv/bin/python',str(source/'Test/Clock/Benchmark/Run.py'),
        '--source',str(source),'--bin',str(release/'build'),'--inventory','/opt/chain-control/automation/cluster.local.json',
        '--known-hosts','/opt/chain-control/known_hosts','--plan',str(source/'Test/Clock/Benchmark/plan-v2.json'),
        '--work',str(out/name),'--policy-v2','--groups',c['group'],'--rates',str(c['rate']),
        '--dataset','/opt/chain-control/results/candidate-policy-0e3c139-20261023-b/transactions.bin',
        '--run-tag','v2retry1','--phase','endpoint','--cycles','1','--source-revision',runtime,
        '--expected-binary-sha256',validation['binaries']['boost'],'--expected-model-sha256',validation['model_sha256']]
    subprocess.run(args,check=True)
print(json.dumps({'diagnostic_retries':len(bad),'selection':'only invalid endpoints; one per failed cell; no valid result replaced'}))
