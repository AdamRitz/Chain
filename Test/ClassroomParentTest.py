"""Windows desktop-parent exit must stop the gateway and its owned real nodes."""
import argparse,json,os,subprocess,sys,time,urllib.request,shutil
from pathlib import Path
import psutil
from NetworkTest import FLAGS,ExeName

def Main(args):
    source=Path(__file__).resolve().parents[1];root=args.work.resolve()
    if args.helper:
        command=[str(args.bin.resolve()/ExeName('chain_classroom')),'--data',str(root/'data'),'--node',str(args.bin.resolve()/ExeName('boost')),'--web',str(source/'Classroom/dist'),'--contract',str(source/'Contracts/ClassroomChat.compiled.json'),'--parent-pid',str(os.getpid())]
        with (root/'gateway.stderr').open('w') as error:
            gateway=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=error,text=True,encoding='utf-8',creationflags=FLAGS)
            print(gateway.stdout.readline().strip(),flush=True)
            sys.stdin.readline()
        return
    if os.name!='nt':raise RuntimeError('This test checks Windows process-handle supervision')
    root.mkdir(parents=True,exist_ok=False)
    helper=subprocess.Popen([sys.executable,__file__,'--bin',str(args.bin.resolve()),'--work',str(root),'--helper'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,encoding='utf-8',creationflags=FLAGS)
    ready=json.loads(helper.stdout.readline());gateway=psutil.Process(ready['pid']);created=gateway.create_time();base,token=ready['url'].split('/#');nodes=[]
    try:
        def Api(action,**kw):
            req=urllib.request.Request(base+'/api',json.dumps(dict(action=action,**kw)).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            return json.load(urllib.request.urlopen(req,timeout=60))
        Api('wallet_create',password='parent-lifetime-test');Api('create',mode='demo',title='桌面生命周期测试')
        nodes=[dict(pid=p.pid,created=p.create_time()) for p in gateway.children(recursive=True) if p.name().lower()=='boost.exe']
        assert len(nodes)==3
        helper.stdin.write('exit\n');helper.stdin.flush();helper.wait(timeout=5)
        gateway.wait(timeout=30)
        for record in nodes:
            try:assert abs(psutil.Process(record['pid']).create_time()-record['created'])>.01
            except psutil.NoSuchProcess:pass
        (root/'result.json').write_text(json.dumps(dict(parent_exit_detected=True,gateway_exited=True,owned_nodes_stopped=3),indent=2));print('Desktop parent-exit lifecycle passed')
    finally:
        if helper.poll() is None:helper.terminate();helper.wait(timeout=5)
        try:
            p=psutil.Process(ready['pid'])
            if abs(p.create_time()-created)<.01:p.wait(timeout=30)
        except psutil.NoSuchProcess:pass
        removed=[]
        for db in (root/'data/networks').glob('*/node*/db'):
            assert db.resolve().is_relative_to(root) and not db.is_symlink();shutil.rmtree(db);removed.append(str(db))
        (root/'database-cleanup.json').write_text(json.dumps(dict(processes_stopped=True,databases=removed),indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--bin',type=Path,required=True);p.add_argument('--work',type=Path,required=True);p.add_argument('--helper',action='store_true');Main(p.parse_args())
