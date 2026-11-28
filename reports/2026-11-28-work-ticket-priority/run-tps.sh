#!/usr/bin/env bash
set -euo pipefail
ROOT=/opt/chain-work-priority-20261128
PROFILE=/opt/chain-continuous32-20261126
SOURCE=/opt/chain-vdf/releases/vdf/work-priority-003/source
PYTHON=/opt/chain-vdf/setup/venv/bin/python
ANSIBLE=/opt/chain-vdf/setup/venv/bin/ansible-playbook
MODE=${1:-}
[[ -n "$MODE" ]] || { printf 'Usage: %s {prepare|prepare-snapshot|test} [run-name]\n' "$0" >&2; exit 2; }
RUN=${2:-work100-$(date +%Y%m%d-%H%M%S)}
[[ "$MODE" == prepare || "$MODE" == prepare-snapshot || "$MODE" == test ]]
[[ "$RUN" =~ ^[A-Za-z0-9_-]{1,60}$ ]]
export PYTHONDONTWRITEBYTECODE=1 ANSIBLE_CONFIG="$SOURCE/ansible/ansible.cfg"
COMMON=(--plan "$PROFILE/plan.json" --dataset "$PROFILE/dataset/transactions.bin"
    --dataset-genesis "$PROFILE/dataset/transactions.bin.genesis.json"
    --dataset-manifest "$PROFILE/dataset.json" --node-settings "$ROOT/node-settings.json"
    --work-election --work-iterations 65536 --work-groups 4 --work-group-size 8
    --rates 8000 9000 10000 11000 --duration 30 --warmup 2 --drain-seconds 5 --wallets 1024
    --readiness-timeout 180 --consensus-timeout 180)
"$PYTHON" -c 'import json,sys; p=json.load(open(sys.argv[1])); assert len(p["nodes"])==100 and len({n["id"] for n in p["nodes"]})==100 and len(p["injectors"])==4' "$PROFILE/plan.json"
if [[ "$MODE" == prepare ]]; then
    exec "$PYTHON" -B "$SOURCE/ansible/scripts/cluster_quick.py" prepare \
        "${COMMON[@]}" --output "$ROOT/prepared/$RUN"
fi
SNAPSHOT="$ROOT/history-001/snapshot.json"
[[ -f "$SNAPSHOT" ]] || { printf 'Prepared Work snapshot is missing: %s\n' "$SNAPSHOT" >&2; exit 1; }
COMMON+=(--work-snapshot "$SNAPSHOT")
if [[ "$MODE" == prepare-snapshot ]]; then
    exec "$PYTHON" -B "$SOURCE/ansible/scripts/cluster_quick.py" prepare \
        "${COMMON[@]}" --output "$ROOT/prepared/$RUN"
fi
[[ -f "$ROOT/build-v5/build-completed-002.json" && -f "$ROOT/build-v5/package.json" ]]
"$PYTHON" -c 'import json,sys; assert json.load(open(sys.argv[1]))["exit_code"]==0' "$ROOT/build-v5/build-completed-002.json"
SHA=$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1]))["sha256"])' "$ROOT/build-v5/package.json")
PACKAGE="$ROOT/build-v5/work-priority-003.tar.gz"
RELEASE=work-priority-003
exec 9>"$ROOT/series.lock"
flock -n 9
if [[ -z ${CHAIN_SSH_PASSWORD:-} ]]; then
    read -r -s -p 'SSH root password: ' CHAIN_SSH_PASSWORD
    printf '\n'
fi
[[ -n "$CHAIN_SSH_PASSWORD" ]]
export CHAIN_SSH_PASSWORD
"$PYTHON" -B "$SOURCE/ansible/scripts/stage_release.py" --archive "$PACKAGE" \
    --release-root "$ROOT/controller" --release-id "$RELEASE" --sha256 "$SHA"
export LD_LIBRARY_PATH="$ROOT/controller/$RELEASE/lib"
bash "$PROFILE/sync-clock.sh"
exec "$PYTHON" -B "$SOURCE/ansible/scripts/cluster_quick.py" test "${COMMON[@]}" \
    --release-id "$RELEASE" --package "$PACKAGE" --package-sha256 "$SHA" \
    --sender "$ROOT/controller/$RELEASE/bin/sender" \
    --extra-vars-file "$PROFILE/connection-env.json" --ansible-playbook "$ANSIBLE" \
    --python /usr/bin/python3 --forks 32 --query-workers 32 --batch 128 \
    --early-stop-start-seconds 5 --early-stop-window-seconds 5 --early-stop-windows 2 \
    --output "$ROOT/results/$RUN"
