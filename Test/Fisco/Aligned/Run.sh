#!/usr/bin/env bash
# Installed on the existing FISCO controller. Creates one new, isolated batch.
set -euo pipefail
batch=${1:?Usage: Run.sh UNIQUE_BATCH}
[[ "$batch" =~ ^[A-Za-z0-9_-]{1,30}$ ]] || exit 2
control=/opt/chain-control/automation
driver=/opt/fisco-control/native-aligned
ansible=/opt/chain-control/venv/bin/ansible
exec 9>/opt/chain-control/model-tps/experiment.lock
flock -n 9 || exit 12
test ! -e "/opt/fisco-control/results/$batch"
test ! -e "/opt/fisco-control/results/${batch}-smoke"
SetServices() (
    # Ansible discovers its inventory from this directory, including in EXIT.
    cd "$control"
    "$ansible" chain --list-hosts | python3 -c 'import sys; s=sys.stdin.read(); assert "hosts (10):" in s, s'
    "$ansible" chain -m ansible.builtin.command -a "systemctl $1 chain-node fisco-bcos"
)
Finish() {
    rc=$?
    trap - EXIT
    SetServices start > "/opt/fisco-control/logs/${batch}-restored.log" 2>&1 || rc=6
    printf '%s\n' "$rc" > "/opt/fisco-control/logs/${batch}.exit"
    exit "$rc"
}
trap Finish EXIT
SetServices stop
cd "$driver"
python3 -u NativeRun.py --hosts /opt/fisco-control/automation/fisco-adapter/hosts.json \
    --name "${batch}-smoke" --smoke --inflight 50000
python3 -u NativeRun.py --hosts /opt/fisco-control/automation/fisco-adapter/hosts.json \
    --name "$batch" --inflight 50000
