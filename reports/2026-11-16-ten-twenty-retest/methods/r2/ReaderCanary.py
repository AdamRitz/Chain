import json
import os
from pathlib import Path
import subprocess
import sys
import time

release = Path('/opt/chain/releases/9d34b55f1d5bf8ebab1f3fe44a86df31e18b1899')
sys.path.insert(0, str(release / 'source/Test'))
from ClusterWorker import Handle
tools = Path(__file__).resolve().parent
request = {'root': '/var/tmp/chain-retest2-canary-20261116',
           'run': 'reader-canary', 'node': 'ecs-c381-0003'}
genesis = json.loads(Path('/var/tmp/fleet20-short-20261116/fleet20-short-20261116-4d55c08809/transactions.bin.genesis.json').read_text())
result = {}
try:
    result['start'] = Handle({**request, 'action': 'start', 'binary': str(release / 'build/boost'),
                             'genesis': genesis, 'arguments': ['--port', 8099, '--bind', '127.0.0.1',
                             '--produce', 0, '--sync', 1, '--run-seconds', 1]})
    time.sleep(2)
    result['stop'] = Handle({**request, 'action': 'stop'})
    command = [sys.executable, str(tools / 'HeaderReader.py'), '--root', request['root'],
               '--run', request['run'], '--node', request['node'],
               '--library', str(release / 'build/lib/librocksdb.so.6.26')]
    p = subprocess.run(command, text=True, capture_output=True, timeout=30)
    result['export_exit'] = p.returncode
    result['export'] = json.loads(p.stdout)
    result['export_stderr'] = p.stderr
finally:
    Handle({**request, 'action': 'stop'})
    result['cleanup'] = Handle({**request, 'action': 'delete-db'})
    (tools / 'reader-canary.json').write_text(json.dumps(result, indent=2))
assert result['export_exit'] == 0 and result['export']['ok'] and result['export']['header_count'] == 1
assert result['cleanup']['removed']
print(json.dumps({'passed': True, 'headers': result['export']['header_count'], 'removed': result['cleanup']['removed']}))
