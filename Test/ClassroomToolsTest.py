"""Real gateway/helper diagnostic integration. No firewall mutation or UAC approval."""
import argparse,json,socket,time,urllib.error
from pathlib import Path
from ClassroomJoinTest import App

def Main(args):
    root=args.work.resolve();root.mkdir(parents=True,exist_ok=False)
    app=App(args.bin.resolve(),Path(__file__).resolve().parents[1],root/'app')
    checks=[]
    try:
        state=app.Api('create',mode='demo',title='Network tools integration')
        port=state['network']['ports'][0]
        try: app.Api('network_configure');raise AssertionError('Demo opened inbound firewall')
        except urllib.error.HTTPError as e: assert e.code==400
        checks.append('demo cannot request an inbound rule')
        start=time.monotonic();reply=app.Api('network_status')
        assert reply['network_tools']['busy'] and time.monotonic()-start<2
        before=reply['stats']['height']
        # An idle chain need not create empty blocks. Submit actual work while
        # Windows policy inspection is in progress, then observe its receipt.
        start=time.monotonic();sent=app.Api('message',room=1,body='Network inspection does not pause classroom work')
        assert time.monotonic()-start<2
        state=app.Wait(lambda s: not s['network_tools'].get('busy'))
        firewall=state['network_tools']['firewall']
        assert firewall['supported'] and not firewall.get('error'),firewall
        assert firewall['rules']==[], 'This isolated build unexpectedly has a firewall rule'
        checks.append('real Windows policy/profile/address inspection finishes asynchronously')
        app.Wait(lambda s:any(t['hash']==sent['hash'] and t['state']=='included' for t in s.get('outbox',[])))
        checks.append('nodes keep progressing while diagnostic helper is running')
        app.Api('network_remove')
        state=app.Wait(lambda s:not s['network_tools'].get('busy'))
        assert state['network_tools']['firewall']['operation']=='unchanged'
        checks.append('removing an absent rule does not ask for elevation')
        for endpoint in [f'127.0.0.1:{port}',f'[::1]:{port}']:
            app.Api('network_probe',endpoint=endpoint)
            state=app.Wait(lambda s:not s['network_tools'].get('probing'))
            result=state['network_tools']['probe']
            assert result['ok'] and result['height']>=before,result
        checks.append('real node queries work over IPv4 and IPv6')
        with socket.socket() as closed:
            closed.bind(('127.0.0.1',0));unused=closed.getsockname()[1]
        start=time.monotonic();app.Api('network_probe',endpoint=f'127.0.0.1:{unused}')
        state=app.Wait(lambda s:not s['network_tools'].get('probing'))
        assert not state['network_tools']['probe']['ok'] and time.monotonic()-start<8
        checks.append('unreachable node gives bounded failure instead of success')
        try:app.Api('network_probe',endpoint='::1:1234');raise AssertionError('Malformed IPv6 accepted')
        except urllib.error.HTTPError as e:assert e.code==400
        old=app.token;app.token='invalid'
        try:app.Api('network_remove');raise AssertionError('Unauthenticated action accepted')
        except urllib.error.HTTPError as e:assert e.code==401
        finally:app.token=old
        checks.append('malformed endpoint and unauthorized management requests rejected')
        result={'checks':checks,'passed':len(checks),'firewall_changed':False,'uac_interaction_tested':False}
        (root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result))
    finally:
        app.Stop()
        (root/'cleanup.json').write_text(json.dumps({'processes_stopped':True,'remaining_databases':[str(p) for p in root.glob('*/data/networks/*/node*/db')]}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--bin',type=Path,required=True);p.add_argument('--work',type=Path,required=True);Main(p.parse_args())
