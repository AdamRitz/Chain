"""Archive old experiment records, then remove only their disposable databases."""
import concurrent.futures, getpass, hashlib, json, shlex, sys, tarfile
from pathlib import Path
import paramiko

ROOT=Path('/home/ubuntu/chain');OUT=ROOT/'results/cleanup-20261014'
HOSTS=['10.206.0.16','10.206.0.4','10.206.0.14','10.206.0.17','10.206.0.15','10.206.0.12','10.206.0.2','10.206.0.6','10.206.0.11','10.206.0.8']
PYTHON=str(ROOT/'releases/2026-09-30-d4106d1/.venv/bin/python')
SCRIPT='''
import hashlib,json,psutil,shutil,sys,tarfile
from pathlib import Path
root=Path('/home/ubuntu/chain').resolve();out=root/'results/cleanup-20261014';out.mkdir(parents=True,exist_ok=True)
for p in psutil.process_iter(['exe']):
    exe=p.info['exe'] or ''
    assert not ('/chain/' in exe and Path(exe).name=='boost'),'A chain node is running'
mode=sys.argv[1];manifest_file=out/'local-manifest.json'
if mode=='prepare':
    if manifest_file.exists():
        manifest=json.loads(manifest_file.read_text())
        with Path(manifest['archive']).open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==manifest['archive_sha256']
        print(json.dumps({k:v for k,v in manifest.items() if k!='candidates'}));sys.exit(0)
    candidates=[];records=[]
    for base,prefix in [(root/'node-results','20260930-'),(root/'throughput-scratch-20261013','throughput20261013-')]:
        for run in sorted(base.glob(prefix+'*')):
            if not run.is_dir() or run.is_symlink():continue
            for directory in sorted(run.glob('node[0-9][0-9]')):
                if not directory.is_dir() or directory.is_symlink():continue
                for file in directory.iterdir():
                    if file.is_file() and not file.is_symlink():records.append(file)
                db=directory/'db'
                if not db.is_dir():continue
                assert not db.is_symlink() and db.resolve()==db
                assert db.resolve().is_relative_to(base.resolve())
                process=json.loads((directory/'process.json').read_text())
                args=process['command'];index=args.index('--data')
                assert Path(args[index+1]).resolve()==db.resolve()
                assert Path(process['binary']).resolve().is_relative_to(root/'releases')
                size=sum(f.stat().st_size for f in db.rglob('*') if f.is_file())
                candidates.append({'path':str(db),'bytes':size,'process':process})
    archive=out/'old-test-records.tar.gz'
    with tarfile.open(archive,'w:gz') as packed:
        for file in records:packed.add(file,arcname=str(file.relative_to(root)),recursive=False)
    with tarfile.open(archive) as packed:assert len(packed.getmembers())==len(records)
    with archive.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
    manifest={'candidates':candidates,'record_count':len(records),'archive':str(archive),'archive_sha256':digest,
              'bytes_to_remove':sum(row['bytes'] for row in candidates),'free_gib_before':shutil.disk_usage(root).free/2**30}
    manifest_file.write_text(json.dumps(manifest,indent=2))
    print(json.dumps({k:v for k,v in manifest.items() if k!='candidates'}))
elif mode=='clean':
    manifest=json.loads(manifest_file.read_text());archive=Path(manifest['archive'])
    with archive.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==manifest['archive_sha256']
    removed=[]
    for row in manifest['candidates']:
        db=Path(row['path']);assert db.name=='db' and not db.is_symlink() and db.resolve()==db
        assert db.parent.name.startswith('node') and db.parent.parent.name.startswith(('20260930-','throughput20261013-'))
        assert db.is_relative_to(root/'node-results') or db.is_relative_to(root/'throughput-scratch-20261013')
        assert json.loads((db.parent/'process.json').read_text())==row['process']
        if db.exists():shutil.rmtree(db);removed.append(str(db))
    manifest.update({'removed':removed,'free_gib_after':shutil.disk_usage(root).free/2**30})
    (out/'local-cleaned.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'removed_databases':len(removed),'free_gib_before':manifest['free_gib_before'],'free_gib_after':manifest['free_gib_after']}))
else:raise ValueError(mode)
'''

mode=sys.argv[1];assert mode in ['prepare','clean']
if mode=='clean':
    prepared=json.loads((OUT/'prepare.json').read_text());assert len(prepared)==10
    for row in prepared:
        with (OUT/row['host']/'old-test-records.tar.gz').open('rb') as stream:
            assert hashlib.file_digest(stream,'sha256').hexdigest()==row['archive_sha256']
OUT.mkdir(parents=True,exist_ok=True);password=getpass.getpass('CLUSTER_PASSWORD: ')
def Run(host):
    c=paramiko.SSHClient();c.load_system_host_keys();c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host,username='ubuntu',password=password,allow_agent=False,look_for_keys=False,timeout=15)
        _,out,err=c.exec_command(shlex.join([PYTHON,'-c',SCRIPT,mode]),timeout=180)
        data=out.read().decode();error=err.read().decode();assert out.channel.recv_exit_status()==0,host+': '+error[-2000:]
        row=json.loads(data);row['host']=host
        directory=OUT/host;directory.mkdir(exist_ok=True)
        with c.open_sftp() as sftp:
            name='local-manifest.json' if mode=='prepare' else 'local-cleaned.json'
            if host!='10.206.0.12':
                sftp.get(str(OUT/name),str(directory/name))
                if mode=='prepare':sftp.get(str(OUT/'old-test-records.tar.gz'),str(directory/'old-test-records.tar.gz'))
            else:
                import shutil
                shutil.copy2(OUT/name,directory/name)
                if mode=='prepare':shutil.copy2(OUT/'old-test-records.tar.gz',directory/'old-test-records.tar.gz')
        print(json.dumps(row),flush=True);return row
    finally:c.close()
try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:rows=list(pool.map(Run,HOSTS))
finally:password=None
(OUT/(mode+'.json')).write_text(json.dumps(rows,indent=2))
if mode=='prepare':
    with tarfile.open(ROOT/'cleanup-records-20261014.tar.gz','w:gz') as packed:packed.add(OUT,arcname='cleanup-20261014')
