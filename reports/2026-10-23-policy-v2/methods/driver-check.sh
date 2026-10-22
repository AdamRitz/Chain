#!/usr/bin/env bash
set -euo pipefail
status=/opt/chain-control/logs/policy-v2-driver.exit
test ! -e "$status"
trap 'rc=$?; printf "%s\n" "$rc" > "$status"' EXIT
cd /opt/chain-control/automation
/opt/chain-control/venv/bin/ansible-playbook /opt/chain-control/policy-v2/driver.yml
/opt/chain/.venv/bin/python /opt/chain-control/policy-v2/driver-c2c0c38536768f2d8a596c4c4f215e89470555a6/Test/ClockToolsTest.py
/opt/chain/.venv/bin/python /opt/chain-control/policy-v2/driver-c2c0c38536768f2d8a596c4c4f215e89470555a6/Test/ClockNetworkTest.py --bin /opt/chain/releases/d28a2fb2146a9afcff4f8f0758cd7c248e1023b6/build --work /opt/chain-control/policy-v2/driver-checks/clock
/opt/chain/.venv/bin/python /opt/chain-control/policy-v2/driver-c2c0c38536768f2d8a596c4c4f215e89470555a6/Test/ExperimentTest.py --bin /opt/chain/releases/d28a2fb2146a9afcff4f8f0758cd7c248e1023b6/build --work /opt/chain-control/policy-v2/driver-checks/experiment
