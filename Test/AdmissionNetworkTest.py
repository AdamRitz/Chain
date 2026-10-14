"""Native admission feedback and the reliable sender on isolated local data."""
import argparse,hashlib,json,socket,struct,time
from pathlib import Path
from NetworkTest import Run,ExeName,StartNode,StopNode,WaitStats,ReadAll,Frame,VerifyAccounts,GetStats

def Reply(connection,payload):
    connection.sendall(Frame(44,payload))
    kind,count=struct.unpack('<BI',ReadAll(connection,5))
    assert kind==45 and count==len(payload)//176
    return ReadAll(connection,count)

def FullBlockBatch(binary,work):
    work.mkdir(parents=True)
    dataset=work/'transactions.bin'
    Run([binary/ExeName('sender'),'--prepare',dataset,'--count',24000,'--wallets',64])
    txs=dataset.read_bytes();node=StartNode(binary,work,'full-batch',produce=0)
    try:
        parent=bytes.fromhex(GetStats(node['port'])['head']);blocks=[]
        for index in range(4):
            payload=txs[index*6000*176:(index+1)*6000*176]
            leaves=[payload[offset+80:offset+112] for offset in range(0,len(payload),176)]
            while len(leaves)>1:
                if len(leaves)%2:leaves.append(leaves[-1])
                leaves=[hashlib.blake2b(leaves[i]+leaves[i+1],digest_size=32).digest() for i in range(0,len(leaves),2)]
            header=parent+leaves[0]+struct.pack('<QQ',index+2,6000)
            parent=hashlib.blake2b(header,digest_size=32).digest();blocks.append(header+parent+payload)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));peer_port=probe.getsockname()[1]
        with socket.create_connection(('127.0.0.1',node['port']),timeout=10) as connection:
            connection.sendall(b'\x03'+struct.pack('<H',peer_port))
            # Headers that claim four full blocks must not trap the connection in capacity waiting.
            malformed=struct.pack('<H',4)+b''.join(struct.pack('<I',112)+block[:112] for block in blocks)
            connection.sendall(Frame(43,malformed)+Frame(13))
            while True:
                kind,size=struct.unpack('<BI',ReadAll(connection,5));response=ReadAll(connection,size)
                if kind==14:
                    assert not json.loads(response)['failed'];break
            for block in blocks:
                connection.sendall(Frame(43,struct.pack('<H',1)+struct.pack('<I',len(block))+block))
            stats=WaitStats(node['port'],lambda s:s['height']==5 and s['chain_transactions']==24000 and s['pending']==0,timeout=10)
            assert stats['head']==parent.hex() and stats['invalid']==0 and stats['rejected']==0
            VerifyAccounts(node['port'],dataset,24000,str(dataset)+'.genesis.json')
    finally:StopNode(node)

def Check(binary,work):
    work.mkdir(parents=True)
    dataset=work/'transactions.bin'
    Run([binary/ExeName('sender'),'--prepare',dataset,'--count',20000,'--wallets',16])
    txs=dataset.read_bytes(); node=StartNode(binary,work,'admission',produce=0,**{'max-pool':6000})
    try:
        with socket.create_connection(('127.0.0.1',node['port']),timeout=10) as connection:
            connection.sendall(b'\x02')
            future=txs[16*176:5416*176]
            assert set(Reply(connection,future))=={0}
            busy=txs[5416*176:5417*176]
            assert Reply(connection,busy)==b'\x02'
            assert Reply(connection,txs[:16*176])==b'\x00'*16
            assert Reply(connection,txs[:16*176])==b'\x01'*16
            altered=bytearray(txs[5417*176:5418*176]); altered[112]^=1
            assert Reply(connection,altered)==b'\x03'
        stats=WaitStats(node['port'],lambda s:s['ready_pool']==5416)
        assert stats['gap_accepted']>0 and stats['pool_busy']>0
    finally:StopNode(node)
    FullBlockBatch(binary,work/'full-block-batch')
    node=StartNode(binary,work,'reliable',**{'max-pool':6000})
    try:
        result=Run([binary/ExeName('sender'),'--host','127.0.0.1','--port',node['port'],'--file',dataset,
                    '--count',20000,'--batch',512,'--connections',4,'--reliable',1],timeout=60)
        assert result['sent']==20000
        stats=WaitStats(node['port'],lambda s:s['chain_transactions']==20000 and s['pool']==0)
        assert not stats['failed']
        VerifyAccounts(node['port'],dataset,20000,str(dataset)+'.genesis.json')
    finally:StopNode(node)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--bin',type=Path,required=True);parser.add_argument('--work',type=Path,required=True)
    args=parser.parse_args();Check(args.bin.resolve(),args.work.resolve()/str(time.time_ns()))
    print(json.dumps({'checks':['gap capacity','known/busy/invalid feedback','malformed batch releases the connection','four full blocks across bounded synchronization frames','reliable sender','complete balances and nonces']}))
