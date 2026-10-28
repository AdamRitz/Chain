"""Preserve diagnostics, then remove recognized RocksDB files from explicitly named, stopped classroom test roots."""
import argparse,json,re,shutil
from pathlib import Path
import psutil

def Main(args):
    build=(Path(__file__).resolve().parents[2]/'build').resolve()
    roots=[p.resolve() for p in args.roots]
    for root in roots:
        assert root.parent==build and root.name.startswith(('classroom-','教学网络','教学目录')) and not root.is_symlink(),str(root)
    active=[]
    for process in psutil.process_iter(['pid','cmdline']):
        try:
            command=process.info['cmdline'] or []
            if '--data' in command:
                data=Path(command[command.index('--data')+1]).resolve()
                if any(data.is_relative_to(root) for root in roots):active.append(dict(pid=process.pid,data=str(data)))
        except (psutil.NoSuchProcess,psutil.AccessDenied,IndexError,OSError):pass
    assert not active,active
    records=[]
    pattern=re.compile(r'(CURRENT|IDENTITY|LOCK|LOG(?:\.old\..+)?|MANIFEST-\d+|OPTIONS-\d+|\d+\.(sst|ldb|log|dbtmp))')
    for root in roots:
        if not root.exists():continue
        for marker in list(root.rglob('CURRENT')):
            database=marker.parent.resolve()
            assert database.is_relative_to(root) and not marker.is_symlink() and list(database.glob('MANIFEST-*'))
            target=build/(root.name+'-db-evidence')/str(len(records));target.mkdir(parents=True,exist_ok=False)
            files=[p for p in database.iterdir() if p.is_file() and pattern.fullmatch(p.name)]
            for path in files:
                assert not path.is_symlink() and path.resolve().parent==database
                if path.name.startswith(('LOG','OPTIONS-','MANIFEST-')):shutil.copy2(path,target/path.name)
            for path in files:path.unlink()
            records.append(dict(database=str(database),files_removed=len(files),evidence=str(target)))
    remaining=[str(p) for root in roots if root.exists() for extension in ['CURRENT','*.sst','*.ldb'] for p in root.rglob(extension)]
    assert not remaining,remaining
    result=dict(processes_stopped=True,roots=[str(r) for r in roots],databases_cleared=len(records),records=records,code_inputs_profiles_and_logs_preserved=True)
    args.record.parent.mkdir(parents=True,exist_ok=True);args.record.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(dict(databases_cleared=len(records))))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--roots',nargs='+',type=Path,required=True);p.add_argument('--record',type=Path,required=True);Main(p.parse_args())
