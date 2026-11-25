"""Deploy a tested package or run a five-minute test using the existing Ansible playbooks."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

sys.dont_write_bytecode = True
from cluster_plan import Identifier, Inventory, MakePlan
from stage_release import Digest


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


def RotationGenesis(accounts_genesis, now_ms=None):
    """Create a new test chain, without changing the cached funded genesis."""
    value = json.loads(json.dumps(accounts_genesis))
    if 'vdf' in value:
        raise ValueError('Do not change an existing VDF genesis; prepare a new test dataset')
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    value['vdf'] = {'enabled': True, 'producer_rotation': True, 'epoch_blocks': 32,
                    'iterations': 65536, 'slot_ms': 200, 'start_time_ms': now_ms // 200 * 200,
                    'fallback_slots': 20, 'future_slots': 2}
    return value


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
    parser.add_argument('action', choices=('stage', 'deploy', 'test'))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--plan', type=Path, help='Existing cluster_plan.py plan.json')
    source.add_argument('--nodes', type=Path, help='Credential-free JSON array of real nodes')
    parser.add_argument('--cluster', default='rotation')
    parser.add_argument('--degree', type=int, default=8)
    parser.add_argument('--injectors', type=int, default=4)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--port', type=int, help='New plan only: deploy 18089, test 28089')
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--package-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True, help='Fresh test result directory or deployment request directory')
    parser.add_argument('--inventory', type=Path, action='append', default=[], help='Additional SSH/Vault inventory')
    parser.add_argument('--extra-vars-file', type=Path, help='Connection/runtime variables; secrets stay in this external file')
    parser.add_argument('--ansible-playbook', default=shutil.which('ansible-playbook') or 'ansible-playbook')
    parser.add_argument('--python', default='/usr/bin/python3')
    parser.add_argument('--forks', type=int, default=32)
    parser.add_argument('--query-workers', type=int, default=32)
    parser.add_argument('--genesis', type=Path, help='Required for deploy: use this existing genesis unchanged')
    parser.add_argument('--sender', type=Path, help='Required for test: sender from the tested release')
    parser.add_argument('--cache', type=Path, help='Shared controller dataset cache, default OUTPUT parent/datasets')
    parser.add_argument('--rate', type=int, default=10000)
    parser.add_argument('--duration', type=float, default=300, help='Formal measurement seconds, excludes warmup and drain')
    parser.add_argument('--warmup', type=float, default=2)
    parser.add_argument('--drain-seconds', type=float, default=5)
    parser.add_argument('--readiness-timeout', type=float, default=300)
    parser.add_argument('--bootstrap-producers', type=int, default=1, help='0 starts all producers immediately')
    parser.add_argument('--wallets', type=int, default=1024)
    parser.add_argument('--batch', type=int, default=128)
    parser.add_argument('--node-settings', type=Path, help='JSON object of additional benchmark node options')
    return parser


def Execute(args, invoke=subprocess.run):
    if not 1 <= args.forks <= 128 or not 1 <= args.query_workers <= 128:
        raise ValueError('Ansible forks and query workers must be in 1..128')
    Identifier(args.release_id)
    output, package = args.output.resolve(), args.package.resolve(strict=True)
    if Digest(package) != args.package_sha256:
        raise ValueError('Package SHA256 mismatch')
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
        port = args.port or (28089 if args.action == 'test' else 18089)
        nodes = [{**node, 'port': port if args.port is not None else node.get('port', port)} for node in nodes]
        plan = MakePlan(nodes, args.cluster, args.degree, args.seed, min(args.injectors, len(nodes)))
    if args.action == 'test' and output.exists():
        raise FileExistsError('Use a new test output directory and run ID')
    if args.action == 'deploy' and not args.genesis:
        raise ValueError('deploy requires --genesis; existing chain parameters are never rewritten')
    if args.action == 'test' and not args.sender:
        raise ValueError('test requires --sender from the same verified release')
    output.mkdir(parents=True, exist_ok=True)
    Save(output / 'plan.json', plan)
    Save(output / 'inventory.json', Inventory(plan))
    variables = {'chain_release_id': args.release_id, 'chain_package': str(package),
                 'chain_package_sha256': args.package_sha256, 'chain_python': args.python,
                 'chain_serial': args.forks, 'chain_cluster_bootstrap_producers': args.bootstrap_producers}
    playbook = {'stage': 'stage.yml', 'deploy': 'cluster_deploy.yml', 'test': 'cluster_benchmark.yml'}[args.action]
    if args.extra_vars_file:
        variables['chain_extra_vars_file'] = str(args.extra_vars_file.resolve(strict=True))
    if args.action == 'deploy':
        variables['chain_cluster_genesis'] = json.loads(args.genesis.read_text(encoding='utf-8'))
        variables['chain_cluster_arguments'] = ['--produce', '1', '--sync', '1', '--block-ms', '200',
                                               '--max-block-txs', '6000', '--block-tx-step', '2000']
    if args.action == 'test':
        VerifySender(package, args.sender.resolve(strict=True))
        count = RequiredTransactions(args.rate, args.warmup, args.duration)
        dataset = PrepareDataset(args.sender, args.cache or output.parent / 'datasets', count, args.wallets)
        genesis_path = output / 'genesis.json'
        Save(genesis_path, RotationGenesis(json.loads(Path(dataset['genesis']).read_text(encoding='utf-8'))))
        settings = json.loads(args.node_settings.read_text(encoding='utf-8')) if args.node_settings else {}
        variables.update(chain_run_id=Identifier(output.name), chain_result_dir=str(output),
                         chain_cluster_plan=str(output / 'plan.json'), chain_dataset=dataset['dataset'],
                         chain_dataset_sha256=dataset['dataset_sha256'], chain_genesis_file=str(genesis_path),
                         chain_genesis_sha256=Digest(genesis_path), chain_cluster_rate=args.rate,
                         chain_cluster_wallets=args.wallets, chain_cluster_duration=args.duration,
                         chain_cluster_warmup=args.warmup, chain_cluster_drain_seconds=args.drain_seconds,
                         chain_cluster_readiness_timeout=args.readiness_timeout,
                         chain_cluster_bootstrap_producers=args.bootstrap_producers,
                         chain_cluster_query_workers=args.query_workers, chain_cluster_forks=args.forks,
                         chain_cluster_batch=args.batch, chain_cluster_node_settings=settings)
        Save(output / 'dataset.json', dataset)
    Save(output / 'variables.json', variables)
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
