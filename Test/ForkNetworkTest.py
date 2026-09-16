"""Partition/reconnect regression; all state lives under --work."""
import argparse
from NetworkTest import *

def MakeBlock(parent, height, transactions):
    hashes = [t[80:112] for t in transactions]
    while len(hashes) > 1:
        if len(hashes) % 2:
            hashes.append(hashes[-1])
        hashes = [hashlib.blake2b(hashes[i]+hashes[i+1], digest_size=32).digest()
                  for i in range(0, len(hashes), 2)]
    header = parent + hashes[0] + struct.pack('<QQ', height, len(transactions))
    return header + hashlib.blake2b(header, digest_size=32).digest() + b''.join(transactions)

def TestFork(binary, work):
    work.mkdir(parents=True, exist_ok=False)
    dataset = work/'transactions.bin'
    Run([binary/'sender.exe', '--prepare', dataset, '--count', 16])
    raw = dataset.read_bytes()
    txs = [raw[i:i+176] for i in range(0, len(raw), 176)]
    nodes, checks = [], []
    try:
        a = StartNode(binary, work, 'a', produce=0)
        b = StartNode(binary, work, 'b', produce=0)
        nodes.extend([a,b])
        genesis = bytes.fromhex(GetStats(a['port'])['head'])
        first = MakeBlock(genesis, 2, [txs[0]])
        other = MakeBlock(genesis, 2, [txs[1],txs[2]])
        SendFrame(a['port'], 7, first)
        SendFrame(b['port'], 7, other)
        WaitStats(a['port'], lambda s: s['height']==2)
        sb = WaitStats(b['port'], lambda s: s['height']==2)
        StopNode(b); nodes.remove(b)
        b = StartNode(binary, work, 'b', produce=0, seed=f"127.0.0.1:{a['port']}")
        nodes.append(b)
        winner = sb['head']
        WaitStats(a['port'], lambda s: s['head']==winner)
        WaitStats(b['port'], lambda s: s['head']==winner)
        config = json.loads((work/'transactions.bin.genesis.json').read_text())
        balances = {item['public_key']:item['balance'] for item in config['accounts']}
        assert GetAccount(a['port'],txs[0][:32].hex())['nonce']==0
        for node in [a,b]:
            assert GetAccount(node['port'],txs[1][:32].hex())['nonce']==1
            assert GetAccount(node['port'],txs[2][:32].hex())['nonce']==1
        checks.append('equal height chooses branch with more valid transactions; native state rolled back')
        # Extend the formerly losing branch past the common ancestor; request parents by hash.
        extended = MakeBlock(first[80:112], 3, [txs[3],txs[4],txs[5]])
        SendFrame(a['port'], 7, extended)
        sa = WaitStats(a['port'], lambda s: s['height']==3)
        WaitStats(b['port'], lambda s: s['head']==sa['head'])
        assert GetAccount(a['port'],txs[1][:32].hex())['nonce']==0
        assert GetAccount(a['port'],txs[0][:32].hex())['nonce']==1
        checks.append('multi-block branch replay and parent-by-hash retrieval')
        StopNode(a); nodes.remove(a)
        a = StartNode(binary, work, 'a', produce=0)
        nodes.append(a)
        assert GetStats(a['port'])['head']==sa['head']
        assert GetAccount(a['port'],txs[1][:32].hex())['nonce']==0
        checks.append('restart preserves selected head and rollback state')

        f = StartNode(binary, work, 'future', produce=0); nodes.append(f)
        good = MakeBlock(first[80:112], 3, [txs[6]])
        invalid = MakeBlock(bytes(32), 3, [txs[7],txs[8]])
        for block in [good,invalid]:
            before = GetStats(f['port'])['block_verify_ms']
            SendFrame(f['port'],7,block)
            WaitStats(f['port'],lambda s:s['block_verify_ms']>before)
        SendFrame(f['port'],7,first)
        WaitStats(f['port'],lambda s:s['head']==good[80:112].hex())
        checks.append('invalid larger future candidate preserves valid alternative')
        report={'checks':checks,'a':GetStats(a['port']),'b':GetStats(b['port'])}
        (work/'result.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({'checks':checks}))
    finally:
        for node in nodes:
            StopNode(node)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--bin',type=Path,required=True); parser.add_argument('--work',type=Path,required=True)
    args=parser.parse_args(); TestFork(args.bin.resolve(),args.work.resolve())
