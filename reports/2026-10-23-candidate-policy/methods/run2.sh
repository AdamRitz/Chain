#!/usr/bin/env bash
set -euo pipefail
cd /opt/chain-control/automation
exec 9>/opt/chain-control/model-tps/experiment.lock
flock -n 9 || exit 12
status=/opt/chain-control/logs/candidate-policy-grid2.exit
test ! -e "$status"
restore() {
  rc=$?
  trap - EXIT INT TERM
  set +e
  cd /opt/chain-control/automation
  /opt/chain-control/venv/bin/ansible chain -m ansible.builtin.command -a 'systemctl start chain-node fisco-bcos' > /opt/chain-control/logs/candidate-policy-restored2.log 2>&1
  if test "$?" != 0; then rc=91; fi
  /opt/chain-control/venv/bin/ansible chain -m ansible.builtin.command -a 'systemctl is-active chain-node fisco-bcos' > /opt/chain-control/logs/candidate-policy-restore-verified2.log 2>&1
  if test "$?" != 0; then rc=92; fi
  printf '%s\n' "$rc" > "$status"
  exit "$rc"
}
trap restore EXIT
trap 'exit 130' INT TERM
/opt/chain-control/venv/bin/ansible chain -m ansible.builtin.command -a 'systemctl is-active chain-node fisco-bcos'
/opt/chain-control/venv/bin/ansible chain -m ansible.builtin.command -a 'systemctl stop chain-node fisco-bcos'
revision=0e3c1397fcd53fe6104ba4d240406bb4028696d5
release=/opt/chain/releases/$revision
binary_hash=$(/opt/chain/.venv/bin/python -c "import json;print(json.load(open('$release/validation.json'))['binaries']['boost'])")
model_hash=$(/opt/chain/.venv/bin/python -c "import json;print(json.load(open('$release/validation.json'))['model_sha256'])")
/opt/chain/.venv/bin/python /opt/chain-control/candidate-policy/methods/Run.py \
 --source "$release/source" --bin "$release/build" \
 --inventory /opt/chain-control/automation/cluster.local.json \
 --known-hosts /opt/chain-control/known_hosts \
 --plan "$release/source/Test/Clock/Benchmark/plan.json" \
 --work /opt/chain-control/results/candidate-policy-0e3c139-20261023-b \
 --phase endpoint --cycles 1 --source-revision "$revision" --expected-binary-sha256 "$binary_hash" --expected-model-sha256 "$model_hash"
