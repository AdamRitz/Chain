#!/usr/bin/env python3
"""FISCO BCOS 3.7.3: prepare once, Ansible stage, fresh transient runs."""
from __future__ import annotations
import argparse
import configparser
import datetime
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from certificates import verify_generated, repair_generated

OWNER = 'fisco100-20261126-v1'
RUN_ROOT = Path('/var/tmp/fisco-experiments')
DEFAULT_ROOT = '/opt/fisco-fleet'
DEFAULT_FLEET = '/opt/chain-fleet'
PROFILES = ('pbft100', 'rpbft5')
SOURCE = 'https://raw.githubusercontent.com/FISCO-BCOS/FISCO-BCOS/v3.7.3/tools/BcosAirBuilder/build_chain.sh'
UNUSED_ADMIN = '0x0000000000000000000000000000000000000000'

def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

def command(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, text=True, **kwargs)

def token(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,59}', value):
        raise ValueError('Invalid run/profile name')
    return value

def inside(path, base):
    path, base = Path(path), Path(base)
    if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()) or path.resolve() == base.resolve():
        raise ValueError('Path outside owned directory: ' + str(path))
    return path.resolve()

def load_inventory(args):
    source = read(args.inventory)
    group = source['all']['children']['chain']
    hosts = group['hosts']
    selected = read(args.hosts) if args.hosts else [row['ansible_host'] for row in hosts.values()]
    if not isinstance(selected, list) or len(selected) != 100 or len(set(selected)) != 100:
        raise ValueError('Exactly 100 distinct hosts required')
    for host in selected:
        if not ipaddress.ip_address(host).is_private:
            raise ValueError('Expected private cluster addresses')
    reverse = {row['ansible_host']: (name, row) for name, row in hosts.items()}
    if set(selected) != set(reverse):
        raise ValueError('Hosts differ from the authorized 100-node inventory')
    return selected, {reverse[host][0]: dict(reverse[host][1], fisco_host=host) for host in selected}

def ini_set(path, section, key, value):
    text = path.read_text()
    pattern = r'(?m)^(\s*' + re.escape(key) + r'\s*=)[^\n]*$'
    section_match = re.search(r'(?ms)^\[' + re.escape(section) + r'\]\s*\n(.*?)(?=^\[|\Z)', text)
    if not section_match:
        raise ValueError('Missing section ' + section)
    body = section_match.group(1)
    if not re.search(pattern, body):
        body += '    ' + key + '=' + str(value) + '\n'
    else:
        body = re.sub(pattern, lambda match: match.group(1) + str(value), body)
    path.write_text(text[:section_match.start(1)] + body + text[section_match.end(1):])

def pack_node(node, archive):
    """Stable package hashes permit prepare/stage retries without replacement."""
    def normalize(info):
        info.mtime = 0
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        return info
    with archive.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as output:
            output.add(node, arcname='node', filter=normalize)

