"""Actual released NSIS binaries, isolated paths, no wallet or chain creation.

Requires WebView2 already installed and no registered production installation.
Restores the remembered installation registry value after normal uninstallation.
Run on Windows with Python + psutil; installers must have been built first.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import winreg

import psutil

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "build" / "installer-20261104-qa"
REPORT = Path(__file__).with_name("validation.json")
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Chain Classroom"
REMEMBERED = r"Software\chain\Chain Classroom"
DATA = Path(os.environ["LOCALAPPDATA"]) / "edu.chain.classroom"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registry(path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            children, count, _ = winreg.QueryInfoKey(key)
            assert children == 0, "Unexpected nested installation registry data"
            return [winreg.EnumValue(key, index) for index in range(count)]
    except FileNotFoundError:
        return None


def restore_registry(path, original):
    assert path in (UNINSTALL, REMEMBERED)
    current = registry(path)
    if current is not None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            for name, _, _ in current:
                winreg.DeleteValue(key, name)
        if original is None:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    if original is not None:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            for name, value, kind in original:
                winreg.SetValueEx(key, name, 0, kind, value)
    assert registry(path) == original


def data_snapshot():
    return {str(p.relative_to(DATA)): sha(p) for p in DATA.rglob("*") if p.is_file()}


def run(exe, args, expected=0):
    start = time.perf_counter()
    result = subprocess.run('"' + str(exe) + '" ' + args, timeout=180)
    record = {"exe": exe.name, "arguments": args, "exit_code": result.returncode,
              "seconds": round(time.perf_counter() - start, 3)}
    records.append(record)
    print(json.dumps(record, ensure_ascii=False), flush=True)
    assert result.returncode == expected, record


def check_install(path):
    assert path.is_relative_to(WORK) and not path.is_symlink()
    manifest = json.loads((path / "runtime/manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        assert sha(path / "runtime" / entry["name"]) == entry["sha256"], entry["name"]
    assert (path / "chain-classroom.exe").is_file()
    assert (path / "uninstall.exe").is_file()
    installed = dict((name, value) for name, value, _ in registry(UNINSTALL))
    assert installed["InstallLocation"].strip('"') == str(path)
    return manifest


def uninstall(path):
    assert path.is_relative_to(WORK) and path.resolve().is_relative_to(WORK.resolve())
    for process in psutil.process_iter(["exe"]):
        exe = process.info["exe"]
        assert not exe or not Path(exe).is_relative_to(path), "Test executable still running"
    # In-place uninstall waits for completion, preserving app data by default.
    run(path / "uninstall.exe", '/P _?=' + str(path))
    assert not (path / "chain-classroom.exe").exists()
    assert registry(UNINSTALL) is None


if __name__ == "__main__":
    assert registry(UNINSTALL) is None, "Do not replace a registered production installation"
    assert not WORK.exists(), "Use a fresh isolated QA directory"
    remembered = registry(REMEMBERED)
    before_data = data_snapshot()
    WORK.mkdir(parents=True)
    records = []
    result = {"webview2_preinstalled": True, "runs": records, "passed": False}
    old = ROOT / "Classroom/release/NornV2-0.2.0-windows-x64-setup.exe"
    standard = ROOT / "Classroom/release/NornV2-0.2.1-windows-x64-setup.exe"
    offline = ROOT / "Classroom/release/NornV2-0.2.1-windows-x64-offline-setup.exe"
    a, b, c, d = [WORK / name for name in ("old-and-upgrade", "中文 自选目录", "standard", "offline")]
    try:
        run(old, '/P /NS /D=' + str(a))
        check_install(a)
        run(standard, '/P /NS /D=' + str(a))
        check_install(a)

        # Exact old-path process detection must work even with a different /D.
        stop = WORK / "stop-gateway"
        gateway = subprocess.Popen([str(a / "runtime/chain_classroom.exe"),
            "--data", str(WORK / "gateway-data"), "--node", str(a / "runtime/boost.exe"),
            "--web", str(a / "runtime/web"), "--contract", str(a / "runtime/ClassroomChat.compiled.json"),
            "--stop-file", str(stop)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            assert gateway.stdout.readline().startswith(b'{'), "Gateway did not start"
            run(standard, '/S /NS /D=' + str(b), expected=2)
            assert (a / "chain-classroom.exe").exists() and not b.exists()
        finally:
            stop.write_text("stop\n")
            gateway.wait(timeout=30)

        run(standard, '/P /NS /D=' + str(b))
        check_install(b)
        assert not (a / "chain-classroom.exe").exists(), "Relocation left old program"
        uninstall(b)

        run(standard, '/P /NS /D=' + str(c))
        standard_manifest = check_install(c)
        uninstall(c)

        run(offline, '/P /NS /D=' + str(d))
        offline_manifest = check_install(d)
        assert standard_manifest == offline_manifest, "Variants bundle different runtime payloads"
        uninstall(d)
        result.update(passed=True, manifest_files=len(standard_manifest["files"]),
                      variants_identical=True, old_process_blocked=True, relocation_passed=True)
    finally:
        # Only restore registry after all test installations were removed.
        if registry(UNINSTALL) is None:
            restore_registry(REMEMBERED, remembered)
            result["remembered_installation_restored"] = True
        result["user_data_unchanged"] = data_snapshot() == before_data
        result["packages"] = [{"name": p.name, "bytes": p.stat().st_size, "sha256": sha(p)} for p in (old, standard, offline)]
        REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert result["user_data_unchanged"] and result["remembered_installation_restored"]
