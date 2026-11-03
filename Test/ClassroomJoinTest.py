"""Two independent local app installations join over IPv6 and exchange funded chat transactions."""
import argparse,json,os,subprocess,time,urllib.request,urllib.error,shutil
from pathlib import Path
import psutil
from NetworkTest import FLAGS,ExeName

class App:
    def __init__(self,binary,source,root,artifact=None):
        self.root=root;root.mkdir(parents=True);self.log=(root/'stderr.txt').open('w')
        self.binary=binary;self.source=source;self.artifact=artifact or source/'Contracts/ClassroomChat.compiled.json'
        self.Launch()
        self.Api('wallet_create',password='isolated-classroom-test')
    def Launch(self):
        binary,source,root=self.binary,self.source,self.root
        self.process=subprocess.Popen([str(binary/ExeName('chain_classroom')),'--data',str(root/'data'),'--node',str(binary/ExeName('boost')),'--web',str(source/'Classroom/dist'),'--contract',str(self.artifact),'--stop-file',str(root/'stop.request')],stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',creationflags=FLAGS)
        ready=json.loads(self.process.stdout.readline());self.origin,self.token=ready['url'].split('/#')
    def NodeProcesses(self):
        return [child for child in psutil.Process(self.process.pid).children(recursive=True)
                if Path(child.exe()).resolve()==self.binary/ExeName('boost')]
    def Api(self,action,**kw):
        req=urllib.request.Request(self.origin+'/api',json.dumps(dict(action=action,**kw)).encode(),headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        return json.load(urllib.request.urlopen(req,timeout=60))
    def Wait(self,predicate):
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            value=self.Api('status')
            if predicate(value):return value
            time.sleep(.15)
        raise AssertionError(json.dumps(value,ensure_ascii=False)[:1500])
    def Stop(self):
        records=[]
        if self.process.poll() is None:
            for p in psutil.Process(self.process.pid).children(recursive=True):
                if p.name().lower()!=ExeName('boost').lower():continue
                try:records.append((p.pid,p.create_time()))
                except psutil.NoSuchProcess:pass
            (self.root/'stop.request').write_text('stop')
            self.process.wait(timeout=45)
        self.log.close()
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            alive=[]
            for pid,created in records:
                try:
                    if abs(psutil.Process(pid).create_time()-created)<.01:alive.append(pid)
                except psutil.NoSuchProcess:pass
            if not alive:break
            time.sleep(.1)
        for db in (self.root/'data/networks').glob('*/node*/db'):
            assert db.resolve().is_relative_to(self.root) and not db.is_symlink()
            for pid,created in records:
                try:assert abs(psutil.Process(pid).create_time()-created)>.01
                except psutil.NoSuchProcess:pass
            shutil.rmtree(db)

def Main(args):
    source=Path(__file__).resolve().parents[1];root=args.work.resolve();root.mkdir(exist_ok=False,parents=True);apps=[];checks=[]
    def Included(app,tx):
        return app.Wait(lambda s:any(t['hash']==tx['hash'] and t['state']=='included' for t in s.get('outbox',[])))
    try:
        creator=App(args.bin.resolve(),source,root/'creator');apps.append(creator)
        first=creator.Api('create',title='研究交流链')
        assert first['network']['chat_version']==2 and 'teacher' not in first['network']
        assert len(first['wallets'])==len(first['nodes'])==len(creator.NodeProcesses())==1
        assert first['wallets'][0]['id']=='main' and first['contract_ready']
        invite=creator.Api('invite',seeds=[f"[::1]:{first['network']['ports'][0]}"])
        member=App(args.bin.resolve(),source,root/'member');apps.append(member);member.Api('join',invite=invite)
        status=member.Wait(lambda s:s.get('contract_ready') and bool(s.get('rooms')))
        identity=status['wallets'][0];assert identity['balance']=='0' and len(member.NodeProcesses())==1
        Included(creator,creator.Api('transfer',receiver=identity['public_key'],amount='10000000'))
        member.Wait(lambda s:int(s['wallets'][0].get('balance','0'))>=10000000)
        member.Api('nickname',nickname='研究者乙');Included(member,member.Api('room',name='论文讨论'))
        creator.Wait(lambda s:any(r['name']=='论文讨论' for r in s['rooms']))
        member.Api('message',room=1,body='任何成员都可以聊天和创建分组')
        creator.Wait(lambda s:any(m['body']=='任何成员都可以聊天和创建分组' for m in s['messages']))
        checks.append('one identity and one node per app; ordinary transfer funds joining member; noncreator creates a group and chats without approval')
        for action,values in [('fund',{}),('members_only',{'enabled':True}),('member',{}),('room_open',{})]:
            try:creator.Api(action,**values);raise AssertionError('Privileged action still available: '+action)
            except urllib.error.HTTPError as error:assert error.code==400
        checks.append('creator has no funding, membership approval or room shutdown API')
        memberInvite=member.Api('invite',seeds=[f"[::1]:{member.Api('status')['network']['ports'][0]}"])
        same=member.Api('status');pid=member.NodeProcesses()[0].pid
        member.Api('join',invite=memberInvite)
        assert member.Api('status')['network']['id']==same['network']['id'] and member.NodeProcesses()[0].pid==pid
        checks.append('importing an invitation for the connected chain reuses the current identity, node and ledger')
        creator.Api('stop')
        peer=App(args.bin.resolve(),source,root/'peer');apps.append(peer);peer.Api('join',invite=memberInvite)
        peer.Wait(lambda s:s.get('contract_ready'))
        peerKey=peer.Api('status')['wallets'][0]['public_key']
        Included(member,member.Api('transfer',receiver=peerKey,amount='2000000'))
        peer.Wait(lambda s:int(s['wallets'][0].get('balance','0'))>=2000000)
        peer.Api('message',room=1,body='创建者离线后，其他成员仍然可以交流')
        member.Wait(lambda s:any(m['body']=='创建者离线后，其他成员仍然可以交流' for m in s['messages']))
        checks.append('joined member exports a valid invite, transfers funds and receives peer chat while creator is offline')
        before=member.Api('status')
        for bad in [{'version':99},dict(memberInvite,genesis_root='00'*32)]:
            try:member.Api('join',invite=bad);raise AssertionError('Invalid invite accepted')
            except urllib.error.HTTPError as error:assert error.code==400
            after=member.Api('status');assert after['running'] and after['network']['id']==before['network']['id']
        checks.append('invalid invitations are rejected without stopping or replacing the active chain')
        peer.Api('stop');member.Api('stop')
        # A real pre-change gateway creates the compatibility fixture when supplied.
        legacyBin=Path(os.environ.get('CHAIN_LEGACY_CHAT_BIN',''))
        if os.environ.get('CHAIN_LEGACY_CHAT_BIN'):
            legacy=App(legacyBin.resolve(),source,root/'legacy',artifact=source/'Contracts/ClassroomChat.legacy-v1.compiled.json');apps.append(legacy)
            old=legacy.Api('create',mode='demo',title='保留的既有链')
            legacy.Api('message',room=1,body='保留原有链上记录')
            old=legacy.Wait(lambda s:any(m['body']=='保留原有链上记录' for m in s['messages']))
            head=old['stats']['head'];publicKeys={w['id']:w['public_key'] for w in old['wallets']}
            legacy.Api('stop');stop=legacy.root/'stop.request';stop.write_text('stop');legacy.process.wait(timeout=45);legacy.process.stdout.close();stop.unlink()
            legacy.binary=args.bin.resolve();legacy.artifact=source/'Contracts/ClassroomChat.compiled.json';legacy.Launch();legacy.Api('unlock',password='isolated-classroom-test')
            upgraded=legacy.Wait(lambda s:s.get('contract_ready') and len(s.get('nodes',[]))==1)
            assert upgraded['network']['id']==old['network']['id'] and upgraded['network']['contract']==old['network']['contract']
            assert upgraded['network']['ports']==old['network']['ports'][:1] and upgraded['stats']['head']==head
            assert upgraded['network']['chat_version']==1 and 'teacher' not in upgraded['network']
            assert {w['id']:w['public_key'] for w in upgraded['wallets']}==publicKeys
            assert any(m['body']=='保留原有链上记录' for m in upgraded['messages'])
            assert len(legacy.NodeProcesses())==1
            checks.append('real 0.1.5 three-node chain resumes as one peer node with all original keys, endpoint, ledger, contract and messages intact')
        (root/'result.json').write_text(json.dumps({'checks':checks,'passed':len(checks)},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'checks':checks,'passed':len(checks)},ensure_ascii=False))
    finally:
        errors=[]
        for app in reversed(apps):
            try:app.Stop()
            except Exception as error:errors.append(repr(error))
        (root/'database-cleanup.json').write_text(json.dumps({'processes_stopped':not errors,'remaining_databases':[str(p) for p in root.glob('*/data/networks/*/node*/db')]},indent=2))
        if errors:raise RuntimeError('; '.join(errors))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--bin',type=Path,required=True);p.add_argument('--work',type=Path,required=True);Main(p.parse_args())
