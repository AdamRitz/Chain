"""Deploy a tested package or run a five-minute test using the existing Ansible playbooks."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time

sys.dont_write_bytecode = True
from cluster_plan import Identifier, Inventory, MakePlan
from stage_release import Digest, ManifestDigest


def Save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def RequiredTransactions(rate, warmup, duration):
    if (type(rate) is not int or not 1 <= rate <= 10000000 or
            any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in (warmup, duration)) or not 0 <= warmup <= 600 or not .1 <= duration <= 3600):
        raise ValueError('Invalid rate, warmup or measurement duration')
    count = math.ceil(rate * (warmup + duration))
    if not 1 <= count <= 100000000:
        raise ValueError('The sender supports at most 100,000,000 prepared transactions')
    return count


def PrepareDataset(sender, cache, count, wallets, invoke=subprocess.run):
    """Sign once on the controller; validate and reuse the same immutable large file."""
    sender, cache = Path(sender).resolve(strict=True), Path(cache).resolve()
    if type(wallets) is not int or not 1 <= wallets <= 100000:
        raise ValueError('Use 1..100000 wallets')
    settings = {'schema': 1, 'sender_sha256': Digest(sender), 'count': count, 'wallets': wallets}
    key = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    directory = cache / key
    def Read(directory):
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        if any(manifest.get(name) != value for name, value in settings.items()):
            raise ValueError('Cached dataset parameters differ')
        data, genesis = directory / 'transactions.bin', directory / 'transactions.bin.genesis.json'
        if (data.stat().st_size != count * 176 or Digest(data) != manifest.get('dataset_sha256') or
                Digest(genesis) != manifest.get('genesis_sha256')):
            raise ValueError('Cached dataset checksum or transaction count differs')
        if len(json.loads(genesis.read_text(encoding='utf-8')).get('accounts', [])) != wallets:
            raise ValueError('Cached genesis wallet count differs')
        return {'dataset': str(data), 'genesis': str(genesis), **manifest}
    if directory.exists():
        return Read(directory)
    temporary = Path(tempfile.mkdtemp(prefix='.prepare-', dir=cache))
    try:
        data = temporary / 'transactions.bin'
        environment = dict(os.environ)
        library = sender.parent.parent / 'lib'
        if library.is_dir():
            environment['LD_LIBRARY_PATH'] = str(library) + (':' + environment['LD_LIBRARY_PATH'] if environment.get('LD_LIBRARY_PATH') else '')
        invoke([str(sender), '--prepare', str(data), '--count', str(count), '--wallets', str(wallets)],
               check=True, env=environment, timeout=max(300, count / 1000))
        genesis = Path(str(data) + '.genesis.json')
        Save(temporary / 'manifest.json', {**settings, 'dataset_sha256': Digest(data),
                                          'genesis_sha256': Digest(genesis)})
        Read(temporary)
        try:
            temporary.rename(directory)
        except OSError:
            # Another controller may have finished the same cache key first.
            if not directory.is_dir():
                raise
        return Read(directory)
    finally:
        if temporary.exists():
            if temporary.resolve().parent != cache or not temporary.name.startswith('.prepare-'):
                raise ValueError('Unrecognized temporary dataset directory')
            shutil.rmtree(temporary)


def PreparedDataset(dataset, genesis, required, wallets, manifest_path=None):
    """Import signed native transfers independently of the generator's OS or binary."""
    if type(wallets) is not int or not 1 <= wallets <= 100000 or type(required) is not int or not 1 <= required <= 100000000:
        raise ValueError('Invalid required transaction or wallet count')
    dataset, genesis = Path(dataset).resolve(strict=True), Path(genesis).resolve(strict=True)
    size = dataset.stat().st_size
    if not size or size % 176 or size // 176 < required:
        raise ValueError('Prepared dataset must contain at least the required complete transactions')
    value = json.loads(genesis.read_text(encoding='utf-8'))
    accounts, fee = value.get('accounts', []), value.get('base_fee')
    if (len(accounts) != wallets or type(fee) is not int or fee < 0 or 'vdf' in value or
            any(not isinstance(row, dict) or not re.fullmatch(r'[0-9a-f]{64}', row.get('public_key', '')) or
                type(row.get('balance')) is not int or not 0 <= row['balance'] <= 2**64-1 for row in accounts) or
            len({row['public_key'] for row in accounts}) != wallets):
        raise ValueError('Prepared account genesis must contain the declared unique funded wallets')
    metadata = {'schema': 2, 'format': 'native-transfer-v1', 'count': size // 176, 'bytes': size,
                'wallets': wallets, 'dataset_sha256': Digest(dataset), 'genesis_sha256': Digest(genesis),
                'verified_signatures': size // 176}
    if manifest_path:
        manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        if any(manifest.get(key) != expected for key, expected in metadata.items()):
            raise ValueError('Prepared dataset manifest does not match transaction/account files')
    else:
        from nacl.signing import VerifyKey
        balances = {bytes.fromhex(row['public_key']): row['balance'] for row in accounts}
        verifiers = {public: VerifyKey(public) for public in balances}
        with dataset.open('rb') as stream:
            first = [stream.read(176) for _ in range(min(wallets, size // 176))]
            order = [tx[:32] for tx in first]
            if len(order) != len(set(order)) or any(public not in balances for public in order):
                raise ValueError('Prepared transaction sender cycle differs from account genesis')
            stream.seek(0)
            for index in range(size // 176):
                tx = stream.read(176)
                sender, receiver = tx[:32], tx[32:64]
                amount, nonce = struct.unpack_from('<QQ', tx, 64)
                pattern = first[index % wallets]
                digest = hashlib.blake2b(tx[:80], digest_size=32).digest()
                if (tx[:72] != pattern[:72] or nonce != index // wallets + 1 or
                        sender not in balances or receiver not in balances or amount == 0 or
                        digest != tx[80:112] or balances[sender] < amount + fee):
                    raise ValueError('Prepared transfer hash, wallet cycle, nonce or balance is invalid')
                verifiers[sender].verify(digest, tx[112:176])
                balances[sender] -= amount + fee
                balances[receiver] += amount
                if balances[receiver] > 2**64-1:
                    raise ValueError('Prepared transfer overflows the receiver balance')
    return {'dataset': str(dataset), 'genesis': str(genesis), 'required_count': required, **metadata}


def RotationGenesis(accounts_genesis, now_ms=None, *, consecutive_blocks=32, producer_timeout_slots=8,
                    epoch_blocks=None):
    """Create a new test chain, without changing the cached funded genesis."""
    value = json.loads(json.dumps(accounts_genesis))
    if 'vdf' in value:
        raise ValueError('Do not change an existing VDF genesis; prepare a new test dataset')
    if type(consecutive_blocks) is not int or not 1 <= consecutive_blocks <= 819:
        raise ValueError('consecutive_blocks must be in 1..819')
    if type(producer_timeout_slots) is not int or not 3 <= producer_timeout_slots <= 65536:
        raise ValueError('producer_timeout_slots must be in 3..65536 (greater than future_slots=2)')
    epoch_blocks = 5 * consecutive_blocks if epoch_blocks is None else epoch_blocks
    if type(epoch_blocks) is not int or epoch_blocks != 5 * consecutive_blocks:
        raise ValueError('epoch_blocks must equal 5 * consecutive_blocks')
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    value['vdf'] = {'enabled': True, 'producer_rotation': True, 'epoch_blocks': epoch_blocks,
                    'consecutive_blocks': consecutive_blocks, 'producer_timeout_slots': producer_timeout_slots,
                    'iterations': 65536, 'slot_ms': 200, 'start_time_ms': now_ms // 200 * 200,
                    'fallback_slots': 20, 'future_slots': 2}
    return value


def WorkIdentities(plan, output):
    from nacl.signing import SigningKey
    directory = output / 'identities'
    directory.mkdir(mode=0o700)
    identities, files = [], {}
    for node in plan['nodes']:
        key = SigningKey.generate()
        path = directory / (node['id'] + '.seed')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
            stream.write(bytes(key).hex() + '\n')
        identities.append({'id': node['id'], 'public_key': bytes(key.verify_key).hex()})
        files[node['id']] = str(path)
    Save(output / 'identities.json', identities)
    return identities, files


def VerifySender(package, sender):
    with tarfile.open(package, 'r:gz') as archive:
        member = archive.getmember('manifest.json')
        if not member.isfile() or not 0 < member.size <= 16 * 1024 * 1024:
            raise ValueError('Invalid package manifest')
        manifest = json.load(archive.extractfile(member))
    hashes = [entry['sha256'] for entry in manifest['files'] if entry['path'] == 'bin/sender']
    if len(hashes) != 1 or Digest(sender) != hashes[0]:
        raise ValueError('The controller sender must match bin/sender in the tested release')


def Parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'stage', 'deploy', 'test'))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--plan', type=Path, help='Existing cluster_plan.py plan.json')
    source.add_argument('--nodes', type=Path, help='Credential-free JSON array of real nodes')
    parser.add_argument('--cluster', default='rotation')
    parser.add_argument('--degree', type=int, default=8)
    parser.add_argument('--injectors', type=int, default=4)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--port', type=int, help='New plan only: deploy 18089, test 28089')
    parser.add_argument('--release-id')
    parser.add_argument('--package', type=Path)
    parser.add_argument('--package-sha256')
    parser.add_argument('--output', type=Path, required=True, help='Fresh test result directory or deployment request directory')
    parser.add_argument('--inventory', type=Path, action='append', default=[], help='Additional SSH/Vault inventory')
    parser.add_argument('--extra-vars-file', type=Path, help='Connection/runtime variables; secrets stay in this external file')
    parser.add_argument('--ansible-playbook', default=shutil.which('ansible-playbook') or 'ansible-playbook')
    parser.add_argument('--python', default='/usr/bin/python3')
    parser.add_argument('--forks', type=int, default=32)
    parser.add_argument('--query-workers', type=int, default=32)
    parser.add_argument('--genesis', type=Path, help='Required for deploy: use this existing genesis unchanged')
    parser.add_argument('--sender', type=Path, help='Required for test: sender from the tested release')
    parser.add_argument('--dataset', type=Path, help='Existing signed data, including data prepared on Windows')
    parser.add_argument('--dataset-genesis', type=Path, help='Funded account genesis; default DATASET.genesis.json')
    parser.add_argument('--dataset-manifest', type=Path, help='Portable dataset.json from a previous offline prepare')
    parser.add_argument('--cache', type=Path, help='Shared controller dataset cache, default OUTPUT parent/datasets')
    parser.add_argument('--rate', type=int, default=10000)
    parser.add_argument('--rates', type=int, nargs='+', help='Measure these rates on one chain, using consecutive signed transactions')
    parser.add_argument('--work-election', action='store_true', help='Create the historical Work election test chain')
    parser.add_argument('--work-snapshot', type=Path, help='Reuse a prepared empty ledger, original genesis/identities and Work proofs')
    parser.add_argument('--work-iterations', type=int, default=65536)
    parser.add_argument('--work-groups', type=int, default=4)
    parser.add_argument('--work-group-size', type=int, default=8)
    parser.add_argument('--consensus-timeout', type=float, help='Wait for Work history once; default 1800 seconds for Work')
    parser.add_argument('--duration', type=float, default=300, help='Formal measurement seconds, excludes warmup and drain')
    parser.add_argument('--warmup', type=float, default=2)
    parser.add_argument('--drain-seconds', type=float, default=5)
    parser.add_argument('--readiness-timeout', type=float, default=300)
    parser.add_argument('--bootstrap-producers', type=int, default=1, help='0 starts all producers immediately')
    parser.add_argument('--consecutive-blocks', type=int, default=32, help='New test chain: blocks per producer')
    parser.add_argument('--producer-timeout-slots', type=int, default=8, help='New test chain: empty slots before switching producer')
    parser.add_argument('--epoch-blocks', type=int, help='New test chain: must equal 5 * consecutive-blocks (default 160)')
    parser.add_argument('--wallets', type=int, default=1024)
    parser.add_argument('--batch', type=int, default=128)
    parser.add_argument('--node-settings', type=Path, help='JSON object of additional benchmark node options')
    parser.add_argument('--staged', action='store_true', help='Test the already distributed verified release')
    parser.add_argument('--no-early-stop', dest='early_stop_enabled', action='store_false', default=True)
    parser.add_argument('--early-stop-ratio', type=float, default=.7)
    parser.add_argument('--early-stop-start-seconds', type=float, default=30)
    parser.add_argument('--early-stop-window-seconds', type=float, default=15)
    parser.add_argument('--early-stop-windows', type=int, default=3)
    parser.add_argument('--max-backlog-growth', type=float, default=10)
    return parser


def Execute(args, invoke=subprocess.run):
    if args.work_snapshot:
        args.work_election = True
    if not 1 <= args.forks <= 128 or not 1 <= args.query_workers <= 128:
        raise ValueError('Ansible forks and query workers must be in 1..128')
    output, package = args.output.resolve(), None
    if args.action != 'prepare':
        if not args.release_id or not args.package or not args.package_sha256:
            raise ValueError('stage, deploy and test require release-id, package and package-sha256')
        Identifier(args.release_id)
        package = args.package.resolve(strict=True)
        if Digest(package) != args.package_sha256:
            raise ValueError('Package SHA256 mismatch')
    if args.staged and args.action != 'test':
        raise ValueError('--staged is only used with test')
    if (args.work_election or args.rates) and args.action not in ('prepare', 'test'):
        raise ValueError('Work election and rate series require prepare or test')
    if args.consensus_timeout is not None and (not math.isfinite(args.consensus_timeout) or not 1 <= args.consensus_timeout <= 2400):
        raise ValueError('consensus-timeout must be in 1..2400 seconds')
    for value, lower, upper in ((args.early_stop_ratio, .01, .99),
            (args.early_stop_start_seconds, 0, 600), (args.early_stop_window_seconds, .1, 600),
            (args.max_backlog_growth, 0, 10000000)):
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError('Invalid early-stop or backlog-growth setting')
    if not 1 <= args.early_stop_windows <= 20:
        raise ValueError('Use 1..20 consecutive early-stop windows')
    if args.plan:
        plan = json.loads(args.plan.read_text(encoding='utf-8'))
        keys = {'id', 'host', 'ansible_host', 'ansible_user', 'ansible_port', 'port', 'bind', 'query_host'}
        rebuilt = MakePlan([{key: value for key, value in node.items() if key in keys} for node in plan['nodes']],
                           plan['cluster'], plan['requested_degree'], plan['seed'], len(plan['injectors']))
        if rebuilt != plan:
            raise ValueError('Existing plan differs from its deterministic graph')
        if args.port is not None:
            raise ValueError('--port cannot change an existing plan')
    else:
        nodes = json.loads(args.nodes.read_text(encoding='utf-8'))
        port = args.port or (28089 if args.action in ('prepare', 'test') else 18089)
        nodes = [{**node, 'port': port if args.port is not None else node.get('port', port)} for node in nodes]
        plan = MakePlan(nodes, args.cluster, args.degree, args.seed, min(args.injectors, len(nodes)))
    if args.action in ('prepare', 'test') and output.exists():
        raise FileExistsError('Use a new test output directory and run ID')
    if args.action == 'deploy' and not args.genesis:
        raise ValueError('deploy requires --genesis; existing chain parameters are never rewritten')
    if args.action == 'test' and not args.sender:
        raise ValueError('test requires --sender from the same verified release')
    if args.action in ('prepare', 'test') and not args.dataset and not args.sender:
        raise ValueError('prepare requires --dataset or --sender')
    if not args.dataset and (args.dataset_genesis or args.dataset_manifest):
        raise ValueError('dataset-genesis and dataset-manifest require --dataset')
    output.mkdir(parents=True, exist_ok=True)
    Save(output / 'plan.json', plan)
    Save(output / 'inventory.json', Inventory(plan))
    variables = {} if args.action == 'prepare' else {'chain_release_id': args.release_id, 'chain_package': str(package),
                 'chain_package_sha256': args.package_sha256, 'chain_python': args.python,
                 'chain_serial': args.forks, 'chain_cluster_bootstrap_producers': args.bootstrap_producers}
    if package:
        variables['chain_package_manifest_sha256'] = ManifestDigest(package)
    playbook = {'prepare': None, 'stage': 'stage.yml', 'deploy': 'cluster_deploy.yml', 'test': 'cluster_benchmark.yml'}[args.action]
    if args.extra_vars_file:
        variables['chain_extra_vars_file'] = str(args.extra_vars_file.resolve(strict=True))
    if args.action == 'deploy':
        variables['chain_cluster_genesis'] = json.loads(args.genesis.read_text(encoding='utf-8'))
        variables['chain_cluster_arguments'] = ['--produce', '1', '--sync', '1', '--block-ms', '200',
                                               '--max-block-txs', '6000']
    if args.action in ('prepare', 'test'):
        if args.action == 'test':
            VerifySender(package, args.sender.resolve(strict=True))
        rates = args.rates or [args.rate]
        if len(rates) > 20 or len(set(rates)) != len(rates):
            raise ValueError('Use at most 20 distinct rates')
        count = sum(RequiredTransactions(rate, args.warmup, args.duration) for rate in rates)
        if count > 100000000:
            raise ValueError('The rate series requires more than 100,000,000 transactions')
        dataset = PreparedDataset(args.dataset, args.dataset_genesis or Path(str(args.dataset) + '.genesis.json'),
            count, args.wallets, args.dataset_manifest) if args.dataset else PrepareDataset(
                args.sender, args.cache or output.parent / 'datasets', count, args.wallets)
        if args.action == 'prepare' and not args.dataset:
            dataset = PreparedDataset(dataset['dataset'], dataset['genesis'], count, args.wallets)
        genesis_path = output / 'genesis.json'
        accounts = json.loads(Path(dataset['genesis']).read_text(encoding='utf-8'))
        identity_files, snapshot = {}, {}
        if args.work_snapshot:
            from work_snapshot import Prepare
            genesis, identity_files, snapshot = Prepare(args.work_snapshot, plan, accounts, output)
        elif args.work_election:
            from work_genesis import MakeGenesis
            identities, identity_files = WorkIdentities(plan, output)
            genesis = MakeGenesis(accounts, identities, int(time.time()*1000)//200*200,
                work_iterations=args.work_iterations, work_groups=args.work_groups, work_group_size=args.work_group_size)
        else:
            genesis = RotationGenesis(accounts, consecutive_blocks=args.consecutive_blocks,
                producer_timeout_slots=args.producer_timeout_slots, epoch_blocks=args.epoch_blocks)
        Save(genesis_path, genesis)
        settings = json.loads(args.node_settings.read_text(encoding='utf-8')) if args.node_settings else {}
        from cluster_benchmark import ValidateSettings
        ValidateSettings(settings)
        variables.update(chain_run_id=Identifier(output.name), chain_result_dir=str(output),
                         chain_cluster_plan=str(output / 'plan.json'), chain_dataset=dataset['dataset'],
                         chain_dataset_sha256=dataset['dataset_sha256'], chain_genesis_file=str(genesis_path),
                         chain_genesis_sha256=Digest(genesis_path), chain_cluster_rate=rates[0],
                         chain_cluster_rates=rates if args.rates else [],
                         chain_cluster_identity_files=identity_files,
                         chain_cluster_snapshot=snapshot,
                         chain_cluster_consensus_timeout=args.consensus_timeout or (1800 if args.work_election else args.readiness_timeout),
                         chain_cluster_wallets=args.wallets, chain_cluster_duration=args.duration,
                         chain_cluster_warmup=args.warmup, chain_cluster_drain_seconds=args.drain_seconds,
                         chain_cluster_readiness_timeout=args.readiness_timeout,
                         chain_cluster_bootstrap_producers=args.bootstrap_producers,
                         chain_cluster_query_workers=args.query_workers, chain_cluster_forks=args.forks,
                         chain_cluster_batch=args.batch, chain_cluster_node_settings=settings,
                         chain_cluster_staged=args.staged,
                         chain_cluster_early_stop_enabled=args.early_stop_enabled,
                         chain_cluster_early_stop_ratio=args.early_stop_ratio,
                         chain_cluster_early_stop_start_seconds=args.early_stop_start_seconds,
                         chain_cluster_early_stop_window_seconds=args.early_stop_window_seconds,
                         chain_cluster_early_stop_windows=args.early_stop_windows,
                         chain_cluster_max_backlog_growth=args.max_backlog_growth)
        Save(output / 'dataset.json', dataset)
        samples = genesis['vdf']['work_groups']*genesis['vdf']['work_group_size'] if args.work_election else 0
        startup = (32 if (snapshot['height']-1) % 160 == 0 else 64) if snapshot else ((samples+2)*32 if args.work_election else 64)
        Save(output / 'test-plan.json', {
            'nodes': len(plan['nodes']), 'rates': rates, 'measurement_seconds_per_rate': args.duration,
            'warmup_seconds_per_rate': args.warmup, 'drain_seconds_per_rate': args.drain_seconds,
            'required_transactions': count, 'signed_dataset_bytes': count*176,
            'work_sample_count': samples,
            'minimum_work_startup_seconds': startup, 'restored_height': snapshot.get('height'),
            'minimum_free_bytes_per_node': 1024**3+count*1024 if args.work_election else None,
            'estimated_database_bytes_per_node': count*1024,
            'estimated_test_seconds': startup+len(rates)*(args.warmup+args.duration+args.drain_seconds),
            'work_iterations': genesis['vdf']['work_iterations'] if args.work_election else None,
            'stop_after_failed_rate': bool(args.rates), 'restart_between_rates': False if args.rates else None})
    Save(output / 'variables.json', variables)
    if args.action == 'prepare':
        Save(output / 'operation.json', {'action': 'prepare', 'offline': True, 'required_transactions': count,
                                         'duration_seconds': args.duration, 'returncode': 0})
        return 0
    ansible = Path(__file__).resolve().parents[1]
    command = [args.ansible_playbook, '-i', str(output / 'inventory.json')]
    for inventory in args.inventory:
        command += ['-i', str(inventory.resolve(strict=True))]
    command += [str(ansible / playbook), '--forks', str(args.forks)]
    if args.extra_vars_file:
        command += ['-e', '@' + str(args.extra_vars_file.resolve(strict=True))]
    command += ['-e', '@' + str(output / 'variables.json')]
    # Stream Ansible output; never capture a possibly credential-bearing transcript.
    completed = invoke(command, cwd=ansible, check=False)
    Save(output / 'operation.json', {'action': args.action, 'returncode': completed.returncode,
                                    'duration_seconds': args.duration if args.action == 'test' else None})
    return completed.returncode


if __name__ == '__main__':
    raise SystemExit(Execute(Parser().parse_args()))
