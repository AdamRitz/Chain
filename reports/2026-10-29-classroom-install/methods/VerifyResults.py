"""Validate saved integration evidence; this script does not install or delete anything."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]


def Read(name):
    return json.loads((ROOT / 'data' / (name + '.json')).read_text('utf-8-sig'))


def Processes(snapshot):
    return sorted((p['pid'], p['create_time'], p['exe']) for p in snapshot['processes'])


artifact = Read('artifact')
installer = REPO / 'Classroom/release/ChainClassroom-0.1.1-windows-x64-setup.exe'
assert hashlib.sha256(installer.read_bytes()).hexdigest() == artifact['installerSHA256']
main_bytes = (REPO / 'Classroom/src-tauri/target/release/chain-classroom.exe').read_bytes()
# Tauri 2.12.4 patches the bundle marker for NSIS, then restores the build file:
# https://github.com/tauri-apps/tauri/blob/tauri-cli-v2.12.4/crates/tauri-bundler/src/bundle.rs
# Reproduce only that documented byte substitution in memory to verify the payload.
assert b'__TAURI_BUNDLE_TYPE_VAR_UNK' in main_bytes
main_hash = hashlib.sha256(main_bytes.replace(b'__TAURI_BUNDLE_TYPE_VAR_UNK', b'__TAURI_BUNDLE_TYPE_VAR_NSS', 1)).hexdigest()
cases = []
for label, description in [
    ('08-upgrade-final', '0.1.0 升级到最终 0.1.1，保留数据'),
    ('09-repeat-final-auto-launch', '重复安装，自动启动'),
    ('13-final-clean-install', '无已安装程序时一键安装，自动启动'),
]:
    record = Read(label)
    assert record['exit_code'] == 0 and record['installer_sha256'] == artifact['installerSHA256']
    after = record['after']
    assert after['registry']['DisplayVersion'] == '0.1.1'
    assert after['main_sha256'] == main_hash and after['data_probe_preserved']
    assert len(after['runtime_files']) == 17 and all(f['matches'] for f in after['runtime_files'])
    assert after['start_menu_shortcut']
    if 'auto-launch' in label or 'clean-install' in label:
        assert len(after['processes']) == 2
    else:
        assert record['before']['registry']['DisplayVersion'] == '0.1.0'
        assert not after['processes']  # /NOLAUNCH was honored.
    cases.append({'case': description, 'result': 'PASS', 'seconds': record['elapsed_seconds'], 'evidence': label})
for label in ('10-final-busy-install', '11-final-busy-uninstall'):
    record = Read(label)
    assert record['exit_code'] == 2
    assert Processes(record['before']) == Processes(record['after'])
    assert len(record['after']['processes']) == 2
    assert record['before']['main_sha256'] == record['after']['main_sha256']
    cases.append({'case': label, 'result': 'PASS', 'evidence': label})
for label in ('12-final-uninstall', '18-cleanup-uninstall'):
    record = Read(label)
    after = record['after']
    assert record['exit_code'] == 0 and not after['registry']
    assert not after['processes'] and after['main_sha256'] is None
    assert not after['start_menu_shortcut'] and after['data_probe_preserved']
cases.append({'case': '正常卸载及数据保留', 'result': 'PASS', 'evidence': '18-cleanup-uninstall'})
shortcuts = Read('final-shortcuts')
assert len(shortcuts) == 2 and all(item['targetExists'] for item in shortcuts)
assert all(item['target'].endswith('chain-classroom.exe') and not item['arguments'] for item in shortcuts)
assert not Read('15-stopped-before-launch')['after']['processes']
assert len(Read('16-registered-launch-observation')['after']['processes']) == 2
cases.append({'case': '桌面/开始菜单快捷方式与注册入口重开', 'result': 'PASS', 'evidence': 'final-shortcuts / 15 / 16'})
wizard = Read('17-wizard-cancel')
assert wizard['exit_code'] == 1 and wizard['before'] == wizard['after']
assert '下一步' in (ROOT / 'data/wizard-ax.txt').read_text('utf-8')
cases.append({'case': '/WIZARD 完整向导与取消', 'result': 'PASS', 'evidence': '17-wizard-cancel'})
assert '0.1.1' in (ROOT / 'data/final-window-ax.txt').read_text('utf-8')
assert '# pass 5' in (ROOT / 'data/frontend-tests.txt').read_text('utf-8-sig')
cases.append({'case': '前端回归', 'result': 'PASS', 'passed': 5, 'total': 5})
images = [{'file': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted((ROOT / 'figures').glob('*.jpg'))]
result = {'version': '0.1.1', 'installer_sha256': artifact['installerSHA256'], 'expected_installed_exe_sha256': main_hash, 'cases': cases, 'screenshots': images,
    'limits': ['WebView2 already installed; missing-runtime installation not executed.',
               'Windows 11 only; packaged host virtualizes LocalAppData.',
               'Preservation probe is non-secret test data; this run did not create or unlock wallets.',
               'Cleanup deletion of residual files blocked by policy; see cleanup.json.']}
(ROOT / 'data/validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
print(json.dumps(result, ensure_ascii=False, indent=2))
