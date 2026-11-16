"""External fixed 20k/30k v0.4.4 retest; caller owns SSH and services.

RunGroup(args, nodes, plan, dataset) preserves native Cluster/ClusterWorker
process identity checks and guarded database deletion. No C++ source changes.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import sys
import time

import LightObserve


REVISION = '9d34b55f1d5bf8ebab1f3fe44a86df31e18b1899'
RATES = [20000, 30000]
HASH = re.compile(r'[0-9a-f]{64}\Z')


def Save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')


def Digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def TraceHead(head, exported):
    """Validate this node's sampled parent chain, including its cumulative score."""
    if not isinstance(head, dict) or exported.get('ok') is not True:
        raise ValueError('missing head or failed header export')
    graph = exported.get('headers')
    if not isinstance(graph, dict) or not HASH.fullmatch(str(head.get('hash', ''))) or not HASH.fullmatch(str(head.get('genesis', ''))):
        raise ValueError('invalid sampled hash/genesis or header graph')
    current = head['hash']
    sampled = graph.get(current)
    if not sampled or sampled.get('height') != head.get('height') or sampled.get('score') != head.get('transactions'):
        raise ValueError('sampled head missing or height/score does not match RPC19')
    expected_genesis = hashlib.blake2b(bytes(32) + bytes.fromhex(head['genesis']) + struct.pack('<QQ', 1, 0), digest_size=32).hexdigest()
    ancestors = {}
    while True:
        if current in ancestors:
            raise ValueError('cycle in sampled header ancestry')
        header = graph.get(current)
        if not isinstance(header, dict) or header.get('hash') != current:
            raise ValueError('missing header in this node ancestry')
        if any(type(header.get(key)) is not int or header[key] < 0 for key in ('height', 'txnum', 'score')) or header['height'] < 1:
            raise ValueError('invalid header height/transaction score')
        if not HASH.fullmatch(str(header.get('parent', ''))):
            raise ValueError('invalid parent hash')
        ancestors[current] = header
        if header['height'] == 1:
            if current != expected_genesis or header['parent'] != '0' * 64 or header['txnum'] != 0 or header['score'] != 0:
                raise ValueError('invalid or different genesis ancestor')
            return ancestors
        parent = graph.get(header['parent'])
        if not isinstance(parent, dict):
            raise ValueError('missing parent in this node header graph')
        if type(parent.get('height')) is not int or header['height'] != parent['height'] + 1:
            raise ValueError('header height is not parent height plus one')
        if type(parent.get('score')) is not int or parent['score'] + header['txnum'] != header['score']:
            raise ValueError('header score is not parent score plus transaction count')
        current = header['parent']


def ReconstructBoundaries(boundaries, exports, names):
    """Use parent-graph intersections; counts never substitute for ancestry."""
    points = []
    if len(set(names)) != len(names):
        raise ValueError('duplicate inventory node identifier')
    for target in (2, 12):
        point = {'target_seconds': target, 'common': None}
        try:
            matches = [value for value in boundaries if value.get('target_seconds') == target]
            if len(matches) != 1:
                raise ValueError('missing or duplicate exact endpoint')
            rows = matches[0].get('nodes', [])
            if len(rows) != len(names) or {row.get('node') for row in rows} != set(names):
                raise ValueError('endpoint node set is incomplete or duplicated')
            if any(type(row.get(key)) not in (int, float) or not math.isfinite(row[key])
                   for row in rows for key in ('start_seconds', 'end_seconds')):
                raise ValueError('missing or nonfinite RPC acquisition interval')
            point['common_observation_interval_seconds'] = [min(row['start_seconds'] for row in rows), max(row['end_seconds'] for row in rows)]
            if any(row.get('error') or row['end_seconds'] < row['start_seconds'] for row in rows):
                raise ValueError('RPC failed or acquisition interval reversed')
            paths = [TraceHead(row.get('head'), exports[row['node']]) for row in rows]
            shared = set(paths[0]).intersection(*(set(path) for path in paths[1:]))
            if not shared:
                raise ValueError('no common ancestor in all node graphs')
            highest = max(shared, key=lambda key: paths[0][key]['height'])
            header = paths[0][highest]
            if any(path[highest] != header for path in paths):
                raise ValueError('shared header/score metadata differs across nodes')
            point['common'] = {'hash': highest, 'height': header['height'], 'transactions': header['score']}
            point['graph_ancestry_proved_nodes'] = len(paths)
        except (ValueError, KeyError, TypeError) as error:
            point['reason'] = str(error)
        points.append(point)
    return points


