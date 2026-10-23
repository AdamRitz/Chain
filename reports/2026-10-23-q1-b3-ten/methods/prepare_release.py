from pathlib import Path
import hashlib,json,subprocess

here=Path(__file__).resolve().parent
repo=Path('D:/Project/Chain')
revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
archive=here/'source.tar.gz'
subprocess.run(['git','archive','--format=tar.gz','--output',str(archive),revision],cwd=repo,check=True)
digest=hashlib.sha256(archive.read_bytes()).hexdigest()
old='d28a2fb2146a9afcff4f8f0758cd7c248e1023b6'
build=(here.parent/'policy-v2/build.sh').read_text().replace(old,revision)
build=build.replace('c543ce52a4ef51a14438eb429e1866c527698ccc835b54a4ffbd336cf2dbe513',digest)
build=build.replace('policy-v2-build','q1-b3-build').replace('check-policy-v2-20261023','check-q1-b3-20261023')
build=build.replace('echo CLOCK_BUILD_PACKAGE', '''echo CLOCK_BUILD_OPTIMIZED_EVM
/opt/chain/.venv/bin/python "$release/source/Test/EvmNetworkTest.py" --bin "$release/build" --work "$release/build/check-q1-b3-evm-20261023" --optimized
echo CLOCK_BUILD_PACKAGE''')
(here/'build.sh').write_text(build,encoding='utf-8',newline='\n')
deploy=(here.parent/'policy-v2/deploy.yml').read_text().replace(old,revision)
(here/'deploy.yml').write_text(deploy,encoding='utf-8',newline='\n')
deploy_sh=(here.parent/'policy-v2/deploy.sh').read_text().replace('policy-v2','q1-b3')
(here/'deploy.sh').write_text(deploy_sh,encoding='utf-8',newline='\n')
run=(here.parent/'policy-v2/run-fixed.sh').read_text().replace(old,revision)
run=run.replace('policy-v2-grid-fixed','q1-b3-grid').replace('policy-v2-restored-fixed','q1-b3-restored').replace('policy-v2-restore-verified-fixed','q1-b3-restore-verified')
run=run.replace('driver=/opt/chain-control/policy-v2/driver-c2c0c38536768f2d8a596c4c4f215e89470555a6','driver="$release/source"')
run=run.replace('/opt/chain-control/results/policy-v2-d28a2fb-20261023-b',f'/opt/chain-control/results/q1-b3-{revision[:7]}-20261023')
run=run.replace('--policy-v2','--policy-v3').replace('--run-tag v2fixed','--run-tag q1b3')
(here/'run.sh').write_text(run,encoding='utf-8',newline='\n')
postflight=(here.parent/'policy-v2/postflight.sh').read_text().replace('clock-endpoint-v2','clock-endpoint-q1b3')
(here/'postflight.sh').write_text(postflight,encoding='utf-8',newline='\n')
meta={'revision':revision,'runtime_base':'97dc3379431263167fd3068881a09a1252f367cc','source_sha256':digest,
      'source_bytes':archive.stat().st_size,'remote_results':f'/opt/chain-control/results/q1-b3-{revision[:7]}-20261023'}
(here/'release.json').write_text(json.dumps(meta,indent=2)+'\n')
print(json.dumps(meta))
