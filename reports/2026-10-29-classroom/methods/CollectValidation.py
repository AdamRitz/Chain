"""Collect existing test/build evidence; never starts a node or runs a benchmark."""
import hashlib,json,re,shutil,subprocess,tomllib
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[3]
OUT=Path(__file__).resolve().parents[1]/'data'
OUT.mkdir(exist_ok=True)
def Hash(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def Read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def Save(name,value):(OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def Main():
    build=ROOT/'build/classroom-20261028';runtime=ROOT/'Classroom/src-tauri/resources'
    xml=ROOT/'build/classroom-final-20261029.xml';tests=list(ET.parse(xml).getroot().iter('testcase'))
    assert len(tests)==15 and all(t.find('failure') is None and t.find('error') is None for t in tests)
    shutil.copy2(xml,OUT/'ctest.xml')
    cases={
        'classroom-network.json':'build/classroom-parent-final-20261029-06/result.json',
        'classroom-index.json':'build/classroom-check-20261029-04/classroom-index/result.json',
        'classroom-join.json':'build/classroom-check-20261029-04/classroom-join/result.json',
        'ipv6-network.json':'build/classroom-ipv6-final-20261029-07/result.json',
        'unicode-path.json':'build/教学网络最终验证-20261029-05/result.json',
        'parent-lifecycle.json':'build/classroom-parent-lifecycle-20261029-01/result.json'}
    for name,path in cases.items():
        result=Read(ROOT/path)
        # Process records/commands are unnecessary for the committed summary.
        if 'checks' in result:result={'checks':result['checks']}
        Save(name,result)
    tap=(ROOT/'build/classroom-web-tests.log').read_text(encoding='utf-8')
    assert '# tests 3' in tap and '# pass 3' in tap and '# fail 0' in tap
    Save('frontend-tests.json',{'passed':3,'failed':0,'checks':['canonical location replaces cached replay location','canonical ordering independent of wall clock','shared keyboard/button permissions and UTF-8 byte limit']})
    package=ROOT/'Classroom/src-tauri/target/release/bundle/nsis/Chain Classroom_0.1.0_x64-setup.exe'
    assert package.stat().st_size>100*1024*1024
    for name in ['boost.exe','chain_classroom.exe']:assert Hash(runtime/name)==Hash(build/name)
    assert Hash(runtime/'ClassroomChat.compiled.json')==Hash(ROOT/'Contracts/ClassroomChat.compiled.json')
    for entry in Read(runtime/'manifest.json')['files']:assert Hash(runtime/entry['name'])==entry['sha256']
    html=(runtime/'web/index.html').read_text(encoding='utf-8')
    for asset in re.findall(r'(?:src|href)="(/assets/[^"]+)"',html):assert (runtime/'web'/asset.lstrip('/')).is_file()
    nsis=(ROOT/'Classroom/src-tauri/target/release/nsis/x64/installer.nsi').read_text(encoding='utf-8-sig')
    for name in [r'runtime\boost.exe',r'runtime\chain_classroom.exe',r'runtime\web\index.html']:assert name in nsis
    delivery=ROOT/'Classroom/release/ChainClassroom-0.1.0-windows-x64-setup.exe';delivery.parent.mkdir(exist_ok=True);shutil.copy2(package,delivery)
    artifacts=[{'path':str(p.relative_to(ROOT)).replace('\\','/'),'bytes':p.stat().st_size,'sha256':Hash(p)} for p in [delivery,ROOT/'Classroom/src-tauri/target/release/chain-classroom.exe',build/'boost.exe',build/'chain_classroom.exe',ROOT/'Contracts/ClassroomChat.compiled.json']]
    Save('artifacts.json',{'files':artifacts,'installer_payload_paths_verified':True,'runtime_manifest_verified':True,'actual_installation_wizard_run':False,'code_signed':False})
    own=['CMakeLists.txt','main.cpp','Message/Message.h','Network/Address.h','Network/Blocks.h','Network/ClassroomQuery.h','Network/Client.h','Network/Server.h','Sender/Sender.cpp','Sender/EvmSender.cpp','EVM/BuildTransaction.h','User/User.h','Tool/Arguments.h','Tool/Path.h','Tool/Utf8.rc','Tool/Utf8.manifest','Contracts/ClassroomChat.sol','Contracts/ClassroomChat.compiled.json']
    extra=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','Classroom'],cwd=ROOT,text=True,encoding='utf-8').splitlines()
    paths=sorted(set(own+extra))
    source={name:hashlib.sha256((ROOT/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for name in paths if (ROOT/name).is_file() and not name.endswith('.ico')}
    npm=Read(ROOT/'Classroom/package-lock.json');cargo=tomllib.loads((ROOT/'Classroom/src-tauri/Cargo.lock').read_text(encoding='utf-8'))
    Save('validation.json',dict(ctest_passed=15,frontend_tests_passed=3,additional_targeted_tests=list(cases),build_type='Release; GCC 15.2.0; MinGW UCRT64; C++20',rust='1.90.0-x86_64-pc-windows-msvc',react=npm['packages']['node_modules/react']['version'],tauri=next(p['version'] for p in cargo['package'] if p['name']=='tauri'),node=subprocess.check_output(['node','--version'],text=True).strip(),base_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),source_hash_mode='SHA256 of UTF-8 text normalized to LF; built from working tree',source_files=source,cloud_deployed=False,performance_benchmarks_run=False))
    print(json.dumps({'ctest':15,'frontend':3,'installer_bytes':delivery.stat().st_size,'installer_sha256':Hash(delivery)}))

if __name__=='__main__':Main()
