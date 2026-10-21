#!/usr/bin/env bash
set -uo pipefail
cd /opt/chain-control/automation
restore() {
  /opt/chain-control/venv/bin/ansible chain -m ansible.builtin.command -a 'systemctl start chain-node fisco-bcos' > /opt/chain-control/logs/clock-restored.log 2>&1
}
trap restore EXIT
/opt/chain-control/venv/bin/ansible chain -m ansible.builtin.command -a 'systemctl stop chain-node fisco-bcos' || exit 2
revision=070fbb46ff25bce38d9599c5165dce66d63fc561
release=/opt/chain/releases/$revision
binary_hash=$(/opt/chain/.venv/bin/python -c "import json;print(json.load(open('$release/validation.json'))['binaries']['boost'])")
model_hash=$(/opt/chain/.venv/bin/python -c "import json;print(json.load(open('$release/validation.json'))['model_sha256'])")
/opt/chain/.venv/bin/python /opt/chain-control/clock/Run.py \
 --source "$release/source" --bin "$release/build" \
 --inventory /opt/chain-control/automation/cluster.local.json \
 --known-hosts /opt/chain-control/known_hosts \
 --plan /opt/chain-control/clock/plan.json \
 --work /opt/chain-control/results/clock-070fbb4-20261021 \
 --source-revision "$revision" --expected-binary-sha256 "$binary_hash" --expected-model-sha256 "$model_hash"
rc=$?
printf '%s\n' "$rc" > /opt/chain-control/logs/clock-grid.exit
exit "$rc"
