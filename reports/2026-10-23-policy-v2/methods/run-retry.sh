#!/usr/bin/env bash
set -euo pipefail
cd /opt/chain-control/automation
exec 9>/opt/chain-control/model-tps/experiment.lock
flock -n 9 || exit 12
status=/opt/chain-control/logs/policy-v2-retry.exit
test ! -e "$status"
restore() {
  rc=$?
  trap - EXIT INT TERM
  set +e
  cd /opt/chain-control/automation
  /opt/chain-control/venv/bin/ansible chain -m ansible.builtin.command -a 'systemctl start chain-node fisco-bcos' > /opt/chain-control/logs/policy-v2-restored-retry.log 2>&1
  if test "$?" != 0; then rc=91; fi
  /opt/chain-control/venv/bin/ansible chain -m ansible.builtin.command -a 'systemctl is-active chain-node fisco-bcos' > /opt/chain-control/logs/policy-v2-restore-verified-retry.log 2>&1
  if test "$?" != 0; then rc=92; fi
  printf '%s\n' "$rc" > "$status"
  exit "$rc"
}
trap restore EXIT
trap 'exit 130' INT TERM
/opt/chain-control/venv/bin/ansible chain -m ansible.builtin.command -a 'systemctl is-active chain-node fisco-bcos'
/opt/chain-control/venv/bin/ansible chain -m ansible.builtin.command -a 'systemctl stop chain-node fisco-bcos'
/opt/chain/.venv/bin/python /opt/chain-control/policy-v2/retry.py
