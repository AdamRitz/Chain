"""Disposable-path installer check; requires no existing registered installation.

Run old, prepare, upgrade, then close the native window normally and run uninstall.
Firewall permission is never requested when this isolated installation has no rule.
"""
import importlib.util,json,os,subprocess,sys,time,winreg
from pathlib import Path
REPO=Path(__file__).resolve().parents[3]
REPORT=Path(__file__).resolve().parents[1]
ROOT=REPO/'build/classroom-network-install-20261029-01'
spec=importlib.util.spec_from_file_location('check',REPO/'reports/2026-10-29-classroom-install/methods/CheckInstaller.py')
check=importlib.util.module_from_spec(spec);spec.loader.exec_module(check)
check.INSTALL=ROOT/'installed';check.DATA=ROOT/'ui/data';check.MARKER=check.DATA/'network-install-probe.txt'

def Main():
    action=sys.argv[1]
    if action=='prepare':
        sys.path.insert(0,str(REPO/'Test'))
        from ClassroomJoinTest import App
        app=App(REPO/'build/classroom-20261028',REPO,ROOT/'ui')
        app.Api('create',mode='teacher',title='NornV2 联网课堂验证')
        app.Api('shutdown');app.process.wait(timeout=45);app.log.close()
        # Keep this test classroom until the native window verification finishes.
        check.MARKER.write_bytes(check.MARKER_BYTES)
        return
    before=check.Snapshot()
    if action=='old':
        assert not before['registry'] and not ROOT.exists()
        ROOT.mkdir(parents=True)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\chain\Chain Classroom') as key:
            previous=winreg.QueryValueEx(key,'')[0]
        (ROOT/'previous-path.json').write_text(json.dumps(previous))
        exe=REPO/'Classroom/release/NornV2-0.1.2-windows-x64-setup.exe'
        command=subprocess.list2cmdline([str(exe),'/S'])+' /D='+str(check.INSTALL)
    elif action=='upgrade':
        assert before['registry']['DisplayVersion']=='0.1.2' and not before['processes']
        exe=REPO/'Classroom/release/NornV2-0.1.3-windows-x64-setup.exe'
        command=[str(exe),'/ARGS','--data',str(check.DATA)]
    elif action=='uninstall':
        assert not before['processes']
        exe=check.INSTALL/'uninstall.exe'
        command=subprocess.list2cmdline([str(exe),'/S'])+' _?='+str(check.INSTALL)
    else:raise ValueError(action)
    digest=check.Sha(exe);start=time.monotonic()
    p=subprocess.Popen(command,env={**os.environ,'PATH':os.environ['SystemRoot']+r'\System32'},creationflags=subprocess.CREATE_NO_WINDOW)
    code=p.wait(timeout=240);time.sleep(2)
    after=check.Snapshot()
    record={'action':action,'exit_code':code,'seconds':round(time.monotonic()-start,3),'installer_sha256':digest,'before':before,'after':after}
    (REPORT/'data'/f'{action}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8')
    assert code==0,record
    if action=='upgrade':
        assert after['registry']['DisplayVersion']=='0.1.3'
        assert after['registry']['InstallLocation']==before['registry']['InstallLocation']
        assert after['data_probe_preserved'] and all(f['matches'] for f in after['runtime_files'])
        assert any(f['file']=='norn-network.exe' for f in after['runtime_files'])
        assert any(f['file']=='Configure-Network.ps1' for f in after['runtime_files'])
        assert len(after['processes'])>=2
    if action=='uninstall':
        assert not after['registry'] and after['main_sha256'] is None and after['data_probe_preserved']
        previous=json.loads((ROOT/'previous-path.json').read_text())
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r'Software\chain\Chain Classroom') as key:
            winreg.SetValueEx(key,'',0,winreg.REG_SZ,previous)
    print(json.dumps({'action':action,'passed':True,'seconds':record['seconds']}))
if __name__=='__main__':Main()
