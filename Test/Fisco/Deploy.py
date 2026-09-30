"""Deploy isolated FISCO test nodes using interactive password authentication."""
import concurrent.futures
import configparser
import getpass
import hashlib
import io
import json
from pathlib import Path
import tarfile
import threading
import paramiko
import os

ROOT = Path(os.environ.get('FISCO_BENCH_ROOT','/home/ubuntu/fisco-bench/20260930'))
HOSTS = os.environ.get('FISCO_BENCH_HOSTS',
    '10.206.0.16,10.206.0.4,10.206.0.14,10.206.0.17,10.206.0.15,10.206.0.12,10.206.0.2,10.206.0.6,10.206.0.11,10.206.0.8').split(',')

def Remote(client, command):
    _, out, err = client.exec_command(command, timeout=180)
    output, error = out.read().decode(), err.read().decode()
    code = out.channel.recv_exit_status()
    if code: raise RuntimeError((command, code, output, error))
    return output

def Connect(host, password):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username='ubuntu', password=password, look_for_keys=False, allow_agent=False, timeout=15)
    return client

def Deploy(host, password):
    node = ROOT/'nodes'/host/'node0'
    ini = configparser.ConfigParser(); ini.read(node/'config.ini')
    ini['p2p']['listen_ip'] = host
    ini['rpc']['listen_ip'] = host
    ini['rpc']['return_input_params'] = 'false'
    ini['web3_rpc']['listen_ip'] = host
    ini['consensus']['min_seal_time'] = '100'
    ini['txpool']['limit'] = '200000'
    ini['txpool']['verify_worker_num'] = '8'
    ini['executor']['baseline_scheduler_parallel'] = 'true'
    ini['executor']['baseline_scheduler_maxthread'] = '8'
    ini['executor']['baseline_scheduler_chunksize'] = '100'
    ini['log']['level'] = 'warning'
    with (node/'config.ini').open('w') as f: ini.write(f)
    genesis = configparser.ConfigParser(); genesis.read(node/'config.genesis')
    genesis['consensus']['block_tx_count_limit'] = '10000'
    assert genesis['executor']['is_serial_execute'] == 'false'
    with (node/'config.genesis').open('w') as f: genesis.write(f)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar: tar.add(node, arcname='node0')
    buffer.seek(0)
    client = Connect(host, password)
    try:
        Remote(client, f'umask 077; mkdir -p {ROOT}/runtime; test ! -d {ROOT}/runtime/node0')
        with client.open_sftp() as sftp:
            sftp.put(str(ROOT/'downloads/fisco-bcos-upload.tar.gz'), str(ROOT/'runtime/binary.tar.gz'))
            sftp.putfo(buffer, str(ROOT/'runtime/node-config.tar.gz'))
        output = Remote(client, f'cd {ROOT}/runtime && tar -xzf binary.tar.gz && tar -xzf node-config.tar.gz && chmod +x fisco-bcos && bash node0/start.sh')
        print(host, output.strip(), flush=True)
        return {'host':host,'started':True,'genesis_sha256':hashlib.sha256((node/'config.genesis').read_bytes()).hexdigest()}
    finally: client.close()

if __name__ == '__main__':
    password = getpass.getpass('CLUSTER_PASSWORD: ')
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda host: Deploy(host,password), HOSTS))
    password = None
    (ROOT/'deployment.json').write_text(json.dumps(results,indent=2))
    print('DEPLOYED',len(results),flush=True)
