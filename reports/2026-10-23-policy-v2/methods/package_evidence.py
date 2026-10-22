from pathlib import Path
import hashlib,json,tarfile
base=Path('/opt/chain-control/results')
names=['policy-v2-d28a2fb-20261023','policy-v2-d28a2fb-20261023-b','policy-v2-d28a2fb-20261023-retry']
assert Path('/opt/chain-control/logs/policy-v2-grid-fixed.exit').read_text().strip()=='0'
assert Path('/opt/chain-control/logs/policy-v2-retry.exit').read_text().strip()=='0'
manifest=[]
for name in names:
    root=base/name;archive=base/(name+'-evidence.tar.gz')
    with tarfile.open(archive,'w:gz') as target:
        for path in sorted(root.rglob('*')):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):continue
            if path.suffix not in {'.json','.jsonl','.log','.txt'} or path.name.endswith('.wallet.json'):continue
            target.add(path,arcname=str(path.relative_to(root)),recursive=False)
    clean=[json.loads(p.read_text()) for p in root.rglob('*-database-cleanup.json')]
    assert all(c.get('removed') for c in clean)
    manifest.append({'root':str(root),'path':str(archive),'bytes':archive.stat().st_size,
        'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'databases_removed':len(clean)})
Path('/opt/chain-control/policy-v2/evidence.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps(manifest))
