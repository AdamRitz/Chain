#!/usr/bin/env python3
"""Finish official configuration generation using the existing 100 identities."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def recover(args):
    root = Path(args.root).resolve()
    generated = root/'generated'
    hosts = json.loads((root/'hosts.json').read_text())
    inventory = json.loads(Path(args.inventory).read_text())['all']['children']['chain']['hosts']
    if len(hosts) != 100 or len(set(hosts)) != 100 or set(hosts) != {row['ansible_host'] for row in inventory.values()}:
        raise ValueError('Expected the authorized 100 hosts')
    node_ids, identities = [], {}
    for host in hosts:
        conf = generated/host/'node0/conf'
        for name in ('ca.crt','ssl.crt','ssl.key','node.pem','node.nodeid'):
            path = conf/name
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                raise ValueError('Existing node identity missing: '+str(path))
        identity = (conf/'node.nodeid').read_text().strip()
        if not re.fullmatch('[0-9a-fA-F]{128}', identity):
            raise ValueError('Invalid node ID: '+host)
        node_ids.append(identity)
        identities[host] = {name:sha(conf/name) for name in ('ca.crt','ssl.crt','ssl.key','node.pem','node.nodeid')}
    if len(set(node_ids)) != 100:
        raise ValueError('Duplicate identities')
    original = Path(getattr(args, 'build_chain', None) or root/'downloads/build_chain.sh')
    source = Path(getattr(args, 'generation_tool', None) or root/'tools/build_chain-openssl.sh')
    content, count = re.subn(r'(?m)^main "\$@"\s*$', '', source.read_text())
    if count != 1:
        raise ValueError('Expected exactly one final main call')
    definitions = root/'tools/build_chain-functions-recovery.sh'
    definitions.write_text(content)
    lines = ['#!/bin/bash', 'set -e', 'source '+shlex.quote(str(definitions)),
             'auth_mode=false', 'auth_admin_account=0x0000000000000000000000000000000000000000',
             'serial_mode=false', 'compatibility_version=3.7.3', 'consensus_type=pbft',
             'nodeid_list='+shlex.quote(''.join('node.%d=%s: 1\n'%(i,node) for i,node in enumerate(node_ids))),
             'connected_nodes='+shlex.quote(','.join(host+':30300' for host in hosts)+',')]
    for host in hosts:
        node = generated/host/'node0'
        lines += ['generate_config false '+shlex.quote(str(node/'config.ini'))+' 0.0.0.0 30300 0.0.0.0 20200',
                  'generate_p2p_connected_conf '+shlex.quote(str(node/'nodes.json'))+' "$connected_nodes" false',
                  'generate_genesis_config '+shlex.quote(str(node/'config.genesis'))+' "$nodeid_list"']
    script = root/'tools/recover-configs.sh'
    script.write_text('\n'.join(lines)+'\n')
    with (root/'recover-configs.log').open('w') as log:
        subprocess.run(['bash',str(script)],cwd=root,stdout=log,stderr=subprocess.STDOUT,check=True)
    genesis_hashes = set()
    for host in hosts:
        node = generated/host/'node0'
        genesis_hashes.add(sha(node/'config.genesis'))
        if any(sha(node/'conf'/name) != digest for name,digest in identities[host].items()):
            raise ValueError('Identity changed during recovery')
        peers = json.loads((node/'nodes.json').read_text())['nodes']
        if len(peers) != 100 or set(peers) != {ip+':30300' for ip in hosts}:
            raise ValueError('Incorrect generated peer list')
    if len(genesis_hashes) != 1:
        raise ValueError('Generated genesis differs across nodes')
    version = subprocess.run([args.openssl,'version'],check=True,text=True,capture_output=True).stdout.strip()
    manifest = {'hosts':hosts,'official_script_sha256':sha(original),'patched_script_sha256':sha(source),
                'source':'https://raw.githubusercontent.com/FISCO-BCOS/FISCO-BCOS/v3.7.3/tools/BcosAirBuilder/build_chain.sh',
                'openssl':args.openssl,'openssl_version':version,'recovered_existing_identities':True,
                'patch':'OPENSSL_CMD path; recovery calls official configuration functions, auth disabled',
                'recovery_script_sha256':sha(script),'functions_sha256':sha(definitions),
                'unused_auth_admin_account':'0x0000000000000000000000000000000000000000',
                'identity_sha256':identities,'genesis_sha256':genesis_hashes.pop()}
    (root/'generation.json').write_text(json.dumps(manifest,indent=2)+'\n')
    removed = []
    if args.remove_binary_copies:
        binary = root/'bin/fisco-bcos'
        expected = sha(binary)
        copies = [generated/host/'fisco-bcos' for host in hosts]
        for path in copies:
            if path.exists() and (path.is_symlink() or path.resolve().parent.parent != generated or sha(path) != expected):
                raise ValueError('Unexpected generated binary copy: '+str(path))
        for path in copies:
            if path.exists():
                removed.append({'path':str(path),'bytes':path.stat().st_size,'sha256':expected})
                path.unlink()
    result = {'recovered':True,'hosts':100,'identities_unchanged':True,'binary_copies_removed':removed}
    (root/'recovery.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'recovered':True,'hosts':100,'identities_unchanged':True,
                      'binary_copies_removed':len(removed),'reclaimed_bytes':sum(row['bytes'] for row in removed)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='/opt/fisco-fleet')
    parser.add_argument('--inventory',default='/opt/chain-fleet/inventory.json')
    parser.add_argument('--openssl',default='/usr/bin/openssl')
    parser.add_argument('--remove-binary-copies',action='store_true')
    recover(parser.parse_args())