def EvaluateBoundaries(points, proof):
    result = dict(passed=False, reasons=[])
    endpoints = [next((p for p in points if p['target_seconds'] == target), None) for target in [2, 12]]
    if not proof.get('passed'):
        result['reasons'].append('persisted canonical proof failed')
    if proof.get('state_audit_passed') is not True:
        result['reasons'].append('final state audit missing or failed')
    if not all(p and p.get('common') for p in endpoints):
        result['reasons'].append('missing exact measurement endpoint')
        result['reasons'].extend(f"endpoint {p['target_seconds']}s: {p['reason']}" for p in endpoints if p and p.get('reason'))
        return result
    a, b = endpoints
    a0, a1 = a['common_observation_interval_seconds']
    b0, b1 = b['common_observation_interval_seconds']
    spans = [a1 - a0, b1 - b0]
    height = b['common']['height'] - a['common']['height']
    count = b['common']['transactions'] - a['common']['transactions']
    if any(not 0 <= value <= .5 for value in spans):
        result['reasons'].append('acquisition span exceeds 0.5s')
    for point, begin, end in ((a, a0, a1), (b, b0, b1)):
        target = point['target_seconds']
        if not target <= begin <= end <= target + .5:
            result['reasons'].append(f'endpoint {target}s acquired outside its target-to-target+0.5s window')
    if height <= 0 or count < 0 or b0 <= a1:
        result['reasons'].append('nonpositive height/time or negative transaction progress')
    result.update(endpoint_spans=spans, endpoint_intervals=[[a0, a1], [b0, b1]],
                  height_delta=height, transaction_delta=count,
                  boundary_targets=[2, 12], node_count=proof.get('node_count'))
    if result['reasons']:
        return result
    seconds = (b0 + b1 - a0 - a1) / 2
    result.update(passed=True, measurement_seconds=seconds, tps=count / seconds,
                  tps_bounds=[count / (b1 - a0), count / (b0 - a1)],
                  mean_common_block_interval_ms=1000 * seconds / height,
                  block_interval_bounds_ms=[1000 * (b0 - a1) / height, 1000 * (b1 - a0) / height])
    return result


