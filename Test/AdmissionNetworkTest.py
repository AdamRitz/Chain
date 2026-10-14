"""Native admission feedback and the reliable sender on isolated local data."""
import argparse,json,socket,struct,time
from pathlib import Path
from NetworkTest import Run,ExeName,StartNode,StopNode,WaitStats,ReadAll,Frame,VerifyAccounts

def Reply(connection,payload):
    connection.sendall(Frame(44,payload))
    kind,count=struct.unpack('<BI',ReadAll(connection,5))
    assert kind==45 and count==len(payload)//176
    return ReadAll(connection,count)

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
    print(json.dumps({'checks':['gap capacity','known/busy/invalid feedback','reliable sender','complete balances and nonces']}))
