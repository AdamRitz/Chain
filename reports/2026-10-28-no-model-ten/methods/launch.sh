#!/usr/bin/env bash
set -euo pipefail
exec 9>/opt/chain-control/model-tps/experiment.lock
flock -n 9
task=/opt/chain-control/no-model-20261028
release=/opt/chain/releases/1504583cda142c8f50fcc6215864160ae469f333
status=/opt/chain-control/logs/no-model-ten-20261028.exit
cd /opt/chain-control/automation
ansible=/opt/chain-control/venv/bin/ansible
Restore() {
  rc=$?
  trap - EXIT
  $ansible chain -m ansible.builtin.shell -a 'systemctl start chain-node fisco-bcos; systemctl is-active chain-node fisco-bcos' > "$task/services-after.txt" 2>&1 || rc=1
  printf '%s\n' "$rc" > "$status"
  exit "$rc"
}
$ansible chain -m ansible.builtin.shell -a 'systemctl is-active chain-node fisco-bcos' > "$task/services-before.txt"
# All twenty services must be active before the reversible pause.
test "$(grep -c '^active$' "$task/services-before.txt")" = 20
trap Restore EXIT
$ansible chain -m ansible.builtin.shell -a 'systemctl stop chain-node fisco-bcos; ! systemctl is-active --quiet chain-node; ! systemctl is-active --quiet fisco-bcos' > "$task/services-paused.txt"
/opt/chain/.venv/bin/python "$task/Run.py" \
  --source "$release/source" --bin "$release/build" \
  --source-revision 1504583cda142c8f50fcc6215864160ae469f333 \
  --inventory "$task/inventory.json" --plan "$task/plan.json" \
  --known-hosts /opt/chain-control/known_hosts \
  --dataset /opt/chain-control/results/chain-comparison-20261021/transactions.bin \
  --expected-binary-sha256 d35647e5599a3882c6b54f87dc22f44dd1f1e289b7761bb51aa2e28e0a3a8e11 \
  --work /opt/chain-control/results/no-model-1504583-20261028