def generation_script(script, root, openssl=None):
    metadata = {'source': SOURCE, 'official_script_sha256': sha(script)}
    if not openssl:
        return script, metadata
    executable = Path(openssl).resolve()
    if not executable.is_file():
        raise ValueError('Requested OpenSSL executable is missing')
    version = command([executable, 'version'], capture_output=True)
    content, count = re.subn(r'(?m)^OPENSSL_CMD=.*$', lambda _: 'OPENSSL_CMD='+shlex.quote(str(executable)), script.read_text())
    if count != 1:
        raise ValueError('Unexpected official OPENSSL_CMD assignment count')
    # The official script otherwise downloads an account-generation tool even
    # with auth_mode=false. The address is unused while auth remains disabled.
    content, admin_count = re.subn(r'(?m)^auth_admin_account=$', 'auth_admin_account='+UNUSED_ADMIN, content)
    if admin_count != 1:
        raise ValueError('Unexpected official auth_admin_account assignment count')
    content, ca_count = re.subn(r'(?m)^(\s*\$\{OPENSSL_CMD\} req -new -x509 [^\n]*-config "\$\{cert_conf\}")(\s+-out "\$\{chaindir\}"/ca\.crt[^\n]*)$',
        r'\1 -extensions v3_ca\2', content)
    from certificates import CA_EXTENSIONS
    content, section_count = re.subn(r'(?m)^\[ v4_req \]$', CA_EXTENSIONS + '\n[ v4_req ]', content)
    if ca_count != 1 or section_count != 1:
        raise ValueError('Unexpected official non-SM CA generation commands')
    patched = root/'tools/build_chain-openssl.sh'
    patched.parent.mkdir(parents=True, exist_ok=True)
    patched.write_text(content)
    metadata.update({'patched_script_sha256': sha(patched), 'patch': 'OPENSSL_CMD path, unused admin address and explicit non-SM CA extensions; auth remains false',
                     'unused_auth_admin_account': UNUSED_ADMIN, 'auth_mode': False,
                     'openssl': str(executable), 'openssl_version': version.stdout.strip()})
    return patched, metadata

def recover_generation(args, script, generation_tool):
    # Recovery validates all 100 existing identities before writing configs.
    # The official script is reused; node keys are never regenerated here.
    import recover_generated
    recover_generated.recover(argparse.Namespace(root=args.root, inventory=args.inventory,
        openssl=args.openssl, build_chain=str(script), generation_tool=str(generation_tool),
        remove_binary_copies=False))

