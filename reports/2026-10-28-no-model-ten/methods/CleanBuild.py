"""Clean only recognized RocksDB files beneath this task's stopped Linux test root."""
import json,re,shutil
from pathlib import Path
release=Path('/opt/chain/releases/1504583cda142c8f50fcc6215864160ae469f333')
root=release/'build/check-no-model-20261028'
assert json.loads((release/'validation.json').read_text())['ctest_passed']
assert root.resolve().parent==release/'build' and not root.is_symlink()
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:args=path.read_bytes().decode().split('\0')
    except (FileNotFoundError,PermissionError,UnicodeError):continue
    assert not any(arg.startswith(str(root)+'/') for arg in args),'Regression process still running'
databases=[]
for marker in root.rglob('CURRENT'):
    db=marker.parent
    assert db.resolve().is_relative_to(root.resolve()) and not db.is_symlink() and list(db.glob('MANIFEST-*'))
    databases.append(db)
preserved=release/'validation-db-logs';preserved.mkdir(exist_ok=False)
records=[]
for index,db in enumerate(databases):
    files=[p for p in db.iterdir() if p.is_file() and re.fullmatch(r'(CURRENT|IDENTITY|LOCK|LOG(?:\.old\..+)?|MANIFEST-\d+|OPTIONS-\d+|\d+\.(sst|ldb|log|dbtmp))',p.name)]
    dest=preserved/str(index);dest.mkdir()
    for path in files:
        assert not path.is_symlink() and path.resolve().parent==db.resolve()
        if path.name.startswith(('LOG','OPTIONS-','MANIFEST-')):shutil.copy2(path,dest/path.name)
    for path in files:path.unlink()
    records.append(dict(database=str(db),removed_files=len(files),logs=str(dest)))
assert not any(root.rglob('CURRENT')) and not any(root.rglob('*.sst')) and not any(root.rglob('*.ldb'))
record=dict(test_processes_stopped=True,databases_cleared=len(records),records=records,code_inputs_and_logs_preserved=True)
(release/'build-test-cleanup.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(dict(databases_cleared=len(records))))
