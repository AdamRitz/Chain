import hashlib,json,sys,tarfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from remote import connect
out=Path(__file__).resolve().parents[2]/'outputs/q1-b3-20261023'
c=connect('182.151.5.57')
try:
    with c.open_sftp() as s:
        s.get('/opt/chain-control/q1-b3/evidence.json',str(out/'evidence.json'))
        manifest=json.loads((out/'evidence.json').read_text())
        for entry,directory in zip(manifest,['raw-main','raw-retry']):
            target=out/(directory+'.tar.gz');s.get(entry['path'],str(target))
            assert target.stat().st_size==entry['bytes'] and hashlib.sha256(target.read_bytes()).hexdigest()==entry['sha256']
            dest=out/directory;dest.mkdir(exist_ok=False)
            with tarfile.open(target) as archive:
                for member in archive.getmembers():
                    assert member.isfile() and (dest/member.name).resolve().is_relative_to(dest.resolve())
                archive.extractall(dest,filter='data')
        for name in ['q1-b3-build.log','q1-b3-deploy.log','q1-b3-grid.log','q1-b3-restored.log','q1-b3-restore-verified.log']:
            s.get('/opt/chain-control/logs/'+name,str(out/name))
        if len(manifest)>1:s.get('/opt/chain-control/logs/q1-b3-retry.log',str(out/'q1-b3-retry.log'))
    print(json.dumps({'verified_archives':len(manifest),'removed_databases':sum(r['databases_removed'] for r in manifest),'output':str(out)}))
finally:c.close()