def OfflineAudit(C, cluster, run, output, genesis, dataset, maximum):
    """Audit stopped databases without producing blocks or connecting peers."""
    def Export(index):
        node = cluster.nodes[index]
        value = C.ExportHeaders(node, run)
        if value.get('ok') is not True or value.get('node') != node['id'] or value.get('run') != run:
            raise ValueError('Header export ownership/identity mismatch')
        Save(output / (node['id'] + '-headers.json'), value)
        return value

    exports = dict(zip([node['id'] for node in cluster.nodes], cluster.All(Export)))
    boundaries_path = output / 'head-boundaries.json'
    boundaries = json.loads(boundaries_path.read_text(encoding='utf-8')) if boundaries_path.exists() else []
    points = ReconstructBoundaries(boundaries, exports, [node['id'] for node in cluster.nodes])
    with (output / 'timeline.jsonl').open('w', encoding='utf-8') as timeline:
        for point in points:
            timeline.write(json.dumps(point) + '\n')
    routes = {}
    with Path(dataset).open('rb') as source:
        for _ in range(1024):
            tx = source.read(176)
            if len(tx) != 176:
                raise ValueError('Incomplete dataset route cycle')
            amount, nonce = struct.unpack('<QQ', tx[64:80])
            if nonce != 1 or tx[:32].hex() in routes:
                raise ValueError('Expected 1024 distinct senders with nonce 1')
            routes[tx[:32].hex()] = (tx[32:64].hex(), amount)
    keys = list(routes)
    fee = genesis['base_fee']
    initial = {row['public_key']: row['balance'] for row in genesis['accounts']}
    if set(keys) != set(initial) or any(receiver not in initial for receiver, _ in routes.values()):
        raise ValueError('Dataset routes must cover exactly the genesis accounts')
    anchors = [point['common'] for point in points if point['target_seconds'] in [2, 12] and point.get('common')]

    def Start(index):
        node = cluster.nodes[index]
        # LightObserve.LoadCase stops every experiment node before returning.
        Save(output / (node['id'] + '-load-stop.json'), C.Worker(node, {'action': 'collect', 'run': run}))
        return C.Worker(node, {'action': 'start', 'run': run, 'restart': True,
            'binary': node['binary'], 'genesis': genesis,
            'arguments': ['--port', node['port'], '--bind', node.get('bind', node['host']),
                          '--produce', 0, '--sync', 1, '--run-seconds', 300]})

    cluster.All(Start)

    def Check(index):
        node = cluster.nodes[index]
        client = C.NodeClient(node)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    before = client.Query(13)
                    break
                except (OSError, ValueError, ConnectionError):
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(.05)
            accounts = {}
            for begin in range(0, 1024, 128):
                batch = keys[begin:begin + 128]
                values = client.Query(37, b''.join(bytes.fromhex(key) for key in batch))
                if len(values) != len(batch) or any(value is None for value in values):
                    raise AssertionError('Account query incomplete')
                accounts.update(zip(batch, values))
            expected = dict(initial)
            confirmed = 0
            for position, key in enumerate(keys):
                nonce = accounts[key]['nonce']
                receiver, amount = routes[key]
                if not 0 <= nonce <= maximum // 1024 + int(position < maximum % 1024):
                    raise AssertionError('Nonce exceeds attempted dataset prefix')
                expected[key] -= nonce * (amount + fee)
                expected[receiver] += nonce * amount
                confirmed += nonce
            if any(accounts[key]['balance'] != expected[key] for key in keys):
                raise AssertionError('Reconstructed account balance mismatch')
            after, state = client.Query(13), client.Query(35)
            if after['failed'] or before['head'] != after['head'] or confirmed != after['chain_transactions']:
                raise AssertionError('Stopped ledger moved or count mismatched')
            if sum(expected.values()) != after['user_supply'] or confirmed * fee != after['user_burned']:
                raise AssertionError('Supply or burned fees mismatch')
            if state['head'] != after['head'] or state['transactions'] != confirmed or state['supply'] != sum(expected.values()) or state['burned'] != confirmed * fee:
                raise AssertionError('State digest metadata mismatch')
            exported_head = exports[node['id']]['current']
            if exported_head != {'hash': after['head'], 'height': after['height'], 'transactions': confirmed}:
                raise AssertionError('Read-only exported head differs from reopened audit ledger')
            checks = [dict(anchor=anchor, matches=anchor['height'] <= after['height'] and
                client.Query(29, struct.pack('<Q', anchor['height'])) == anchor) for anchor in anchors]
            ledger = [client.Query(29, struct.pack('<Q', height)) for height in range(1, after['height'] + 1)]
            if client.Query(13)['head'] != after['head']:
                raise AssertionError('Canonical index changed while auditing')
            Save(output / (node['id'] + '-canonical-index.json'), ledger)
            return dict(node=node['id'], accounts_checked=1024, confirmed=confirmed,
                        height=after['height'], head=after['head'], state=state, passed=True, anchor_checks=checks)
        finally:
            client.Close()

    nodes = cluster.All(Check)
    heads_equal = len({node['head'] for node in nodes}) == 1
    states_equal = len({json.dumps(node['state'], sort_keys=True) for node in nodes}) == 1
    audit = dict(passed=all(node['passed'] for node in nodes) and heads_equal and states_equal,
                 nodes=nodes, node_count=len(cluster.nodes), all_heads_equal=heads_equal,
                 all_states_equal=states_equal,
                 method='RPC19 boundary heads traced through stopped read-only header graphs; highest shared ancestor proved on every final canonical chain; stopped nodes reopened without production or peers for independent account/state audit',
                 measurement_or_drain_extended=False)
    proof = dict(passed=len(anchors) == 2 and all(len(node['anchor_checks']) == 2 and
                 all(check['matches'] for check in node['anchor_checks']) for node in nodes),
                 node_count=len(cluster.nodes), state_audit_passed=audit['passed'], nodes=nodes)
    Save(output / 'persisted-state-audit.json', audit)
    Save(output / 'persisted-boundary-proof.json', proof)
    measurement = EvaluateBoundaries(points, proof)
    measurement['method'] = 'highest common ancestor of each RPC19 head set, proved offline and on every final canonical chain'
    return audit, measurement


