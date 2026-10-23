#!/usr/bin/env bash
set -euo pipefail
revision=6ca799a49ce1a3a7ab264ccdda2baaa0d0cd71dd
release=/opt/chain/releases/$revision
archive=/opt/chain-control/packages/chain-source-$revision.tar.gz
status=/opt/chain-control/logs/q1-b3-build.exit
trap 'rc=$?; printf "%s\n" "$rc" > "$status"' EXIT
printf '9791140e667b58adda6fa4717373a8977dab54a3d9facc97d8dff2cdfc833fdd  %s\n' "$archive" | sha256sum --check
test ! -e "$release"
install -d -m 0755 "$release/source" "$release/build"
tar -xzf "$archive" -C "$release/source"
printf '%s\n' "$revision" > "$release/REVISION"
source /opt/rh/gcc-toolset-13/enable
echo CLOCK_BUILD_CONFIGURE
cmake -S "$release/source" -B "$release/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DRocksDB_DIR=/opt/chain/cmake \
  -DCMAKE_CXX_FLAGS_RELWITHDEBINFO='-O2 -g -fno-omit-frame-pointer -DNDEBUG' \
  -DPython3_EXECUTABLE=/opt/chain/.venv/bin/python \
  -DCHAIN_TEST_DATA_ROOT="$release/build/check-q1-b3-20261023"
echo CLOCK_BUILD_COMPILE
cmake --build "$release/build" --parallel 2
echo CLOCK_BUILD_CTEST
ctest --test-dir "$release/build" --output-on-failure --output-junit "$release/ctest.xml"
echo CLOCK_BUILD_OPTIMIZED_EVM
/opt/chain/.venv/bin/python "$release/source/Test/EvmNetworkTest.py" --bin "$release/build" --work "$release/build/check-q1-b3-evm-20261023" --optimized
echo CLOCK_BUILD_PACKAGE
/opt/chain/.venv/bin/python - <<'PY'
import hashlib,json,tarfile,time
from pathlib import Path
revision='6ca799a49ce1a3a7ab264ccdda2baaa0d0cd71dd'
release=Path('/opt/chain/releases')/revision
def Hash(path):
    with path.open('rb') as source:
        digest=hashlib.sha256()
        for part in iter(lambda:source.read(1048576),b''):digest.update(part)
        return digest.hexdigest()
data={'revision':revision,'tests':14,'ctest_passed':True,'built_at':time.time(),
      'source_sha256':Hash(Path('/opt/chain-control/packages/chain-source-'+revision+'.tar.gz')),
      'model_sha256':Hash(release/'source/Time/Models/preparation-v1.json'),
      'models':{name:Hash(release/'source/Time/Models'/name) for name in ['preparation-v1.json','preparation-v2.json']},
      'binaries':{name:Hash(release/'build'/name) for name in ['boost','sender','evm_sender']}}
(release/'validation.json').write_text(json.dumps(data,indent=2)+'\n')
bundle=Path('/opt/chain-control/packages/chain-binaries-'+revision+'.tar.gz')
with tarfile.open(bundle,'w:gz') as archive:
    for name in ['boost','sender','evm_sender']:archive.add(release/'build'/name,arcname=name)
data['binary_archive_sha256']=Hash(bundle)
(release/'validation.json').write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps(data),flush=True)
PY
echo CLOCK_BUILD_READY