def prepare(args):
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    hosts, inventory = load_inventory(args)
    save(root / 'hosts.json', hosts)
    save(root / 'inventory.json', {'all': {'children': {'fisco': {'hosts': inventory}}}})
    script = Path(args.build_chain) if args.build_chain else next((p for p in (root/'tools/build_chain.sh', root/'downloads/build_chain.sh') if p.exists()), root/'tools/build_chain.sh')
    if not script.is_file():
        raise ValueError('Official build_chain.sh is missing')
    generated = root / 'generated'
    if not generated.exists():
        generation_tool, dependency = generation_script(script, root, getattr(args, 'openssl', None))
        binary = Path(args.binary or root/'bin/fisco-bcos')
        generator_binary = binary if binary.is_file() else Path('/bin/true')
        if not generator_binary.is_file():
            raise ValueError('A Linux controller is required for official certificate generation')
        try:
            with (root/'generate.log').open('w') as log:
                command(['bash', generation_tool, '-l', ','.join(host+':1' for host in hosts), '-p', '30300,20200',
                         '-o', generated, '-e', generator_binary, '-v', '3.7.3', '-R', 'false', '-T', 'pbft'],
                        cwd=root, stdout=log, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            if not args.openssl:
                raise
            recover_generation(args, script, generation_tool)
        else:
            save(root/'generation.json', {'hosts': hosts, 'generator_binary': str(generator_binary), **dependency})
    elif not (root/'generation.json').exists():
        if not args.openssl:
            raise ValueError('Existing generated directory has no owner manifest; recovery requires --openssl')
        generation_tool, _ = generation_script(script, root, args.openssl)
        recover_generation(args, script, generation_tool)
    elif read(root/'generation.json')['hosts'] != hosts:
        raise ValueError('Existing generated directory has a different owner manifest')
    openssl = getattr(args, 'openssl', None) or shutil.which('openssl')
    if not openssl:
        raise ValueError('OpenSSL is required to verify generated certificates')
    if getattr(args, 'repair_ca', False):
        repair_generated(root, hosts, openssl)
    save(root/'certificate-validation.json', verify_generated(root, hosts, openssl))
    for profile in PROFILES:
        directory = root/'profiles'/profile
        directory.mkdir(parents=True, exist_ok=True)
        genesis_hashes = set()
        nodes = {}
        for name, row in inventory.items():
            original = generated/row['fisco_host']/'node0'
            node = directory/name/'node'
            if not original.is_dir():
                raise ValueError('Official node output missing: ' + str(original))
            if not node.exists():
                node.mkdir(parents=True)
                for filename in ('config.ini', 'config.genesis', 'nodes.json'):
                    shutil.copy2(original/filename, node/filename)
                shutil.copytree(original/'conf', node/'conf')
            shutil.copy2(original/'conf/ca.crt', node/'conf/ca.crt')
            config, genesis = node/'config.ini', node/'config.genesis'
            for section, key, value in [('consensus','min_seal_time',200), ('executor','enable_dag','true'),
                    ('sync','send_txs_by_tree','true'), ('sync','sync_block_by_tree','true'), ('txpool','limit',200000)]:
                ini_set(config, section, key, value)
            for section, key, value in [('consensus','block_tx_count_limit',6000), ('consensus','leader_period',1),
                    ('consensus','consensus_type','rpbft' if profile=='rpbft5' else 'pbft'),
                    ('version','compatibility_version','3.7.3'), ('executor','is_serial_execute','false')]:
                ini_set(genesis, section, key, value)
            if profile == 'rpbft5':
                ini_set(genesis, 'consensus', 'epoch_sealer_num', 5)
                ini_set(genesis, 'consensus', 'epoch_block_num', 1000)
            parsed = configparser.ConfigParser()
            parsed.read(genesis)
            if len([key for key in parsed['consensus'] if re.fullmatch(r'node\.\d+', key)]) != 100:
                raise ValueError('Genesis does not contain all 100 consensus identities')
            genesis_hashes.add(sha(genesis))
            archive = directory/(name+'.tar.gz')
            pack_node(node, archive)
            nodes[name] = {'host': row['fisco_host'], 'sha256': sha(archive),
                           'archive': str(archive), 'node_id': (node/'conf/node.nodeid').read_text().strip()}
            if not re.fullmatch('[0-9a-fA-F]{128}', nodes[name]['node_id']):
                raise ValueError('Invalid secp256k1 node identity: ' + name)
        if len(genesis_hashes) != 1 or len({row['node_id'] for row in nodes.values()}) != 100:
            raise ValueError('Genesis mismatch or duplicate node identities')
        save(directory/'manifest.json', {'owner': OWNER, 'profile': profile, 'hosts': hosts, 'nodes': nodes,
              'genesis_sha256': genesis_hashes.pop(), 'p2p_port': 30300, 'rpc_port': 20200,
              'min_seal_time': 200, 'block_tx_count_limit': 6000, 'leader_period': 1,
              'txpool_limit': 200000, 'send_txs_by_tree': True, 'sync_block_by_tree': True,
              'executor_dag': True, 'is_serial_execute': False, 'verify_worker_num': 'official default: CPU cores',
              'committee': 5 if profile=='rpbft5' else 100, 'epoch_block_num': 1000,
              'source': SOURCE})
    print(json.dumps({'prepared': True, 'hosts': len(hosts), 'profiles': PROFILES, 'sdk': str(generated/hosts[0]/'sdk')}))

def active_processes(directory):
    result = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            cwd = (path/'cwd').resolve(strict=True)
            if cwd.is_relative_to(directory):
                result.append({'pid': int(path.name), 'cwd': str(cwd), 'exe': str((path/'exe').resolve(strict=True))})
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    return result

def run_directory(args):
    token(args.run)
    path = inside(RUN_ROOT/args.run, RUN_ROOT)
    marker = read(path/'owner.json')
    if marker.get('owner') != OWNER or marker.get('run') != args.run or marker.get('profile') != args.profile:
        raise ValueError('Run ownership mismatch')
    return path, marker

def check_data_path(node):
    config = configparser.ConfigParser()
    config.read(node/'config.ini')
    if config.get('storage', 'data_path', fallback='data') not in ('data', './data'):
        raise ValueError('This experiment requires storage.data_path=data')

def unit_show(unit):
    return subprocess.run(['systemctl','show',unit,'--no-pager','--property=LoadState,ActiveState,SubState,MainPID,ExecMainStatus,WorkingDirectory,ExecStart'], text=True, capture_output=True).stdout

def remote(args):
    root = Path(args.root).resolve()
    if args.action == 'remote-stage':
        profile = root/'profiles'/args.profile
        archive = profile/'node.tar.gz'
        if sha(archive) != args.archive_sha256:
            raise ValueError('Node package hash mismatch')
        marker = profile/'installed.json'
        if marker.exists():
            if read(marker).get('owner') != OWNER or read(marker)['sha256'] != args.archive_sha256:
                raise ValueError('Refusing to replace an installed immutable profile')
            print(json.dumps({'installed': True, 'reused': True})); return
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                inside(profile/member.name, profile)
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise ValueError('Unsupported package member')
            source.extractall(profile)
        save(marker, {'owner': OWNER, 'sha256': args.archive_sha256})
        print(json.dumps({'installed': True})); return
    token(args.run)
    directory = inside(RUN_ROOT/args.run, RUN_ROOT)
    unit = 'fisco100-'+args.run+'.service'
    if args.action == 'remote-start':
        if directory.exists():
            raise ValueError('Run directory must be fresh: ' + str(directory))
        source = root/'profiles'/args.profile/'node'
        if read(source.parent/'installed.json')['owner'] != OWNER:
            raise ValueError('Profile is not installed')
        check_data_path(source)
        directory.mkdir(parents=True, mode=0o700)
        shutil.copytree(source, directory/'node')
        save(directory/'owner.json', {'owner': OWNER, 'run': args.run, 'profile': args.profile,
             'binary': str(root/'bin/fisco-bcos'), 'unit': unit, 'started': datetime.datetime.now(datetime.timezone.utc).isoformat()})
        command(['systemd-run','--unit='+unit,'--property=WorkingDirectory='+str(directory/'node'),
                 '--property=Restart=no','--property=KillMode=control-group','--property=TimeoutStopSec=30',
                 str(root/'bin/fisco-bcos'), '-c','config.ini','-g','config.genesis'], capture_output=True)
        print(json.dumps({'started': True, 'unit': unit, 'directory': str(directory)})); return
    directory, marker = run_directory(args)
    shown = unit_show(unit)
    if args.action == 'remote-stop':
        working = next((line.split('=',1)[1] for line in shown.splitlines() if line.startswith('WorkingDirectory=')), '')
        if working and working != str(directory/'node'):
            raise ValueError('Unit has a different working directory')
        execution = next((line.split('=',1)[1] for line in shown.splitlines() if line.startswith('ExecStart=')), '')
        if execution and marker['binary'] not in execution:
            raise ValueError('Unit executes a different binary')
        if working:
            command(['systemctl','stop',unit], capture_output=True)
        processes = active_processes(directory)
        if processes:
            raise RuntimeError('Run processes still active: '+json.dumps(processes))
        save(directory/'stopped.json', {'stopped': True, 'unit': unit, 'status': unit_show(unit)})
        print(json.dumps({'stopped': True})); return
    if args.action == 'remote-collect':
        if active_processes(directory):
            raise RuntimeError('Stop the run before collecting final logs')
        evidence = directory/'evidence'
        evidence.mkdir(exist_ok=True)
        for name in ('config.ini','config.genesis','nodes.json'):
            shutil.copy2(directory/'node'/name, evidence/name)
        logfiles = sorted((directory/'node'/'log').glob('*'), key=lambda p:p.stat().st_mtime)[-4:]
        logs = []
        for index, path in enumerate(logfiles):
            if not path.is_file() or path.is_symlink():
                continue
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-1024*1024))
                (evidence/('log-'+str(index)+'.tail')).write_bytes(stream.read())
            logs.append({'name':path.name,'bytes':path.stat().st_size})
        journal = subprocess.run(['journalctl','-u',unit,'--no-pager','-n','1000'], text=True, capture_output=True)
        (evidence/'journal.log').write_text(journal.stdout)
        save(evidence/'status.json', {'owner':marker, 'unit':shown, 'logs':logs, 'processes':[], 'free_bytes':shutil.disk_usage(directory).free})
        archive = directory/'evidence.tar.gz'
        with tarfile.open(archive,'w:gz') as output:
            output.add(evidence,arcname='evidence')
        print(json.dumps({'collected': True, 'sha256': sha(archive), 'bytes':archive.stat().st_size})); return
    if args.action == 'remote-clean':
        if not (directory/'stopped.json').exists() or active_processes(directory):
            raise RuntimeError('Run is not stopped')
        if sha(directory/'evidence.tar.gz') != args.evidence_sha256:
            raise ValueError('Fetched evidence does not match')
        check_data_path(directory/'node')
        data = inside(directory/'node'/'data', directory/'node')
        files, size = 0, 0
        if data.exists():
            import fcntl
            locks = []
            try:
                for path in data.rglob('*'):
                    inside(path,data)
                    if path.is_symlink(): raise ValueError('Symlink in database')
                    if path.is_file():
                        files += 1; size += path.stat().st_size
                        if path.name == 'LOCK':
                            stream = path.open('r+b'); fcntl.lockf(stream,fcntl.LOCK_EX|fcntl.LOCK_NB); locks.append(stream)
                shutil.rmtree(data)
            finally:
                for stream in locks: stream.close()
        save(directory/'cleanup.json', {'removed': True, 'files':files,'bytes':size,'evidence_sha256':args.evidence_sha256})
        print(json.dumps({'removed': True, 'files':files,'bytes':size})); return
    raise ValueError(args.action)

