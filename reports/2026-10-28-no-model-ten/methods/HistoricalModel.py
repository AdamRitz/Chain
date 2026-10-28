"""Export existing historical endpoints; does not start nodes or send load."""
import json
from pathlib import Path
root=Path('/opt/chain-control/results/model-tps-10f6543-20261022')
rows=[]
for r in json.loads((root/'results.json').read_text()):
    c=r['case']
    if c['group'] not in ['N95','M75']:continue
    name=f"{c['group']}-{c['rate']}-r{c['repeat']}"
    points=[json.loads(l) for l in (root/name/'timeline.jsonl').read_text().splitlines()]
    rows.append(dict(case=c,metrics=r['metrics'],endpoints=[
        {key:p.get(key) for key in ['target_seconds','common','common_observation_interval_seconds']}
        for p in points if p['target_seconds'] in [2,12]]))
print(json.dumps(rows,indent=2))
