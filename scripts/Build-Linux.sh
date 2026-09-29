#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${1:-}" == "--install" ]]; then
    sudo apt-get update
    sudo apt-get install -y build-essential cmake ninja-build libboost-dev librocksdb-dev \
        libsodium-dev libspdlog-dev libyaml-cpp-dev nlohmann-json3-dev python3-venv iproute2
fi
python3 -m venv .venv
.venv/bin/python -m pip install -r Experiments/requirements.txt
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DPython3_EXECUTABLE="$PWD/.venv/bin/python" \
    -DCHAIN_TEST_DATA_ROOT="$PWD/build/check-$(date +%Y%m%d-%H%M%S)-$$"
cmake --build build --parallel "${CHAIN_BUILD_JOBS:-4}"
ctest --test-dir build --output-on-failure
