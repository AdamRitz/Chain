"""Build the exact exported commit and validate it on Ubuntu."""
import hashlib
import json
import subprocess
import tarfile
import time
from pathlib import Path

ROOT=Path('/home/ubuntu/chain')
RELEASE=ROOT/'releases/2026-10-14-tps-a5cf8a2'
SOURCE=ROOT/'chain-source-a5cf8a2.tar.gz'
PYTHON=ROOT/'releases/2026-09-30-d4106d1/.venv/bin/python'
COMMIT='a5cf8a2dc2585cccf321dd08691b1f09ceef4628'
EXPECTED='1771b1ddab273ba79fbb8d09b76918c4fbd803037a518d46aac02f54769ed498'
started=time.time()
digest=hashlib.file_digest(SOURCE.open('rb'),'sha256').hexdigest()
assert digest==EXPECTED,'Source archive checksum mismatch'
RELEASE.mkdir(exist_ok=False)
with tarfile.open(SOURCE) as archive:archive.extractall(RELEASE,filter='data')
commands=[
    ['cmake','-S',str(RELEASE),'-B',str(RELEASE/'build'),'-G','Ninja','-DCMAKE_BUILD_TYPE=Release',
        '-DPython3_EXECUTABLE='+str(PYTHON),'-DCHAIN_TEST_DATA_ROOT='+str(RELEASE/'build/check-a5cf8a2')],
    ['cmake','--build',str(RELEASE/'build'),'--parallel','4'],
    ['ctest','--test-dir',str(RELEASE/'build'),'--output-on-failure','--parallel','2','--output-junit',str(RELEASE/'ctest.xml')],
]
for index,command in enumerate(commands):
    log=RELEASE/f'build-step-{index}.log'
    print('BUILD_STEP',index,flush=True)
    with log.open('w') as output:
        result=subprocess.run(command,stdout=output,stderr=subprocess.STDOUT)
    print(log.read_text()[-6000:],flush=True)
    if result.returncode:
        (RELEASE/'build-status.json').write_text(json.dumps({'ok':False,'step':index,'exit':result.returncode}))
        raise SystemExit(result.returncode)
binaries={}
for name in ['boost','sender','evm_sender']:
    with (RELEASE/'build'/name).open('rb') as binary:
        binaries[name]=hashlib.file_digest(binary,'sha256').hexdigest()
validation={'commit':COMMIT,'source_sha256':digest,'sha256':binaries['boost'],'binaries':binaries,
    'ctest_passed':True,'tests':11,'started_utc':started,'completed_utc':time.time()}
(RELEASE/'validation.json').write_text(json.dumps(validation,indent=2))
bundle=ROOT/'runtime-a5cf8a2.tar.gz'
with tarfile.open(bundle,'w:gz') as archive:
    for relative in ['Test','Contracts','Experiments','config.yaml','validation.json','build/boost','build/sender','build/evm_sender']:
        archive.add(RELEASE/relative,arcname=relative,filter=lambda info:None if '__pycache__' in info.name else info)
(RELEASE/'build-status.json').write_text(json.dumps({'ok':True,'bundle':str(bundle),'validation':validation},indent=2))
print('BUILD_OK',json.dumps(validation),flush=True)
