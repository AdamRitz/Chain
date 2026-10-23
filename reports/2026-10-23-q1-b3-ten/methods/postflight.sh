#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
from pathlib import Path
import json,subprocess
services={}
for name in ['chain-node','fisco-bcos']:
    services[name]={query:subprocess.run(['systemctl',query,name],capture_output=True,text=True).stdout.strip() for query in ['is-active','is-enabled']}
processes=[]
for file in Path('/proc').glob('[0-9]*/cmdline'):
    try:args=file.read_bytes().decode().split('\0')
    except (FileNotFoundError,PermissionError,UnicodeError):continue
    if any(a.startswith('/var/tmp/chain-experiments/clock-endpoint-q1b3') for a in args):processes.append(int(file.parent.name))
databases=[str(p) for p in Path('/var/tmp/chain-experiments').glob('clock-endpoint-q1b3*/node*/db')]
passed=not processes and not databases and all(v['is-active']=='active' and v['is-enabled']=='enabled' for v in services.values())
print(json.dumps({'services':services,'remaining_experiment_processes':processes,'remaining_experiment_databases':databases,'passed':passed}))
raise SystemExit(0 if passed else 1)
PY
