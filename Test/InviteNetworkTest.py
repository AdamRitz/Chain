"""Real IPv6 gateways: human invitations, bounded preflight and live seed updates."""
import argparse,json,socket,time,urllib.error
from pathlib import Path
from ClassroomJoinTest import App

def Main(args):
    source=Path(__file__).resolve().parents[1];root=args.work.resolve();root.mkdir(parents=True,exist_ok=False)
    apps=[];checks=[]
    def New(name):
        app=App(args.bin.resolve(),source,root/name);apps.append(app);return app
    def Preview(app,text,phase='ready'):
        started=time.monotonic();value=app.Api('invite_preview',invite=text)
        assert time.monotonic()-started<2,'preflight blocked the HTTP request'
        return app.Wait(lambda s:s.get('invite_preview',{}).get('id')==value['id'] and s['invite_preview'].get('phase')==phase)['invite_preview']
    def Included(app,tx):
        app.Wait(lambda s:any(t['hash']==tx['hash'] and t['state']=='included' for t in s.get('outbox',[])))
    try:
        a=New('creator');first=a.Api('create',title='邀请与连接测试')
        with socket.socket(socket.AF_INET6) as free:free.bind(('::1',0));closed=free.getsockname()[1]
        good=f"[::1]:{first['network']['ports'][0]}"
        prepared=a.Api('invite_prepare',seeds=[f'[::1]:{closed}',good])
        assert '点击“加入聊天室”' in prepared['share_text'] and 'NORN1:' in prepared['share_text']
        addresses=a.Api('network_addresses');assert addresses['external_verified'] is False
        b=New('member');preview=Preview(b,prepared['share_text'])
        assert preview['seeds']==[good] and len(preview['endpoints'])==2
        b.Api('join',preview_id=preview['id'])
        member=b.Wait(lambda s:s.get('connection_state',{}).get('phase')=='ready')
        Included(a,a.Api('transfer',receiver=member['wallets'][0]['public_key'],amount='5000000'))
        b.Wait(lambda s:int(s['wallets'][0].get('balance','0'))>=5000000)
        checks.append('whole human invitation; unreachable IPv6 fallback; asynchronous preflight; real ready state')

        peer=New('peer');peer.Api('join',invite=prepared['invite']);peer.Wait(lambda s:s.get('contract_ready'))
        newseed=f"[::1]:{peer.Api('status')['network']['ports'][0]}"
        newinvite=peer.Api('invite',seeds=[newseed]);before=b.Api('status');pid=b.NodeProcesses()[0].pid
        updated=Preview(b,json.dumps(newinvite));assert updated['same_chain']
        b.Api('join',preview_id=updated['id'])
        assert b.NodeProcesses()[0].pid==pid and newseed in b.Api('status')['network']['seeds']
        a.Api('stop')
        b.Wait(lambda s:any(p['port']==peer.Api('status')['network']['ports'][0] for n in s.get('nodes',[]) for p in n.get('connections',[])))
        Included(b,b.Api('message',room=1,body='更新入口后无需重启节点'))
        peer.Wait(lambda s:any(m['body']=='更新入口后无需重启节点' for m in s['messages']))
        checks.append('same-chain invitation merges entry without PID change; peer connection and chat survive original creator stopping')

        other=New('other');wrong=other.Api('create',title='另一条链')
        mismatch=dict(newinvite,seeds=[f"[::1]:{wrong['network']['ports'][0]}"])
        bad=Preview(b,json.dumps(mismatch),'error');assert '不一致' in bad['error']
        for values in [{'preview_id':bad['id']},{'preview_id':'stale'}]:
            try:b.Api('join',**values);raise AssertionError('invalid preview accepted')
            except urllib.error.HTTPError as error:assert error.code==400
        assert b.NodeProcesses()[0].pid==pid and b.Api('status')['network']['id']==before['network']['id']
        checks.append('wrong-chain endpoint and invalid preview cannot replace the running chain')

        inbox=b.root/'data/incoming-invites';inbox.mkdir(exist_ok=True)
        (inbox/'invite-test.json').write_text(json.dumps({'text':prepared['share_text']}),encoding='utf-8')
        incoming=b.Api('status')['incoming_invitation'];assert incoming['text']==prepared['share_text']
        b.Api('invite_received',id=incoming['id']);assert not b.Api('status')['incoming_invitation']
        assert b.Api('status')['network']['id']==before['network']['id']
        checks.append('native-file inbox previews content once and never automatically switches chains')
        (root/'result.json').write_text(json.dumps({'checks':checks,'cross_physical_network_tested':False},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':len(checks),'checks':checks},ensure_ascii=False))
    finally:
        errors=[]
        for app in reversed(apps):
            try:app.Stop()
            except Exception as error:errors.append(str(error))
        if errors:raise RuntimeError('; '.join(errors))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--bin',type=Path,required=True);p.add_argument('--work',type=Path,required=True);Main(p.parse_args())
