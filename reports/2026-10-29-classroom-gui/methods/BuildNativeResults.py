"""Combine saved desktop observations and independently collected public node evidence."""
import hashlib
import json
from pathlib import Path
from PIL import Image

REPORT = Path(__file__).resolve().parents[1]
REPO = REPORT.parents[1]
DATA = REPORT / "data"
FIG = REPORT / "figures"


def Read(name):
    return json.loads((DATA/name).read_text(encoding="utf-8"))


def Main():
    transfer = Read("native-after-transfer.json")
    settled = Read("native-after-chat-settled.json")
    restart = Read("native-after-restart.json")
    reopened = Read("native-after-desktop-relaunch.json")
    processes = Read("native-processes.json")
    alice, bob = transfer["profile"]["genesis"]["accounts"][:2]
    assert transfer["accounts"][alice["public_key"]]["balance"] == alice["balance"]-101
    assert transfer["accounts"][alice["public_key"]]["nonce"] == 1
    assert transfer["accounts"][bob["public_key"]]["balance"] == bob["balance"]+100
    messages = [e for e in settled["events"] if e["type"] == "message"]
    assert len(messages) == 2 and {e["height"] for e in messages} == {4, 5}
    for value in (settled, restart, reopened):
        assert len(value["nodes"]) == 3
        assert len({n["head"] for n in value["nodes"]}) == 1
        assert value["nodes"][0]["height"] == 5
        assert value["nodes"][0]["head"] == settled["nodes"][0]["head"]
        assert value["accounts"] == settled["accounts"] and value["events"] == settled["events"]
    assert processes["runtime_matches_release"]
    assert processes["stop_button"]["all_three_node_processes_exited"]
    assert processes["normal_close"]["gui_gateway_three_nodes_exited"]
    assert processes["final_close"]["gui_gateway_three_nodes_exited"]
    lessons = (DATA/"native-lessons-ax.txt").read_text(encoding="utf-8")
    assert all(any(f"实验 {i:02d} " in line and "已完成" in line for line in lessons.splitlines()) for i in range(1, 7))
    locked = (DATA/"native-reopened-chat-ax.txt").read_text(encoding="utf-8")
    assert "先解锁钱包，再发送消息" in locked and "(disabled) 发送消息" in locked
    assert Image.open(FIG/"37-native-small-message-detail.jpg").size == (902, 712)
    assert Image.open(FIG/"35-native-maximized.jpg").size == (2560, 1392)
    assert Image.open(FIG/"39-native-reopened.jpg").size == (1382, 932)
    qa = REPO/"build/classroom-native-gui-20261029-02/bundle"
    published = REPO/"Classroom/src-tauri/resources"
    runtime_files = []
    for path in (qa/"runtime").rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(qa/"runtime")
        assert path.read_bytes() == (published/relative).read_bytes()
        runtime_files.append({"path": relative.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    desktop_hash = hashlib.sha256((qa/"ChainClassroom.exe").read_bytes()).hexdigest()
    assert desktop_hash == Read("artifacts.json")["desktop_sha256"]
    (DATA/"native-runtime-files.json").write_text(json.dumps(runtime_files, indent=2), encoding="utf-8")
    cases = [
        ("N01", "真实桌面启动与钱包", "通过（人工配合）", "与安装包相同程序、System32 PATH 启动；用户手动创建独立测试钱包。"),
        ("N02", "三节点课堂与合约部署", "通过", "桌面创建课堂，3/3 节点同头，聊天室通过普通交易部署。"),
        ("N03", "桌面原生转账", "通过", "界面向 Bob 发送 100，Alice 减 101、Bob 加 100，序号为 1。"),
        ("N04", "桌面中文聊天", "通过", "按钮发送两条中文消息，分别进入 #4、#5；未将原生 Enter 自动化计为通过。"),
        ("N05", "消息、区块和执行回执", "通过", "从消息打开 #4，再打开合约交易，显示执行成功、序号 2 和 Gas。"),
        ("N06", "真实节点诊断", "通过", "三个本地进程、真实邻居与同一链头，IPv6 回环诊断可读。"),
        ("N07", "停止与恢复", "通过", "停止后 0/3 且三个节点退出；恢复后余额、事件、链头不变。"),
        ("N08", "窗口缩放与窄屏详情", "通过", "最大化 2560x1392；最小外框 902x712，消息详情弹窗可操作。"),
        ("N09", "六项实验引导", "通过", "按真实操作点亮六项实验，停止恢复后的第 6 项显示已完成。"),
        ("N10", "窗口正常关闭", "通过", "两次点击窗口关闭，桌面程序、网关和三个节点均退出，无所属残留。"),
        ("N11", "重开与锁定只读", "通过", "自动恢复同一课堂；钱包锁定、发送禁用，原有两条聊天和账本不变。"),
    ]
    shots = [
        ("26-native-welcome.jpg", "Windows 原生窗口欢迎页。"),
        ("27-native-overview.jpg", "桌面创建的独立三节点课堂，3/3 节点观测链头一致。"),
        ("28-native-transfer.jpg", "桌面原生转账后余额与账户序号。"),
        ("29-native-chat.jpg", "桌面按钮发送的两条真实消息及其规范链位置。"),
        ("30-native-block.jpg", "从桌面消息打开区块 #4；下方详情可以继续滚动。"),
        ("31-native-block-detail.jpg", "滚动后完整区块详情、Merkle 根及序号 2 的合约交易。"),
        ("32-native-receipt.jpg", "桌面合约交易回执，执行成功，区块 #4、序号 2。"),
        ("33-native-network.jpg", "桌面真实节点连接、同头观测及 IPv6 回环诊断。"),
        ("34-native-stopped.jpg", "桌面停止后 0/3，节点显示未连接，连接列表清空。"),
        ("35-native-maximized.jpg", "2560x1392 最大化布局；画面上方机器人为桌面悬浮层。"),
        ("36-native-minimum-window.jpg", "902x712 最小窗口外框，对应配置的 900x680 内容区。"),
        ("37-native-small-message-detail.jpg", "窄窗口消息详情自动改为弹窗，哈希换行、状态和跳转按钮可读。"),
        ("38-native-lessons.jpg", "六项实验均已完成，图中显示停止恢复后的第 6 项。"),
        ("39-native-reopened.jpg", "正常退出后重新打开，钱包保持锁定，课堂自动恢复。"),
        ("40-native-reopened-locked-chat.jpg", "重开后保留两条消息，发送按钮禁用，提示先解锁钱包。"),
    ]
    for file, _ in shots:
        assert Image.open(FIG/file).format == "JPEG"
    result = {"core_flow_complete": True, "minimum_window_tested": True, "desktop_sha256": desktop_hash,
              "cases": cases, "screenshots": shots, "pdf_screenshots": [s for s in shots if s[0].startswith(("29-", "32-", "34-", "37-", "38-", "40-"))],
              "window_sizes": {"default_outer": [1382, 932], "maximized_outer": [2560, 1392], "minimum_outer": [902, 712], "configured_minimum_content": [900, 680]},
              "summary": "原生桌面与浏览器为不同的独立课堂。桌面课堂最终高度 5，含两条成功消息；节点停止恢复和桌面重开后的公开账户、事件及链头完全一致。早期采样保留索引追赶前的观测，最终对照使用 settled 快照。测试助手没有代填或导出钱包密码，报告和采样记录不保存密码；钱包由用户在本机手动创建。",
              "keyboard_limit": "原生 Enter 自动化未取得可靠焦点及可验证提交，不计为通过；原生消息通过按钮验证，浏览器 Enter 已通过。"}
    (DATA/"native-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    screenshots = [{"file": p.name, "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(FIG.glob("*.jpg"))]
    (DATA/"screenshots.json").write_text(json.dumps({"source": "24 actual in-app browser screenshots and 15 native Windows window screenshots", "files": screenshots}, indent=2), encoding="utf-8")
    print(json.dumps({"native_cases": len(cases), "native_screenshots": len(shots), "total_screenshots": len(screenshots)}))


if __name__ == "__main__":
    Main()
