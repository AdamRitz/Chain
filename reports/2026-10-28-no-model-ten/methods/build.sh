#!/usr/bin/env bash
set -euo pipefail
revision=1504583cda142c8f50fcc6215864160ae469f333
expected=$1
[[ "$expected" =~ ^[0-9a-f]{64}$ ]]
release=/opt/chain/releases/$revision
archive=/opt/chain-control/packages/chain-source-$revision.tar.gz
status=/opt/chain-control/logs/no-model-build-20261028.exit
trap 'rc=$?; printf "%s\n" "$rc" > "$status"' EXIT
printf '%s  %s\n' "$expected" "$archive" | sha256sum --check
test ! -e "$release"
install -d -m 0755 "$release/source" "$release/build"
tar -xzf "$archive" -C "$release/source"
printf '%s\n' "$revision" > "$release/REVISION"
source /opt/rh/gcc-toolset-13/enable
cmake -S "$release/source" -B "$release/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DRocksDB_DIR=/opt/chain/cmake \
  -DCMAKE_CXX_FLAGS_RELWITHDEBINFO='-O2 -g -fno-omit-frame-pointer -DNDEBUG' \
  -DPython3_EXECUTABLE=/opt/chain/.venv/bin/python \
  -DCHAIN_TEST_DATA_ROOT="$release/build/check-no-model-20261028"
cmake --build "$release/build" --parallel 2
ctest --test-dir "$release/build" --output-on-failure --output-junit "$release/ctest.xml"
/opt/chain/.venv/bin/python - "$revision" <<'PY'
import hashlib,json,tarfile,sys,time,xml.etree.ElementTree as ET
from pathlib import Path
revision=sys.argv[1];release=Path('/opt/chain/releases')/revision
def Hash(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda:f.read(1024*1024),b''):h.update(data)
    return h.hexdigest()
tests=list(ET.parse(release/'ctest.xml').getroot().iter('testcase'))
assert len(tests)==11 and all(t.find('failure') is None and t.find('error') is None for t in tests)
record=dict(revision=revision,baseline='933905448951c600156a8ad2a0b3c3a895916fb5',tests=11,ctest_passed=True,
            built_at=time.time(),compiler='GCC toolset 13; -O2 -g -fno-omit-frame-pointer -DNDEBUG',
            source_sha256=Hash(Path('/opt/chain-control/packages/chain-source-'+revision+'.tar.gz')),
            binaries={n:Hash(release/'build'/n) for n in ['boost','sender','evm_sender']})
bundle=Path('/opt/chain-control/packages/chain-binaries-'+revision+'.tar.gz')
with tarfile.open(bundle,'w:gz') as archive:
    for name in ['boost','sender','evm_sender']:archive.add(release/'build'/name,arcname=name)
record['binary_archive_sha256']=Hash(bundle)
(release/'validation.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record),flush=True)
PY