def RunGroup(args, nodes, plan, dataset):
    """Run exactly two fixed short cases; return the recorded result rows."""
    sys.path.insert(0, str(Path(args.source) / 'Test'))
    import Experiments as E
    import Cluster as C
    nodes = [dict(node) for node in nodes]
    plan = dict(plan)
    count = len(nodes)
    if count not in (10, 20):
        raise ValueError('Retest requires 10 or 20 nodes')
    required = dict(nodes=count, repeats=1, warmup=2, duration=10, drain_seconds=5,
                    wallets=1024, batch=128, ingress='broadcast')
    for key, value in required.items():
        if plan.get(key) != value:
            raise ValueError(f'Fixed comparison requires {key}={value!r}')
    if plan.get('throughput_rates') != RATES or plan.get('throughput_modes') != ['throughput'] or plan.get('parallel_ingress', False):
        raise ValueError('Fixed 20k/30k rates and original sequential broadcast are required')
    if any(int(node['port']) != 8089 or not node.get('root') for node in nodes):
        raise ValueError('Use dedicated 8089 test nodes with explicit experiment roots')
    E.ValidatePlan(plan)
    output_root = Path(args.work)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,65}', output_root.name):
        raise ValueError('Group directory name must be a unique valid run prefix of at most 65 characters')
    dataset = Path(dataset)
    dataset_size = dataset.stat().st_size
    if dataset_size % 176 or dataset_size < max(RATES) * 12 * 176:
        raise ValueError('Shared dataset must contain at least 360000 complete native transfers')
    digest = Digest(dataset)
    genesis_path = Path(str(dataset) + '.genesis.json')
    genesis = json.loads(genesis_path.read_text(encoding='utf-8'))
    if len(genesis['accounts']) != 1024:
        raise ValueError('Expected 1024 genesis accounts')
    output_root.mkdir(parents=True, exist_ok=False)
    Save(output_root / 'inventory.json', {'nodes': nodes})
    _, nodes = C.ReadInventory(output_root / 'inventory.json')
    Save(output_root / 'plan.json', plan)
    Save(output_root / 'dataset.json', dict(path=str(dataset), bytes=dataset_size,
         count=dataset_size // 176, sha256=digest, genesis_sha256=Digest(genesis_path)))
    Save(output_root / 'protocol.json', dict(revision=getattr(args, 'source_revision', REVISION),
         plan=plan, inventory=nodes, dataset_sha256=digest,
         driver_sha256=Digest(__file__), observer_sha256=Digest(LightObserve.__file__),
         known_hosts_sha256=Digest(args.known_hosts),
         expected_binary_sha256=args.expected_binary_sha256,
         block_time_definition='observation midpoint elapsed divided by net canonical common-prefix height growth',
         measurement_eligibility='independent RPC19 at exact 2s/12s; highest common ancestor from per-node read-only header graphs; all-node acquisition span <=0.5s; both anchors on every final persisted canonical chain and independent account/state audit'))
    LightObserve.Install(args)
    rows = []
    for number, rate in enumerate(RATES, 1):
        name = f'{number:04d}-throughput-r1'
        output = output_root / name
        output.mkdir()
        run = f'{output_root.name}-{rate}-r1'
        case_nodes = [{**node, 'binary': str(Path(args.bin) / 'boost'),
                       'worker': str(Path(args.source) / 'Test' / 'ClusterWorker.py')} for node in nodes]
        settings = {'io-threads': 4, 'verify-threads': 8, 'sync': 1, 'produce': 1,
                    'experiment-index': 1, 'clock-sync': 1, 'sync-period-ms': 100,
                    'sync-gain': .25, 'round-ms': 50, 'candidate-ms': 12,
                    'relay-transactions': 0, 'block-ms': 50, 'max-block-txs': 6000, 'run-seconds': 300}
        case = dict(suite='throughput', mode='throughput', rate=rate, nodes=count, repeat=1,
                    pattern='steady', group=f'retest-{count}', revision=getattr(args, 'source_revision', REVISION))
        row = dict(case=case, status='failed', run=run, dataset_sha256=digest)
        cluster = C.Cluster(case_nodes, run, genesis, settings, output)
        abort_group = False
        print(json.dumps({'starting': run, 'nodes': count, 'rate': rate}), flush=True)
        try:
            cluster.Start()
            deadline = time.monotonic() + 30
            while True:
                stats = cluster.Stats()
                if all(not stat['failed'] and stat['peers'] >= count - 1 for stat in stats):
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('Full graph not ready')
                time.sleep(.2)
            Save(output / 'readiness.json', stats)
            Save(output / 'load-starts.json', cluster.starts)
            row['metrics'] = E.LoadCase(cluster, case, plan, dataset, genesis, output)
            maximum = row['metrics']['sent_unique_attempted_upper_bound']
            row['state_audit'], row['audited_measurement'] = OfflineAudit(C, cluster, run, output, genesis, dataset, maximum)
            accepted = {row['metrics']['sent_unique_completed_all_ingress'], maximum}
            row['metrics']['drained_after_offline_audit'] = bool(row['state_audit']['passed'] and
                all(node['confirmed'] in accepted for node in row['state_audit']['nodes']))
            observation = row['metrics'].get('head_drain_observation') or {}
            heads = [value.get('head') for value in observation.get('nodes', [])]
            row['metrics']['head_drain_observed_equal_accepted_count'] = bool(len(heads) == count and all(heads) and
                len({head['hash'] for head in heads}) == 1 and all(head['transactions'] in accepted for head in heads))
            row['load_success'] = row['metrics'].get('load_success') is True
            row['audited_measurement']['load_success'] = row['load_success']
            if not row['load_success']:
                row['audited_measurement']['passed'] = False
                row['audited_measurement']['reasons'].append('load generator failed or ended before the fixed cutoff')
                if 'tps' in row['audited_measurement']:
                    row['audited_measurement']['diagnostic_tps'] = row['audited_measurement'].pop('tps')
            row['status'] = 'complete'
        except Exception as error:
            row['error'] = repr(error)
            abort_group = isinstance(error, LightObserve.RetestObserve.ProcessStopError)
            row['shutdown_unconfirmed'] = abort_group
        finally:
            cluster.Close()
        row['cleanup_errors'] = cluster.cleanup_errors
        row['success'] = row['status'] == 'complete' and row.get('load_success') is True and \
            row.get('state_audit', {}).get('passed') is True and \
            row.get('audited_measurement', {}).get('passed') is True and not cluster.cleanup_errors
        Save(output / 'result.json', row)
        rows.append(row)
        Save(output_root / 'results.json', rows)
        Save(output_root / 'summary.json', [dict(case=value['case'], status=value['status'],
             error=value.get('error'), measurement=value.get('audited_measurement'),
             state_audit_passed=value.get('state_audit', {}).get('passed'),
             load_success=value.get('load_success'), success=value.get('success'),
             drained=value.get('metrics', {}).get('drained'),
             drained_after_offline_audit=value.get('metrics', {}).get('drained_after_offline_audit'),
             head_drain_observed_equal_accepted_count=value.get('metrics', {}).get('head_drain_observed_equal_accepted_count'),
             achieved_send_tps=value.get('metrics', {}).get('achieved_send_tps'),
             cleanup_errors=value.get('cleanup_errors')) for value in rows])
        print(json.dumps(dict(finished=run, status=row['status'], measurement=row.get('audited_measurement'), error=row.get('error'))), flush=True)
        if cluster.cleanup_errors:
            raise RuntimeError('Case cleanup incomplete; refusing to start another case')
        if abort_group:
            raise RuntimeError('Experiment shutdown was not confirmed; refusing to start another case')
    return rows
