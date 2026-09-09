"""Account CLI, signed transfers and replicated state in isolated local databases."""
import argparse
import json
from pathlib import Path
import time
from NetworkTest import Run, StartNode, StopNode, SendFrame, GetStats, GetAccount, WaitStats


def TestAccounts(binary, work):
    work.mkdir(parents=True, exist_ok=False)
    alice_file, bob_file = work/'alice.wallet.json', work/'bob.wallet.json'
    alice = Run([binary/'sender.exe', '--create-wallet', alice_file])['public_key']
    bob = Run([binary/'sender.exe', '--create-wallet', bob_file])['public_key']
    genesis = work/'genesis.json'
    genesis.write_text(json.dumps({'base_fee': 1, 'accounts': [{'public_key': alice, 'balance': 10000}]}), encoding='utf-8')
    nodes = []
    checks = []
    def Transaction(wallet, receiver, amount, nonce, name):
        output = work/(name+'.bin')
        Run([binary/'sender.exe', '--wallet', wallet, '--receiver', receiver,
             '--amount', amount, '--nonce', nonce, '--prepare', output])
        result = output.read_bytes()
        assert len(result) == 176
        return result
    try:
        leader = StartNode(binary, work, 'leader', genesis=genesis)
        nodes.append(leader)
        port = leader['port']
        assert GetAccount(port, alice) == {'exists': True, 'balance': 10000, 'nonce': 0}
        assert GetAccount(port, bob) == {'exists': False, 'balance': 0, 'nonce': 0}
        checks.append('explicit genesis funding and zero balance for a new public key')
        second = Transaction(alice_file, bob, 10, 2, 'second')
        first = Transaction(alice_file, bob, 10, 1, 'first')
        SendFrame(port, 1, second)
        WaitStats(port, lambda s: s['pool'] == 1 and s['ready_pool'] == 0)
        SendFrame(port, 1, first)
        WaitStats(port, lambda s: s['committed'] == 2 and s['pool'] == 0)
        assert GetAccount(port, alice) == {'exists': True, 'balance': 9978, 'nonce': 2}
        assert GetAccount(port, bob) == {'exists': True, 'balance': 20, 'nonce': 0}
        assert GetStats(port)['user_burned'] == 2
        checks.append('out-of-order transfers execute in nonce order with exact balances and fees')
        queried = Run([binary/'sender.exe', '--account', alice, '--port', port])
        assert queried['balance'] == 9978 and queried['nonce'] == 2
        checks.append('account query CLI returns committed state')
        self_tx = Transaction(alice_file, alice, 5, 3, 'self')
        SendFrame(port, 1, self_tx)
        WaitStats(port, lambda s: s['committed'] == 3)
        assert GetAccount(port, alice)['balance'] == 9977
        checks.append('self transfer charges one nonrecoverable base fee')
        invalid = Transaction(alice_file, bob, 10000, 4, 'insufficient')
        SendFrame(port, 1, invalid)
        WaitStats(port, lambda s: s['invalid_user_tx'] == 1)
        assert GetStats(port)['committed'] == 3
        checks.append('insufficient balance transaction is rejected over the network')
        follower = StartNode(binary, work, 'follower', genesis=genesis, produce=0, seed=f'127.0.0.1:{port}')
        nodes.append(follower)
        expected = GetStats(port)
        WaitStats(follower['port'], lambda s: s['head'] == expected['head'])
        for key in [alice, bob]:
            assert GetAccount(follower['port'], key) == GetAccount(port, key)
        assert GetStats(follower['port'])['user_burned'] == 3
        checks.append('historical synchronization replays balances, nonces and burned fees')
        StopNode(leader)
        nodes.remove(leader)
        restarted = StartNode(binary, work, 'leader')
        nodes.append(restarted)
        assert GetStats(restarted['port'])['head'] == expected['head']
        assert GetAccount(restarted['port'], alice)['balance'] == 9977
        assert GetStats(restarted['port'])['base_fee'] == 1
        checks.append('restart restores genesis policy and account state without the external genesis file argument')
    finally:
        for node in nodes:
            StopNode(node)
    return {'passed': len(checks), 'checks': checks}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(TestAccounts(args.bin.resolve(), args.work.resolve()/str(time.time_ns())), indent=2))
