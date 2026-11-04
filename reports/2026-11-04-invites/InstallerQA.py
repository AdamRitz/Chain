"""Current-user NSIS registration tests. Refuses an existing registered install/association."""
import argparse,importlib.util,json,winreg
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('installer_qa',ROOT/'reports/2026-11-04-installer/RunQA.py')
qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)
qa.records=[]
EXT=r'Software\Classes\.norninvite';CLASS=r'Software\Classes\NornV2Invite'

def read(path,name=''):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,path) as key:return winreg.QueryValueEx(key,name)[0]
    except FileNotFoundError:return None
def exists(path):
    try:winreg.OpenKey(winreg.HKEY_CURRENT_USER,path).Close();return True
    except FileNotFoundError:return False
def set_handler(value):
    assert value in ('NornInviteQAPrevious','NornInviteQASelected')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,EXT) as key:winreg.SetValueEx(key,'',0,winreg.REG_SZ,value)
def clear_test_handler():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,EXT) as key:
        children,count,_=winreg.QueryInfoKey(key)
        assert children==0 and count==1 and winreg.EnumValue(key,0)[1] in ('NornInviteQAPrevious','NornInviteQASelected')
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER,EXT)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--work',type=Path,default=ROOT/'build/invite-installer-20261104-04')
    qa.WORK=parser.parse_args().work.resolve()
    assert qa.WORK.is_relative_to((ROOT/'build').resolve())
    assert qa.registry(qa.UNINSTALL) is None and not exists(EXT) and not exists(CLASS)
    assert not qa.WORK.exists();qa.WORK.mkdir()
    remembered=qa.registry(qa.REMEMBERED);before_data=qa.data_snapshot();result={'passed':False,'runs':qa.records}
    installed=qa.WORK/'installed'
    standard=ROOT/'Classroom/release/NornV2-0.3.0-windows-x64-setup.exe'
    offline=ROOT/'Classroom/release/NornV2-0.3.0-windows-x64-offline-setup.exe'
    def install(exe):
        qa.run(exe,'/P /NS /D='+str(installed));manifest=qa.check_install(installed)
        assert read(EXT)=='NornV2Invite'
        assert read(CLASS+r'\shell\open\command')=='"'+str(installed/'chain-classroom.exe')+'" "%1"'
        return manifest
    try:
        manifest=install(standard)
        for entry in manifest['files']:
            if entry['name'] in ('boost.exe','chain_classroom.exe'):
                assert entry['sha256']==qa.sha(ROOT/'build/classroom-20261028'/entry['name'])
        install(standard)
        assert read(EXT,'NornV2Invite_backup')==''
        qa.uninstall(installed);assert not exists(EXT) and not exists(CLASS)
        result['same_version_reinstall_and_clean_uninstall']=True

        set_handler('NornInviteQAPrevious');install(standard)
        assert read(EXT,'NornV2Invite_backup')=='NornInviteQAPrevious'
        qa.uninstall(installed);assert read(EXT)=='NornInviteQAPrevious' and not exists(CLASS)
        clear_test_handler();result['previous_handler_restored']=True

        install(standard);set_handler('NornInviteQASelected')
        qa.uninstall(installed);assert read(EXT)=='NornInviteQASelected' and not exists(CLASS)
        clear_test_handler();result['later_user_handler_preserved']=True

        offline_manifest=install(offline);assert offline_manifest==manifest
        qa.uninstall(installed);assert not exists(EXT) and not exists(CLASS)
        result.update(offline_payload_matches=True,manifest_files=len(manifest['files']),passed=True)
    finally:
        if qa.registry(qa.UNINSTALL) is not None and (installed/'uninstall.exe').exists():qa.uninstall(installed)
        if qa.registry(qa.UNINSTALL) is None:qa.restore_registry(qa.REMEMBERED,remembered)
        result['user_data_unchanged']=qa.data_snapshot()==before_data
        result['remembered_installation_restored']=qa.registry(qa.REMEMBERED)==remembered
        result['packages']=[{'name':p.name,'bytes':p.stat().st_size,'sha256':qa.sha(p)} for p in (standard,offline)]
        Path(__file__).with_name('installer.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert result['passed'] and result['user_data_unchanged'] and result['remembered_installation_restored']
