"""EVM deployment, gas, rollback, replication and restart through the public wire protocol."""
import argparse
from NetworkTest import *

def InitCode(runtime):
    size=len(bytes.fromhex(runtime))
    assert size<256
    return f'60{size:02x}600c60003960{size:02x}6000f3'+runtime

def QueryJson(port, kind, expected, payload):
    with socket.create_connection(('127.0.0.1',port),timeout=5) as connection:
        connection.sendall(b'\x02'+Frame(kind,payload))
        response,size=struct.unpack('<BI',ReadAll(connection,5))
        assert response==expected and size<=2097152
        return json.loads(ReadAll(connection,size))

def Receipt(port,tx):
    return QueryJson(port,23,24,bytes.fromhex(tx))

def Contract(port,address):
    return QueryJson(port,25,26,bytes.fromhex(address))

def WaitReceipt(port,tx):
    end=time.monotonic()+15
    while time.monotonic()<end:
        receipt=Receipt(port,tx)
        if receipt is not None:
            return receipt
        time.sleep(.02)
    raise TimeoutError(f'Receipt missing: {tx}, {GetStats(port)}')

def TestEvm(binary,work):
    work.mkdir(parents=True,exist_ok=False)
    wallet=work/'owner.wallet.json'
    key=Run([binary/'sender.exe','--create-wallet',wallet])['public_key']
    genesis=work/'genesis.json'
    genesis.write_text(json.dumps({'base_fee':2,'accounts':[{'public_key':key,'balance':1000000000}]}))
    nodes,checks=[],[]
    def Submit(node,nonce,**options):
        args=[binary/'evm_sender.exe','--wallet',wallet,'--genesis',genesis,'--nonce',nonce,'--port',node['port']]
        for name,value in options.items(): args += ['--'+name,str(value)]
        sent=Run(args)
        return sent['transaction_hash'],WaitReceipt(node['port'],sent['transaction_hash'])
    try:
        a=StartNode(binary,work,'producer',genesis=genesis); nodes.append(a)
        runtime='60005460010160005560005460005260206000f3'
        _,deployed=Submit(a,1,deploy=InitCode(runtime))
        assert deployed['status']==0
        address=deployed['contract']
        assert Contract(a['port'],address)['code']==runtime
        callHash,called=Submit(a,2,call=address,input='')
        assert called['status']==0 and int(called['output'],16)==1
        assert int(Contract(a['port'],address)['storage']['00'*32],16)==1
        checks.append('deploy and execute persistent storage with return value')
        _,reverter=Submit(a,3,deploy=InitCode('602a60005560006000fd'))
        _,reverted=Submit(a,4,call=reverter['contract'],input='')
        assert reverted['status']==2 and Contract(a['port'],reverter['contract'])['storage']=={}
        assert GetAccount(a['port'],key)['nonce']==4
        checks.append('REVERT restores storage and consumes nonce and actual gas')
        _,exhausted=Submit(a,5,call=address,input='',gas=21000)
        assert exhausted['status']==3 and exhausted['gas_used']==21000
        assert int(Contract(a['port'],address)['storage']['00'*32],16)==1
        checks.append('out of gas restores contract state')
        _,sha=Submit(a,6,call='00'*19+'02',input='616263')
        assert sha['status']==0 and sha['output']==hashlib.sha256(b'abc').hexdigest()
        checks.append('SHA256 precompile returns the expected digest')
        prepared=work/'tamper.bin'
        Run([binary/'evm_sender.exe','--wallet',wallet,'--genesis',genesis,'--nonce',7,'--call',address,'--input','00','--prepare',prepared])
        tx=bytearray(prepared.read_bytes()[4:]); tx[-1]^=1
        before=GetStats(a['port'])['invalid']
        SendFrame(a['port'],17,tx)
        WaitStats(a['port'],lambda s:s['invalid']>before)
        assert GetAccount(a['port'],key)['nonce']==6
        checks.append('signature binds full contract input')
        compiled=json.loads((Path(__file__).resolve().parents[1]/'Contracts/Counter.compiled.json').read_text())['contracts']
        counter=compiled['Counter']; caller=compiled['Caller']
        counterMethods=counter['evm']['methodIdentifiers']; callerMethods=caller['evm']['methodIdentifiers']
        _,solidity=Submit(a,7,deploy=counter['evm']['bytecode']['object'],gas=1000000)
        assert solidity['status']==0
        solidityAddress=solidity['contract']
        _,increment=Submit(a,8,call=solidityAddress,input=counterMethods['Increment()'])
        assert increment['status']==0 and int(increment['output'],16)==1 and len(increment['logs'])==1
        _,forwarder=Submit(a,9,deploy=caller['evm']['bytecode']['object'],gas=1000000)
        _,forwarded=Submit(a,10,call=forwarder['contract'],input=callerMethods['Forward(address)']+'00'*12+solidityAddress)
        assert forwarded['status']==0 and int(forwarded['output'],16)==2
        _,rollback=Submit(a,11,call=solidityAddress,input=counterMethods['Rollback()'])
        assert rollback['status']==2 and int(Contract(a['port'],solidityAddress)['storage']['00'*32],16)==2
        checks.append('Solidity 0.8.28 Shanghai bytecode events nested CALL and revert')
        modular=(1).to_bytes(32,'big')*3+bytes([3,5,7])
        _,modexp=Submit(a,12,call='00'*19+'05',input=modular.hex())
        assert modexp['status']==0 and modexp['output']=='05'
        checks.append('modular exponentiation precompile')
        head=GetStats(a['port'])['head']
        b=StartNode(binary,work,'follower',genesis=genesis,produce=0,seed=f"127.0.0.1:{a['port']}"); nodes.append(b)
        WaitStats(b['port'],lambda s:s['head']==head)
        assert Contract(a['port'],address)==Contract(b['port'],address)
        assert Receipt(a['port'],callHash)==Receipt(b['port'],callHash)
        assert GetAccount(a['port'],key)==GetAccount(b['port'],key)
        checks.append('follower independently replays code storage fees and receipts')
        balance=GetAccount(a['port'],key)
        StopNode(a); nodes.remove(a)
        a=StartNode(binary,work,'producer',genesis=genesis); nodes.append(a)
        assert GetStats(a['port'])['head']==head and GetAccount(a['port'],key)==balance
        assert Contract(a['port'],address)==Contract(b['port'],address)
        checks.append('restart recovers contract state and supply')
        # Partition two producers at an identical contract state, then write different values.
        StopNode(b); nodes.remove(b)
        b=StartNode(binary,work,'follower',genesis=genesis); nodes.append(b)
        leftHash,left=Submit(a,13,call=solidityAddress,input=counterMethods['Set(uint256)']+f'{7:064x}')
        rightHash,right=Submit(b,13,call=solidityAddress,input=counterMethods['Set(uint256)']+f'{9:064x}')
        leftHead=GetStats(a['port'])['head']; rightHead=GetStats(b['port'])['head']
        assert leftHead!=rightHead
        expectedHead=min(leftHead,rightHead)
        expectedValue=7 if expectedHead==leftHead else 9
        lostHash=rightHash if expectedHead==leftHead else leftHash
        keptHash=leftHash if expectedHead==leftHead else rightHash
        StopNode(b); nodes.remove(b)
        b=StartNode(binary,work,'follower',genesis=genesis,produce=0,seed=f"127.0.0.1:{a['port']}"); nodes.append(b)
        WaitStats(a['port'],lambda s:s['head']==expectedHead)
        WaitStats(b['port'],lambda s:s['head']==expectedHead)
        for node in [a,b]:
            assert int(Contract(node['port'],solidityAddress)['storage']['00'*32],16)==expectedValue
            assert Receipt(node['port'],lostHash) is None
            assert Receipt(node['port'],keptHash)['status']==0
        assert GetAccount(a['port'],key)==GetAccount(b['port'],key)
        checks.append('same height tie converges by hash; EVM storage receipts native balances and fees follow selected branch')
        (work/'result.json').write_text(json.dumps({'checks':checks,'gas':{'deploy':deployed['gas_used'],'call':called['gas_used'],'revert':reverted['gas_used']}},indent=2))
        print(json.dumps({'checks':checks}))
    finally:
        for node in nodes: StopNode(node)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--bin',type=Path,required=True); parser.add_argument('--work',type=Path,required=True)
    args=parser.parse_args(); TestEvm(args.bin.resolve(),args.work.resolve())
