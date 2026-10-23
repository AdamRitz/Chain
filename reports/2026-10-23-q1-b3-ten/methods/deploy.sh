#!/usr/bin/env bash
set -euo pipefail
status=/opt/chain-control/logs/q1-b3-deploy.exit
test ! -e "$status"
trap 'rc=$?; printf "%s\n" "$rc" > "$status"' EXIT
cd /opt/chain-control/automation
/opt/chain-control/venv/bin/ansible-playbook /opt/chain-control/q1-b3/deploy.yml
