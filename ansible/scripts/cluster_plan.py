"""Generate a connected fixed-degree graph and a credential-free Ansible inventory."""
import argparse
import collections
import ipaddress
import json
from pathlib import Path
import random
import re


def Identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', value):
        raise ValueError('Invalid cluster or node identifier')
    return value


def Endpoint(host, port):
    return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'


def Graph(count, degree=8, seed=1):
    if type(count) is not int or not 2 <= count <= 16384:
        raise ValueError('Use 2..16384 nodes')
    if type(degree) is not int or degree not in (2, 4, 6, 8, 12, 16, 32, 64):
        raise ValueError('Degree must be 2, 4, 6, 8, 12, 16, 32 or 64')
    if count <= degree:
        return [set(range(count)) - {index} for index in range(count)]
    generator = random.Random(seed)
    order = list(range(count))
    generator.shuffle(order)
    # +/-1 makes a spanning ring. Other symmetric offsets add long connections.
    offsets = [1, *generator.sample(range(2, (count - 1) // 2 + 1), degree // 2 - 1)]
    graph = [set() for _ in range(count)]
    for position, node in enumerate(order):
        for offset in offsets:
            peer = order[(position + offset) % count]
            graph[node].add(peer)
            graph[peer].add(node)
    return graph


def BackupNeighbors(graph, seed=1):
    """Bounded distinct alternatives; the normal graph keeps its original edges."""
    count = len(graph)
    result = []
    for index, neighbors in enumerate(graph):
        available = count - 1 - len(neighbors)
        wanted = min(len(neighbors), available)
        generator = random.Random(f'{seed}:backup:{index}')
        if available <= wanted * 2:
            candidates = [peer for peer in range(count) if peer != index and peer not in neighbors]
            result.append(generator.sample(candidates, wanted))
            continue
        # Avoid building an O(node_count) complement for every sparse node.
        selected = set()
        ordered = []
        while len(ordered) < wanted:
            peer = generator.randrange(count)
            if peer != index and peer not in neighbors and peer not in selected:
                selected.add(peer)
                ordered.append(peer)
        result.append(ordered)
    return result


def ValidateNodes(rows):
    if not isinstance(rows, list) or not 2 <= len(rows) <= 16384:
        raise ValueError('nodes must contain 2..16384 machines')
    result, names, endpoints = [], set(), set()
    allowed = {'id', 'host', 'ansible_host', 'ansible_user', 'ansible_port', 'port', 'bind', 'query_host'}
    for row in rows:
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError('Unexpected node field; keep passwords and private keys outside the plan')
        name = Identifier(row['id'])
        host = str(ipaddress.ip_address(row['host']))
        port = row.get('port', 18089)
        if name in names or type(port) is not int or not 1024 <= port <= 65535 or (host, port) in endpoints:
            raise ValueError('Duplicate node/endpoint or invalid port')
        node = {**row, 'id': name, 'host': host, 'port': port}
        node['ansible_host'] = str(ipaddress.ip_address(row.get('ansible_host', host)))
        node['query_host'] = str(ipaddress.ip_address(row.get('query_host', host)))
        node['bind'] = str(ipaddress.ip_address(row.get('bind', '::' if ':' in host else '0.0.0.0')))
        if 'ansible_user' in row and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,31}', row['ansible_user']):
            raise ValueError('Invalid SSH username')
        if 'ansible_port' in row and (type(row['ansible_port']) is not int or not 1 <= row['ansible_port'] <= 65535):
            raise ValueError('Invalid SSH port')
        names.add(name)
        endpoints.add((host, port))
        result.append(node)
    return result


def MakePlan(rows, cluster, degree=8, seed=1, injectors=4):
    cluster = Identifier(cluster)
    nodes = ValidateNodes(rows)
    if any(len(cluster) + len(node['id']) > 99 for node in nodes):
        raise ValueError('Cluster and node names together must fit the service name limit')
    if type(injectors) is not int or not 1 <= injectors <= min(64, len(nodes)):
        raise ValueError('Use 1..64 injectors, no more than the node count')
    graph = Graph(len(nodes), degree, seed)
    backups = BackupNeighbors(graph, seed)
    # A single endpoint initiates each TCP edge; incoming peers complete the degree.
    for index, node in enumerate(nodes):
        node['neighbors'] = [nodes[peer]['id'] for peer in sorted(graph[index])]
        node['seeds'] = [Endpoint(nodes[peer]['host'], nodes[peer]['port'])
                         for peer in sorted(graph[index]) if peer > index]
        node['backup_neighbors'] = [nodes[peer]['id'] for peer in backups[index]]
        node['backup_seeds'] = [Endpoint(nodes[peer]['host'], nodes[peer]['port']) for peer in backups[index]]
    distances, pending = {0: 0}, collections.deque([0])
    while pending:
        node = pending.popleft()
        for peer in graph[node]:
            if peer not in distances:
                distances[peer] = distances[node] + 1
                pending.append(peer)
    if len(distances) != len(nodes):
        raise ValueError('Generated graph is disconnected')
    return {'schema': 1, 'cluster': cluster, 'seed': seed, 'requested_degree': degree,
            'nodes': nodes, 'injectors': [node['id'] for node in nodes[:injectors]],
            'graph': {'nodes': len(nodes), 'edges': sum(map(len, graph)) // 2,
                      'min_degree': min(map(len, graph)), 'max_degree': max(map(len, graph)),
                      'max_distance_from_first_node': max(distances.values()), 'connected': True}}


def Inventory(plan):
    hosts = {}
    for node in plan['nodes']:
        hosts[node['id']] = {key: node[key] for key in ('ansible_host', 'ansible_user', 'ansible_port') if key in node}
        hosts[node['id']].update(chain_node_id=node['id'], chain_p2p_address=node['host'],
                                chain_query_address=node['query_host'], chain_cluster_bind=node['bind'],
                                chain_cluster_port=node['port'], chain_test_port=node['port'],
                                chain_service_name=f"chain-vdf-{plan['cluster']}-{node['id']}.service",
                                chain_live_port=node['port'], chain_cluster_peers=node['seeds'],
                                chain_cluster_backup_peers=node['backup_seeds'],
                                chain_cluster_degree=len(node['neighbors']))
    return {'all': {'children': {'chain': {'hosts': hosts, 'vars': {
        'chain_cluster_id': plan['cluster'], 'chain_serial': 32,
        'chain_cluster_node_count': len(plan['nodes'])}}}}}


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', type=Path, required=True, help='JSON array of actual machines')
    parser.add_argument('--cluster', required=True)
    parser.add_argument('--degree', type=int, default=8)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--injectors', type=int, default=4)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = MakePlan(json.loads(args.nodes.read_text(encoding='utf-8')), args.cluster,
                    args.degree, args.seed, args.injectors)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in (('plan.json', plan), ('inventory.json', Inventory(plan))):
        (args.output / name).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(plan['graph']))


if __name__ == '__main__':
    Main()