def play(args):
    root = Path(args.root).resolve()
    profile = read(root/'profiles'/args.profile/'manifest.json')
    if profile['owner'] != OWNER: raise ValueError('Missing profile owner')
    hosts, inventory = load_inventory(args)
    save(root/'inventory.json', {'all':{'children':{'fisco':{'hosts':inventory}}}})
    output = root/'results'/args.run if args.run else root/'stage-results'/args.profile
    output.mkdir(parents=True,exist_ok=True)
    tasks = []
    base_command = ['/usr/bin/python3',str(root/'tools/deploy.py')]
    params = ['--root',str(root),'--profile',args.profile]
    if args.run: params += ['--run',token(args.run)]
    if args.action == 'stage':
        openssl = getattr(args, 'openssl', None) or shutil.which('openssl')
        if not openssl:
            raise ValueError('OpenSSL is required before staging TLS certificates')
        verify_generated(root, hosts, openssl)
        binary = Path(args.binary or root/'bin/fisco-bcos')
        version = command([binary,'--version'],capture_output=True)
        if '3.7.3' not in version.stdout+version.stderr: raise ValueError('Expected official binary 3.7.3')
        tasks = [{'name':'Create owned directories','ansible.builtin.file':{'path':str(root/'{{ item }}'),'state':'directory','mode':'0700'},'loop':['bin','tools','profiles/'+args.profile]},
                 {'name':'Stage official binary once','ansible.builtin.copy':{'src':str(binary),'dest':str(root/'bin/fisco-bcos'),'mode':'0755'}},
                 {'name':'Stage run controller','ansible.builtin.copy':{'src':str(Path(__file__).resolve()),'dest':str(root/'tools/deploy.py'),'mode':'0755'}},
                 {'name':'Stage TLS certificate validator','ansible.builtin.copy':{'src':str(Path(__file__).with_name('certificates.py').resolve()),'dest':str(root/'tools/certificates.py'),'mode':'0644'}},
                 {'name':'Stage only this host certificates and config','ansible.builtin.copy':{'src':str(root/'profiles'/args.profile/'{{ inventory_hostname }}.tar.gz'),'dest':str(root/'profiles'/args.profile/'node.tar.gz'),'mode':'0600'},'no_log':True},
                 {'name':'Verify and install profile','ansible.builtin.command':{'argv':base_command+['remote-stage']+params+['--archive-sha256',"{{ fisco_package_hash[inventory_hostname] }}"]}}]
        save(output/'binary.json', {'sha256':sha(binary),'version':version.stdout+version.stderr})
    else:
        action = {'start':'remote-start','stop':'remote-stop','collect':'remote-collect','clean':'remote-clean'}[args.action]
        argv = base_command+[action]+params
        if args.action == 'clean':
            collected = read(output/'COLLECTED.json')
            for name in inventory:
                if sha(output/(name+'.tar.gz')) != collected[name]: raise ValueError('Missing fetched host evidence: '+name)
            argv += ['--evidence-sha256','{{ fisco_evidence_hash[inventory_hostname] }}']
        tasks = [{'name':args.action+' owned FISCO run','ansible.builtin.command':{'argv':argv},'register':'fisco_result'}]
        if args.action == 'collect':
            tasks += [{'name':'Fetch final evidence','ansible.builtin.fetch':{'src':str(RUN_ROOT/args.run/'evidence.tar.gz'),'dest':str(output/'{{ inventory_hostname }}.tar.gz'),'flat':True}}]
    variables = {'fisco_package_hash':{name:row['sha256'] for name,row in profile['nodes'].items()}}
    if args.action == 'clean': variables['fisco_evidence_hash'] = collected
    playbook = [{'name':'FISCO100 '+args.action,'hosts':'fisco','gather_facts':False,'strategy':'free','vars':variables,'tasks':tasks}]
    play_path = output/(args.action+'.json')
    save(play_path,playbook)
    ansible = args.ansible or str(Path(sys.executable).parent/'ansible-playbook')
    cmd = [ansible,'-i',root/'inventory.json',play_path,'--forks',str(args.forks),'-e','@'+str(Path(args.connection).resolve())]
    if args.dry_run:
        print(json.dumps({'command':[str(x) for x in cmd],'playbook':str(play_path)})); return
    if not os.environ.get('CHAIN_SSH_PASSWORD'): raise ValueError('Export CHAIN_SSH_PASSWORD')
    env = os.environ.copy(); env['PYTHONDONTWRITEBYTECODE']='1'
    with (output/(args.action+'.log')).open('w') as log:
        command(cmd,env=env,stdout=log,stderr=subprocess.STDOUT)
    if args.action == 'collect':
        save(output/'COLLECTED.json',{name:sha(output/(name+'.tar.gz')) for name in inventory})
    print(json.dumps({'action':args.action,'passed':True,'output':str(output)}))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','stage','start','stop','collect','clean','remote-stage','remote-start','remote-stop','remote-collect','remote-clean'])
    parser.add_argument('--root',default=DEFAULT_ROOT)
    parser.add_argument('--inventory',default=DEFAULT_FLEET+'/inventory.json')
    parser.add_argument('--connection',default=DEFAULT_FLEET+'/connection-env.json')
    parser.add_argument('--hosts')
    parser.add_argument('--binary')
    parser.add_argument('--build-chain')
    parser.add_argument('--openssl', help='Use this OpenSSL executable for non-SM certificate generation; preserves the downloaded script')
    parser.add_argument('--repair-ca', action='store_true', help='With prepare, repair the existing CA using its original key before packaging')
    parser.add_argument('--profile',choices=PROFILES,default='pbft100')
    parser.add_argument('--run')
    parser.add_argument('--archive-sha256')
    parser.add_argument('--evidence-sha256')
    parser.add_argument('--ansible')
    parser.add_argument('--forks',type=int,default=32)
    parser.add_argument('--dry-run',action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare': prepare(args)
    elif args.action.startswith('remote-'): remote(args)
    else:
        if args.action != 'stage' and not args.run: parser.error('--run is required')
        play(args)

if __name__ == '__main__':
    main()
