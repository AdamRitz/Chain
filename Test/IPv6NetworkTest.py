"""IPv6-only, dual-stack and legacy IPv4 peers replicate a real signed native transaction."""
import argparse,json,os,socket,struct,subprocess,time,shutil
from pathlib import Path
from NetworkTest import Run,ReadAll,Frame,FLAGS,ExeName

def Main(args):
    work=args.work.resolve();work.mkdir(parents=True,exist_ok=False);binary=args.bin.resolve();nodes=[];checks=[]
    # IPv6 is an explicit requirement: an unavailable stack fails this suite rather than silently passing.
    with socket.socket(socket.AF_INET6) as probe:probe.bind(('::1',0))
    from Cluster import ReadInventory
    inventory=work/'inventory.json'
    inventory.write_text(json.dumps({'defaults':{'binary':'unused','worker':'unused','root':str(work)},'nodes':[{'id':'ipv6','host':'::1','port':8090,'ssh':'root@::1'},{'id':'ipv4','host':'127.0.0.1','port':8091,'ssh':'root@127.0.0.1'}]}))
    _,parsed=ReadInventory(inventory);assert parsed[0]['host']=='::1'
    checks.append('experiment inventory accepts IPv6 endpoints and SSH destinations')
    dataset=work/'transactions.bin';Run([binary/ExeName('sender'),'--prepare',dataset,'--count',1])
    genesis=str(dataset)+'.genesis.json'
    def Query(host,port,kind=13,payload=b''):
        with socket.create_connection((host,port),timeout=3) as c:
            c.sendall(b'\x02'+Frame(kind,payload));response,size=struct.unpack('<BI',ReadAll(c,5));assert response==kind+1
            return json.loads(ReadAll(c,size))
    def Start(name,host,seed=None,dual=False,legacy=False):
        with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET) as p:p.bind((host,0));port=p.getsockname()[1]
        directory=work/name;directory.mkdir();stop=directory/'stop.request';log=(directory/'node.log').open('w')
        command=[str(binary/ExeName('boost')),'--data',str(directory/'db'),'--genesis',genesis,'--bind',host,'--port',str(port),'--stop-file',str(stop),'--io-threads','2','--verify-threads','2','--relay-transactions','1','--peer-protocol','1' if legacy else '2','--run-seconds','90']
        if dual:command+=['--bind-v6','::1']
        if seed:command+=['--seed',seed]
        child=subprocess.Popen(command,stdout=log,stderr=log,creationflags=FLAGS);node=dict(process=child,host=host,port=port,stop=stop,log=log,directory=directory);nodes.append(node)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if child.poll() is not None:raise RuntimeError((directory/'node.log').read_text(errors='replace'))
            try:Query(host,port);return node
            except (OSError,ConnectionError):time.sleep(.05)
        raise AssertionError('Node startup timeout')
    try:
        a=Start('dual','127.0.0.1',dual=True)
        b=Start('ipv6-only','::1',f"[::1]:{a['port']}")
        c=Start('legacy-ipv4','127.0.0.1',f"127.0.0.1:{a['port']}",legacy=True)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if Query(a['host'],a['port'])['peers']>=2:break
            time.sleep(.05)
        else:raise AssertionError('Mixed peers did not connect')
        with socket.create_connection(('::1',b['port']),timeout=3) as connection:connection.sendall(b'\x02'+Frame(1,dataset.read_bytes()))
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            states=[Query(n['host'],n['port']) for n in nodes]
            if all(s['chain_transactions']==1 for s in states) and len({s['head'] for s in states})==1:break
            time.sleep(.05)
        else:raise AssertionError('Mixed peers did not converge')
        checks.append('IPv6-only sender propagates one signed transaction through dual-stack and legacy IPv4 peers')
        # Exercise the existing CLI transport over IPv6, not just Python sockets.
        output=Run([binary/ExeName('sender'),'--stats','--host','::1','--port',b['port']])
        checks.append('sender CLI resolves and queries IPv6')
        def Discover(first,expected):
            with socket.create_connection(('127.0.0.1',a['port']),timeout=3) as connection:
                connection.sendall(bytes([first])+struct.pack('<H',65534)+Frame(8))
                while True:
                    kind,size=struct.unpack('<BI',ReadAll(connection,5));payload=ReadAll(connection,size)
                    if kind==expected:return payload
        extended=Discover(4,48);families=[];offset=0
        while offset<len(extended):
            family=extended[offset];families.append(family);assert family in [4,6];offset+=1+(4 if family==4 else 16)+2
        assert offset==len(extended) and 4 in families and 6 in families
        legacy=Discover(3,9);assert len(legacy)%6==0
        checks.append('version-2 discovery contains IPv4 and IPv6; legacy discovery keeps six-byte records')
        invalid=Query('::1',b['port'],46,json.dumps({'method':'block','height':0}).encode());assert 'error' in invalid
        assert not Query('::1',b['port'])['failed']
        checks.append('invalid teaching query returns an error without stopping the node')
        (work/'result.json').write_text(json.dumps(dict(checks=checks),indent=2));print(json.dumps(dict(checks=checks)))
    finally:
        for n in nodes:n['stop'].write_text('stop')
        for n in nodes:
            try:n['process'].wait(timeout=20)
            except subprocess.TimeoutExpired:n['process'].kill();n['process'].wait(timeout=5)
            n['log'].close()
        removed=[]
        for n in nodes:
            db=n['directory']/'db';assert n['process'].poll() is not None and db.resolve().is_relative_to(work) and not db.is_symlink()
            if db.exists():shutil.rmtree(db);removed.append(str(db))
        (work/'database-cleanup.json').write_text(json.dumps(dict(databases=removed,process_stopped=True),indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--bin',type=Path,required=True);p.add_argument('--work',type=Path,required=True);Main(p.parse_args())
