import json
from pathlib import Path
import shutil
import zipfile
import psutil

release=Path('/home/ubuntu/chain/releases/2026-10-14-tps-a5cf8a2').resolve()
root=release/'build/check-a5cf8a2'
assert root.is_dir() and not root.is_symlink() and root.resolve().is_relative_to(release/'build')
assert json.loads((release/'validation.json').read_text())['ctest_passed']
active=[]
for process in psutil.process_iter(['exe']):
    exe=process.info['exe'] or ''
    if exe and Path(exe).is_relative_to(release/'build'):active.append(process.pid)
assert not active,'Release test processes still running'
databases=[path.parent.resolve() for path in root.rglob('CURRENT')]
assert all(path.is_relative_to(root) and not path.is_symlink() for path in databases)
top=[path for path in databases if not any(path!=other and path.is_relative_to(other) for other in databases)]
archive=release/'ctest-records.zip'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as saved:
    for path in root.rglob('*'):
        if path.is_file() and not any(path.resolve().is_relative_to(db) for db in top):
            saved.write(path,path.relative_to(root))
size=sum(path.stat().st_size for database in top for path in database.rglob('*') if path.is_file())
for path in top:shutil.rmtree(path)
assert not list(root.rglob('CURRENT'))
record={'databases_removed':len(databases),'bytes_removed':size,'records_archive':str(archive),
        'scope':str(root),'remaining_databases':0}
Path('/home/ubuntu/chain/results/tps-a5cf8a2/linux-test-cleanup.json').write_text(json.dumps(record,indent=2))
print(json.dumps(record),flush=True)
