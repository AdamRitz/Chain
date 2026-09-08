"""Local integration tests. All databases live in a new caller-selected work directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

def Run(args, timeout=90):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout, creationflags=FLAGS)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stdout}\n{result.stderr}')
    return json.loads(result.stdout.strip().splitlines()[-1])

def ReadAll(connection, size):
    data = bytearray()
    while len(data) < size:
        part = connection.recv(size-len(data))
        if not part:
            raise ConnectionError('Unexpected end of frame')
        data.extend(part)
    return bytes(data)

def Frame(kind, data=b''):
    return struct.pack('<BI', kind, len(data)) + data

def GetStats(port):
    with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
        connection.sendall(b'\x02' + Frame(13))
        kind, size = struct.unpack('<BI', ReadAll(connection, 5))
        if kind != 14 or size > 16384:
            raise ValueError('Invalid stats frame')
        return json.loads(ReadAll(connection, size))

def WaitStats(port, predicate, timeout=30):
    end = time.monotonic()+timeout
    latest = None
    while time.monotonic() < end:
        latest = GetStats(port)
        if latest['failed']:
            raise RuntimeError('Node failed')
        if predicate(latest):
            return latest
        time.sleep(.01)
    raise TimeoutError(f'Stats predicate timed out: {latest}')

def SendFrame(port, kind, payload, fragment=False):
    with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
        data = b'\x02' + Frame(kind, payload)
        if fragment:
            for begin in range(0, len(data), 7):
                connection.sendall(data[begin:begin+7])
        else:
            connection.sendall(data)

def StartNode(binary, work, name, **settings):
    directory = work/name
    directory.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    options = {'data': directory/'db', 'port': port, 'bind': '127.0.0.1',
               'io-threads': 2, 'verify-threads': 4, 'block-ms': 10,
               'max-block-txs': 512, 'sync': 1, 'run-seconds': 90,
               'metrics': directory/'final.json'}
    options.update(settings)
    command = [str(binary/'boost.exe')]
    for key, value in options.items():
        command.extend(['--'+key, str(value)])
    log = open(directory/'node.log', 'w', encoding='utf-8')
    process = subprocess.Popen(command, cwd=directory, stdout=log, stderr=log,
                               creationflags=FLAGS)
    result = {'process': process, 'port': port, 'log': log, 'directory': directory}
    end = time.monotonic()+15
    while time.monotonic() < end:
        if process.poll() is not None:
            log.close()
            raise RuntimeError((directory/'node.log').read_text(encoding='utf-8', errors='replace'))
        try:
            GetStats(port)
            return result
        except (OSError, ConnectionError):
            time.sleep(.05)
    StopNode(result)
    raise TimeoutError('Node startup timeout')

def StopNode(node):
    process = node['process']
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    node['log'].close()

def TestNetwork(binary, work):
    work.mkdir(parents=True, exist_ok=False)
    dataset = work/'transactions.bin'
    Run([binary/'sender.exe', '--prepare', dataset, '--count', 4096])
    raw = dataset.read_bytes()
    nodes = []
    checks = []
    try:
        leader = StartNode(binary, work, 'leader', **{'max-pending': 6000})
        nodes.append(leader)
        port = leader['port']
        stalled = socket.create_connection(('127.0.0.1', port), timeout=3)
        assert GetStats(port)['height'] == 1
        stalled.close()
        checks.append('slow handshake does not block accept loop')
        for kind, size in [(1, 175), (10, 177), (2, 0xffffffff), (255, 0)]:
            with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
                connection.sendall(b'\x02'+struct.pack('<BI', kind, size))
                assert connection.recv(1) == b''
        checks.append('bad type, oversized and malformed length rejected before payload allocation')
        invalid = bytearray(raw[:176])
        invalid[112] ^= 1
        SendFrame(port, 1, invalid)
        WaitStats(port, lambda x: x['invalid'] == 1)
        stats = GetStats(port)
        header = bytes.fromhex(stats['head']) + bytes(invalid[80:112]) + struct.pack('<QQ', 2, 1)
        block = header + hashlib.blake2b(header, digest_size=32).digest() + invalid
        SendFrame(port, 7, block)
        time.sleep(.15)
        assert GetStats(port)['height'] == 1
        checks.append('invalid signatures rejected in transaction and sync block paths')
        SendFrame(port, 1, raw[:176], fragment=True)
        SendFrame(port, 10, raw[176:528])
        WaitStats(port, lambda x: x['committed'] == 3)
        SendFrame(port, 1, raw[:176])
        WaitStats(port, lambda x: x['duplicate'] >= 1)
        assert GetStats(port)['committed'] == 3
        checks.append('fragmented frame and complete batch handled; replay does not commit twice')
        Run([binary/'sender.exe', '--file', dataset, '--offset', 3, '--count', 4093,
             '--port', port, '--connections', 4, '--batch', 128])
        expected = WaitStats(port, lambda x: x['committed'] == 4096 and x['pending'] == 0)
        assert expected['pool'] == 0 and expected['rejected'] == 0
        checks.append('4096 unique network transactions committed without loss')
        follower = StartNode(binary, work, 'follower', produce=0, seed=f'127.0.0.1:{port}')
        nodes.append(follower)
        synced = WaitStats(follower['port'], lambda x: x['head'] == expected['head'], timeout=30)
        assert synced['height'] == expected['height']
        checks.append('late follower verifies history and reaches same height/head')
        # A new transaction must also be broadcast over the existing live peer connection.
        fresh = work/'fresh.bin'
        Run([binary/'sender.exe', '--prepare', fresh, '--count', 16])
        Run([binary/'sender.exe', '--file', fresh, '--count', 16, '--port', port])
        expected = WaitStats(port, lambda x: x['committed'] == 4112)
        WaitStats(follower['port'], lambda x: x['head'] == expected['head'])
        checks.append('live block broadcast and bidirectional peer connection remain active')
        StopNode(follower)
        nodes.remove(follower)
        StopNode(leader)
        nodes.remove(leader)
        restarted = StartNode(binary, work, 'leader')
        nodes.append(restarted)
        recovered = GetStats(restarted['port'])
        assert recovered['head'] == expected['head'] and recovered['height'] == expected['height']
        SendFrame(restarted['port'], 1, raw[:176])
        replay = WaitStats(restarted['port'], lambda x: x['duplicate'] == 1)
        assert replay['committed'] == 0 and replay['pool'] == 0
        checks.append('process termination/restart restores committed head and rejects replay')
        graceful = StartNode(binary, work, 'graceful', **{'run-seconds': 2})
        nodes.append(graceful)
        with socket.create_connection(('127.0.0.1', graceful['port']), timeout=3) as active_peer:
            active_peer.sendall(b'\x01'+struct.pack('<H', 65500))
            WaitStats(graceful['port'], lambda x: x['peers'] == 1)
            assert graceful['process'].wait(timeout=8) == 0
        assert json.loads((graceful['directory']/'final.json').read_text())['height'] == 1
        checks.append('timed graceful shutdown with an active peer exits cleanly and writes metrics')
    finally:
        for node in nodes:
            StopNode(node)
    result = {'passed': len(checks), 'checks': checks, 'work': str(work)}
    (work/'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bin', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(TestNetwork(args.bin.resolve(), args.work.resolve()/str(time.time_ns())), indent=2), flush=True)
