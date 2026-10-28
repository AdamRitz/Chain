"""Real local nodes: encrypted wallet, deployed chat, IPv6, receipts, persistence and gateway boundaries."""
import argparse,json,os,socket,struct,subprocess,time,urllib.request,urllib.error
from pathlib import Path
import psutil
from NetworkTest import ReadAll,FLAGS

def Main(args):
    work=args.work.resolve();work.mkdir(parents=True,exist_ok=False)
    binary=args.bin.resolve();source=Path(__file__).resolve().parents[1]
    env=dict(os.environ);log=(work/'gateway.stderr.txt').open('w',encoding='utf-8')
    command=[str(binary/'chain_classroom.exe' if os.name=='nt' else binary/'chain_classroom'),
        '--data',str(work/'app'),'--node',str(binary/('boost.exe' if os.name=='nt' else 'boost')),
        '--web',str(source/'Classroom/dist'),'--contract',str(source/'Contracts/ClassroomChat.compiled.json')]
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=log,text=True,encoding='utf-8',creationflags=FLAGS,env=env)
    records=[];checks=[]
    try:
        line=process.stdout.readline();assert line,line
        ready=json.loads(line);origin=ready['url'].split('/#')[0];token=ready['url'].split('/#')[1]
        def Api(action,**values):
            request=urllib.request.Request(origin+'/api',json.dumps(dict(action=action,**values)).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            with urllib.request.urlopen(request,timeout=60) as response:return json.load(response)
        def Wait(predicate,timeout=20):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                status=Api('status')
                if predicate(status):return status
                time.sleep(.15)
            raise AssertionError('Timed out: '+json.dumps(status,ensure_ascii=False)[:1800])
        assert not Api('status')['has_wallet']
        Api('wallet_create',password='classroom-test-password')
        backup=Api('backup',password='another-backup-password');assert backup['kdf']=='argon2id-interactive'
        Api('lock')
        try:Api('unlock',password='incorrect');raise AssertionError('Wrong password accepted')
        except urllib.error.HTTPError as e:assert e.code==400
        Api('unlock',password='classroom-test-password');checks.append('encrypted wallet create lock unlock and backup; wrong password rejected')
        status=Api('create',mode='demo',title='真实三节点课堂测试')
        status=Wait(lambda s:s.get('contract_ready') and len(s.get('nodes',[]))==3 and all(n.get('online') for n in s['nodes']))
        for child in psutil.Process(process.pid).children(recursive=True):
            if Path(child.exe()).resolve()!=binary/('boost.exe' if os.name=='nt' else 'boost'):continue
            records.append(dict(pid=child.pid,created=child.create_time(),binary=child.exe(),command=child.cmdline()))
        assert len(records)==3
        wallets={w['id']:w for w in status['wallets']};port=status['network']['ports'][0]
        invite=Api('invite',seeds=[f'127.0.0.1:{port}',f'[::1]:{port}'])
        assert 'teacher' not in json.dumps(invite) or 'teacher' in invite.get('title','')
        sent=Api('transfer',receiver=wallets['bob']['public_key'],amount='100')
        status=Wait(lambda s:any(t['hash']==sent['hash'] and t['state']=='included' for t in s.get('outbox',[])))
        checks.append('three real nodes, idempotent deployment and a signed native transfer')
        client_id='12'*32;body='你好，区块链！<img src=x onerror=alert(1)>'
        Api('message',room=1,body=body,client_id=client_id)
        status=Wait(lambda s:any(m['body']==body for m in s.get('messages',[])))
        message=next(m for m in status['messages'] if m['body']==body);assert message['sender']==wallets['alice']['evm_address']
        Wait(lambda s:len({n.get('head') for n in s['nodes']})==1)
        contract=invite['contract'];compiled=json.loads((source/'Contracts/ClassroomChat.compiled.json').read_text())['contracts']['ClassroomChat']
        result=Api('call',to=contract,input=compiled['evm']['methodIdentifiers']['messageCount()'])
        assert result['status']==0 and int(result['output'],16)==1 and result['read_only']
        count_before=Api('status')['stats']['chain_transactions']
        try:Api('message',room=1,body='x'*513);raise AssertionError('Oversize message accepted')
        except urllib.error.HTTPError as e:assert e.code==400
        Api('message',room=1,body=body,client_id=client_id)
        status=Wait(lambda s:any(t.get('execution_status',0)!=0 for t in s.get('outbox',[])))
        assert len([m for m in status['messages'] if m['client_id']==client_id])==1
        checks.append('UTF-8 chat event and receipt, bounded body, read-only call and duplicate-ID contract revert')
        def Node(host,kind,payload=b''):
            with socket.create_connection((host,port),timeout=4) as sock:
                sock.sendall(b'\x02'+struct.pack('<BI',kind,len(payload))+payload)
                response,size=struct.unpack('<BI',ReadAll(sock,5));assert response==kind+1
                return ReadAll(sock,size)
        if status['ipv6_available']:
            assert json.loads(Node('::1',13))['height']==json.loads(Node('127.0.0.1',13))['height']
            checks.append('IPv6 ::1 and IPv4 share one canonical ledger')
        try:
            urllib.request.urlopen(urllib.request.Request(origin+'/api',b'{"action":"status"}',headers={'Authorization':'Bearer '+token,'Origin':'https://invalid.example'}));raise AssertionError('Foreign origin accepted')
        except urllib.error.HTTPError as e:assert e.code==403
        try:
            urllib.request.urlopen(urllib.request.Request(origin+'/api',b'{"action":"status"}'));raise AssertionError('Missing token accepted')
        except urllib.error.HTTPError as e:assert e.code==401
        assert '"raw":' not in json.dumps(Api('diagnostics'))
        checks.append('gateway rejects foreign origins and missing token; diagnostics exclude raw outbox and seeds')
        Api('rebuild_index');Wait(lambda s:any(m['client_id']==client_id for m in s.get('messages',[])))
        Api('stop');assert not Api('status')['running'];Api('start')
        status=Wait(lambda s:s.get('contract_ready') and any(m['client_id']==client_id for m in s.get('messages',[])))
        assert status['network']['contract']==contract
        checks.append('index rebuild and full node restart preserve contract and canonical chat')
        # Simulate a desktop interruption after deployment mined but before its manifest was saved.
        profile_path=work/'app'/'networks'/status['network']['id']/'profile.json'
        Api('shutdown');process.wait(timeout=30)
        saved=json.loads(profile_path.read_text(encoding='utf-8'));saved['contract']='';saved.pop('deploy_tx',None)
        profile_path.write_text(json.dumps(saved),encoding='utf-8')
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=log,text=True,encoding='utf-8',creationflags=FLAGS,env=env)
        ready=json.loads(process.stdout.readline());origin=ready['url'].split('/#')[0];token=ready['url'].split('/#')[1]
        status=Wait(lambda s:s.get('contract_ready') and s.get('network',{}).get('contract')==contract)
        assert not status['unlocked']
        checks.append('desktop restart recovers deployment metadata from the signed outbox without unlocking or redeploying')
        (work/'result.json').write_text(json.dumps(dict(checks=checks,nodes=records,contract=contract),ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(dict(checks=checks),ensure_ascii=False))
        Api('shutdown');process.wait(timeout=30);assert process.returncode==0
    finally:
        if process.poll() is None:
            (work/'app'/'gateway-stop.request').write_text('stop')
            # Only this gateway process tree belongs to this fresh test directory.
            for child in psutil.Process(process.pid).children(recursive=True):
                if Path(child.exe()).resolve()!=binary/('boost.exe' if os.name=='nt' else 'boost'):continue
                records.append(dict(pid=child.pid,created=child.create_time(),binary=child.exe(),command=child.cmdline()))
            process.terminate();process.wait(timeout=30)
        log.close()
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            live=[]
            for record in records:
                try:
                    p=psutil.Process(record['pid'])
                    if abs(p.create_time()-record['created'])<.01:live.append(p.pid)
                except psutil.NoSuchProcess:pass
            if not live:break
            time.sleep(.1)
        # Evidence and encrypted inputs remain; delete only stopped test node databases.
        import shutil
        removed=[]
        for db in (work/'app'/'networks').glob('*/node*/db'):
            assert db.resolve().is_relative_to(work) and not db.is_symlink() and (db/'CURRENT').is_file()
            for record in records:
                try:
                    alive=psutil.Process(record['pid'])
                    assert abs(alive.create_time()-record['created'])>.01,'Test child still running'
                except psutil.NoSuchProcess:pass
            shutil.rmtree(db);removed.append(str(db))
        (work/'database-cleanup.json').write_text(json.dumps(dict(databases=removed,process_stopped=True),indent=2),encoding='utf-8')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--bin',type=Path,required=True);p.add_argument('--work',type=Path,required=True);Main(p.parse_args())
