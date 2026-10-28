"""Two independent local app installations join over IPv6 and exchange funded chat transactions."""
import argparse,json,os,subprocess,time,urllib.request,urllib.error,shutil
from pathlib import Path
import psutil
from NetworkTest import FLAGS,ExeName

class App:
    def __init__(self,binary,source,root):
        self.root=root;root.mkdir(parents=True);self.log=(root/'stderr.txt').open('w')
        self.process=subprocess.Popen([str(binary/ExeName('chain_classroom')),'--data',str(root/'data'),'--node',str(binary/ExeName('boost')),'--web',str(source/'Classroom/dist'),'--contract',str(source/'Contracts/ClassroomChat.compiled.json'),'--stop-file',str(root/'stop.request')],stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',creationflags=FLAGS)
        ready=json.loads(self.process.stdout.readline());self.origin,self.token=ready['url'].split('/#')
        self.Api('wallet_create',password='isolated-classroom-test')
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
    try:
        teacher=App(args.bin.resolve(),source,root/'teacher');apps.append(teacher)
        status=teacher.Api('create',mode='teacher',title='IPv6 同班教学测试');port=status['network']['ports'][0]
        original=status['network']['genesis_root'];invite=teacher.Api('invite',seeds=[f'[::1]:{port}'])
        student=App(args.bin.resolve(),source,root/'student');apps.append(student);student.Api('join',invite=invite)
        studentStatus=student.Wait(lambda s:s.get('contract_ready') and s.get('messages') is not None)
        own=next(w for w in studentStatus['wallets'] if w['id']=='alice');assert own['balance']=='0'
        teacher.Api('fund',receiver=own['public_key'],amount='10000000')
        student.Wait(lambda s:any(w['id']=='alice' and int(w.get('balance','0'))>=10000000 for w in s['wallets']))
        student.Api('nickname',nickname='加入课堂的学生');student.Api('message',room=1,body='来自独立学生钱包的 IPv6 消息')
        teacher.Wait(lambda s:any(m['body']=='来自独立学生钱包的 IPv6 消息' and m['sender']==own['evm_address'] for m in s.get('messages',[])))
        checks.append('two independently encrypted app vaults join through IPv6; normal teacher transfer funds student chat')
        try:student.Api('members_only',enabled=True);raise AssertionError('Student became teacher')
        except urllib.error.HTTPError as e:assert e.code==400
        checks.append('student app cannot invoke teacher administration')
        teacher.Api('room',name='实验分组')
        teacher.Wait(lambda s:len(s.get('rooms',[]))==2)
        teacher.Api('room_open',room=2,open=False)
        student.Wait(lambda s:any(r['id']==2 and not r['open'] for r in s.get('rooms',[])))
        closed=student.Api('message',room=2,body='关闭房间的拒绝测试')
        student.Wait(lambda s:any(t['hash']==closed['hash'] and t.get('execution_status',0)!=0 for t in s.get('outbox',[])))
        teacher.Api('members_only',enabled=True)
        teacher.Wait(lambda s:all(t['state']=='included' for t in s.get('outbox',[])))
        denied=student.Api('message',room=1,body='尚未授权的成员')
        student.Wait(lambda s:any(t['hash']==denied['hash'] and t.get('execution_status',0)!=0 for t in s.get('outbox',[])))
        teacher.Api('member',member=own['evm_address'],allowed=True)
        teacher.Wait(lambda s:all(t['state']=='included' for t in s.get('outbox',[])))
        student.Api('message',room=1,body='授权成员恢复正常发言')
        teacher.Wait(lambda s:any(m['body']=='授权成员恢复正常发言' for m in s.get('messages',[])))
        checks.append('on-chain room closure and membership restrictions reject calls; explicit authorization restores chat')
        student.Api('stop');teacher.Api('create',mode='demo',title='另一个独立实验')
        assert teacher.Api('status')['network']['genesis_root']!=original
        checks.append('new experiments have distinct genesis fingerprints even when reusing the local wallet')
        (root/'result.json').write_text(json.dumps(dict(checks=checks),ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(dict(checks=checks),ensure_ascii=False))
    finally:
        errors=[]
        for app in reversed(apps):
            try:app.Stop()
            except Exception as error:errors.append(repr(error))
        (root/'database-cleanup.json').write_text(json.dumps(dict(processes_stopped=True,remaining_databases=[str(p) for p in root.glob('*/data/networks/*/node*/db')]),indent=2))
        if errors:raise RuntimeError('; '.join(errors))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--bin',type=Path,required=True);p.add_argument('--work',type=Path,required=True);Main(p.parse_args())
