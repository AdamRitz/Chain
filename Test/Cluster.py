"""Node inventory, read-only protocol queries and local/SSH lifecycle."""
import concurrent.futures
import ipaddress
import json
from pathlib import Path
import re
import shlex
import socket
import struct
import subprocess
import threading
import time
from ClusterWorker import Handle
from NetworkTest import ExeName, Frame, ReadAll, FLAGS


def ReadInventory(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    nodes = [{**data.get('defaults', {}), **node} for node in data['nodes']]
    if not 2 <= len(nodes) <= 512:
        raise ValueError('Use 2..512 nodes in the inventory')
    identities, endpoints = set(), set()
    for node in nodes:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', node['id']) or node['id'] in identities:
            raise ValueError('Node identifiers must be unique and contain letters, numbers, _ or -')
        node['host'] = str(ipaddress.IPv4Address(node['host']))
        node['port'] = int(node.get('port', 8089))
        if not 1 <= node['port'] <= 65535 or (node['host'], node['port']) in endpoints:
            raise ValueError('Node endpoints must be valid and unique')
        identities.add(node['id']); endpoints.add((node['host'], node['port']))
        if node.get('transport', 'ssh') == 'ssh':
            if not re.fullmatch(r'[A-Za-z0-9_.@-]+', node['ssh']) or node['ssh'].startswith('-'):
                raise ValueError('Invalid SSH destination')
        elif node['transport'] != 'local':
            raise ValueError('Use local or ssh transport')
    return data, nodes


def LocalInventory(binary, root, count):
    probes, nodes = [], []
    try:
        for index in range(count):
            probe = socket.socket(); probe.bind(('127.0.0.1', 0)); probes.append(probe)
            nodes.append({'id': f'node{index+1:03}', 'host': '127.0.0.1', 'port': probe.getsockname()[1],
                          'transport': 'local', 'binary': str((Path(binary)/ExeName('boost')).resolve()),
                          'root': str(Path(root).resolve())})
    finally:
        for probe in probes:
            probe.close()
    return nodes


def Worker(node, request):
    request = {**request, 'root': node['root'], 'node': node['id']}
    if node.get('transport') == 'local':
        return Handle(request)
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-p', str(node.get('ssh_port', 22))]
    if node.get('ssh_key'):
        command += ['-i', str(Path(node['ssh_key']).expanduser())]
    command += [node['ssh'], shlex.join([node.get('python', 'python3'), node['worker']])]
    result = subprocess.run(command, input=json.dumps(request), capture_output=True, text=True,
                            encoding='utf-8', timeout=90, creationflags=FLAGS)
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(f'{node["id"]} worker: {result.stderr or result.stdout}') from error
    if result.returncode or not response.get('ok'):
        raise RuntimeError(f'{node["id"]}: {response.get("error", result.stderr)}')
    return response['result']


class NodeClient:
    def __init__(self, node):
        self.node, self.socket = node, None
        self.lock = threading.Lock()

    def Close(self):
        if self.socket:
            self.socket.close(); self.socket = None

    def Query(self, kind, payload=b'', raw=False):
        with self.lock:
            try:
                if self.socket is None:
                    self.socket = socket.create_connection((self.node['host'], self.node['port']), timeout=5)
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.socket.sendall(b'\x02')
                self.socket.sendall(Frame(kind, payload))
                response, size = struct.unpack('<BI', ReadAll(self.socket, 5))
                if response != kind+1 or size > 16384:
                    raise ValueError('Invalid measurement response')
                data = ReadAll(self.socket, size)
                return data if raw else json.loads(data)
            except BaseException:
                self.Close()
                raise


class Cluster:
    def __init__(self, nodes, run, genesis, settings, output):
        self.nodes, self.run, self.genesis = nodes, run, genesis
        self.settings, self.output = settings, Path(output)
        self.clients = [NodeClient(node) for node in nodes]
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(nodes)))
        self.started, self.offline, self.faulted = set(), set(), set()
        self.starts, self.metadata, self.cleanup_errors = {}, {}, []

    def All(self, function, indices=None):
        indices = list(range(len(self.nodes))) if indices is None else list(indices)
        futures = [self.pool.submit(function, index) for index in indices]
        values, errors = [], []
        for index, future in zip(indices, futures):
            try:
                values.append(future.result())
            except Exception as error:
                values.append(None); errors.append(f'{self.nodes[index]["id"]}: {error}')
        if errors:
            raise RuntimeError('; '.join(errors))
        return values

    def Start(self):
        for index, node in enumerate(self.nodes):
            settings = {**self.settings, **node.get('options', {})}
            offsets = settings.pop('offsets_ms', [0])
            drifts = settings.pop('drifts_ppm', [0])
            settings.update({'clock-offset-ms': offsets[index % len(offsets)],
                             'clock-drift-ppm': drifts[index % len(drifts)],
                             'port': node['port'], 'bind': node.get('bind', node['host']),
                             'max-peers': max(64, len(self.nodes)*2),
                             'max-connections': max(128, len(self.nodes)*2+32)})
            arguments = []
            for key, value in settings.items():
                arguments += ['--'+key, str(int(value) if isinstance(value, bool) else value)]
            # One TCP connection per undirected edge; configured peers reconnect automatically.
            for peer in self.nodes[index+1:]:
                arguments += ['--seed', f'{peer["host"]}:{peer["port"]}']
            self.starts[index] = {'action': 'start', 'run': self.run, 'binary': node['binary'],
                                  'genesis': self.genesis, 'arguments': arguments}
        def StartOne(index):
            self.started.add(index)
            self.metadata[self.nodes[index]['id']] = Worker(self.nodes[index], self.starts[index])
        self.All(StartOne)
        self.WaitReady(range(len(self.nodes)))
        (self.output/'machines.json').write_text(json.dumps(self.metadata, indent=2), encoding='utf-8')

    def WaitReady(self, indices, timeout=40):
        deadline = time.monotonic()+timeout
        waiting = set(indices)
        while waiting and time.monotonic() < deadline:
            for index in list(waiting):
                try:
                    if not self.clients[index].Query(13)['failed']:
                        waiting.remove(index)
                except (OSError, ValueError, ConnectionError):
                    pass
            if waiting:
                time.sleep(.1)
        if waiting:
            raise TimeoutError(f'Nodes did not start: {waiting}')

    def Stats(self):
        return self.All(lambda index: None if index in self.offline else self.clients[index].Query(13))

    def Common(self, stats=None):
        if self.offline:
            return None
        stats = stats or self.Stats()
        upper = min(row['height'] for row in stats)
        def At(height):
            rows = self.All(lambda index: self.clients[index].Query(29, struct.pack('<Q', height)))
            return rows[0] if rows[0] and all(row == rows[0] for row in rows) else None
        value = At(upper)
        if value:
            return value
        lower, best = 1, At(1)
        while lower < upper:
            middle = (lower+upper+1)//2
            value = At(middle)
            if value:
                lower, best = middle, value
            else:
                upper = middle-1
        return best

    def Restart(self, index):
        Worker(self.nodes[index], {**self.starts[index], 'restart': True})
        self.clients[index].Close()
        self.WaitReady([index])
        self.offline.discard(index)

    def StopOne(self, index):
        self.offline.add(index)
        self.clients[index].Close()
        return Worker(self.nodes[index], {'action': 'stop', 'run': self.run})

    def Fault(self, index, peers, settings):
        node = self.nodes[index]
        if not node.get('allow_netem') or not node.get('interface'):
            raise ValueError('Set allow_netem and the dedicated test interface in the node inventory')
        self.faulted.add(index)
        return Worker(node, {'action': 'fault', 'run': self.run, 'interface': node['interface'],
                            'port': node['port'], 'peers': peers, 'settings': settings,
                            'sudo': node.get('sudo_netem', False)})

    def ClearFaults(self):
        for index in list(self.faulted):
            Worker(self.nodes[index], {'action': 'clear-fault', 'run': self.run})
            self.faulted.remove(index)

    def Close(self):
        for index in list(self.faulted):
            try:
                Worker(self.nodes[index], {'action': 'clear-fault', 'run': self.run})
                self.faulted.remove(index)
            except Exception as error:
                self.cleanup_errors.append(str(error))
        for index in sorted(self.started):
            self.clients[index].Close()
            try:
                Worker(self.nodes[index], {'action': 'stop', 'run': self.run})
                data = Worker(self.nodes[index], {'action': 'collect', 'run': self.run})
                (self.output/(self.nodes[index]['id']+'.json')).write_text(json.dumps(data, indent=2), encoding='utf-8')
            except Exception as error:
                self.cleanup_errors.append(str(error))
        self.pool.shutdown(wait=True)
        if self.cleanup_errors:
            (self.output/'cleanup-errors.json').write_text(json.dumps(self.cleanup_errors, indent=2))
