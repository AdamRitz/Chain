"""Build the GUI report only from saved evidence. Does not launch nodes or UI."""
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FIG = ROOT / "figures"

CASES = [
    ("G01", "欢迎页与七个功能入口", "通过", "教学中文界面可导航，教师端显示课堂管理，学生端隐藏教师入口。"),
    ("G02", "一键本地实验", "通过", "界面创建独立创世和三个真实节点，正常交易部署聊天室；3/3 链头一致。"),
    ("G03", "原生转账", "通过", "Alice 向 Bob 发 100；Alice 减 101、Bob 加 100，Alice 序号从 0 到 1。"),
    ("G04", "回执与区块跳转", "通过", "等待入块转为本机已上链；打开 #3，交易哈希、Merkle 根和序号一致。"),
    ("G05", "演示身份与昵称", "通过", "切换 Bob，余额正确；保存 Bob 同学昵称，后续消息使用该昵称。"),
    ("G06", "消息按钮与 Enter", "通过", "两种入口均提交真实交易；观察到等待入块与本机已上链。"),
    ("G07", "换行与文本显示", "通过", "Shift+Enter 保留换行；<script> 作为普通文本显示，未执行脚本。"),
    ("G08", "UTF-8 超限拦截", "通过", "171 个“链”为 513 字节；按钮禁用，Enter 不生成消息交易。"),
    ("G09", "房间管理", "通过", "创建房间 #2；关闭后按钮与 Enter 均不发送；恢复后可发言。"),
    ("G10", "合约成员权限", "通过", "未授权 Bob 的交易执行状态 2，无消息事件；授权后新消息成功。"),
    ("G11", "只读合约查询", "通过", "界面查到 3 条成功消息；最终证据查到 4 条，只读调用不发交易。"),
    ("G12", "索引重建", "通过", "点击重建后，历史消息、房间和规范链位置继续可读。"),
    ("G13", "停止状态", "通过/已修复", "修复旧缓存在线状态；新网关停止后 0/1、无连接、TPS 归零。"),
    ("G14", "恢复与实验引导", "通过/已修复", "恢复相同网络和链头、合约可用后，第 6 项显示已完成。"),
    ("G15", "邀请生成和下载", "通过", "界面下载 JSON 已实际解析，包含 IPv4 和带方括号 IPv6 入口。"),
    ("G16", "无效邀请反馈", "通过/已修复", "无效 JSON 不启动网络；错误现在显示在前景操作弹窗内。"),
    ("G17", "纯 IPv6 学生加入", "通过", "独立目录、独立钱包，唯一种子 [::1]:52411；连接详情标注 IPv6。"),
    ("G18", "教师发币与学生聊天", "通过", "教师界面按默认金额发一千万；学生收到并发言，教师看到同一交易 #15。"),
    ("G19", "跨节点账本核对", "通过", "教师三节点加学生一节点共四节点同头；双方四条成功消息的链上字段一致。"),
    ("G20", "源码与诊断导出", "通过", "源码弹窗可读；实际下载诊断包，不含 raw/private_key/seed/password/token 字段。"),
    ("G21", "消息详情状态隔离", "通过/已修复", "查看转账后进入聊天，流程图保持未选择；选中消息才显示消息位置。"),
    ("G22", "前端控制台", "通过", "教师端与学生端采集的 error/warn 列表均为空。"),
]

PENDING = [
    ("P01", "Windows 原生窗口完整交互与截图", "受阻：电脑锁屏；启动与可访问性树已返回，未执行桌面输入。"),
    ("P02", "安装向导、首次安装、卸载", "未执行：本次使用隔离便携运行目录；新安装包仅构建和载荷核对。"),
    ("P03", "900x680 最小窗口和高 DPI", "未验证：浏览器视口设置未生效，实际 innerWidth/Height 仍为 1280x720。"),
    ("P04", "校园网或公网跨机器 IPv6", "未执行：本次为同机 ::1 回环；不据此推断真实入站可达性。"),
    ("P05", "钱包密码、备份恢复的完整 UI 流程", "未自动化密码界面；合成钱包经 API 准备，相关后端流程由本轮 ClassroomNetwork 回归覆盖。"),
]

SHOTS = [
    ("01-welcome-browser.jpg", "欢迎页：本地实验、加入课堂和教师入口。"),
    ("02-overview-browser.jpg", "真实三节点实验就绪，聊天室已正常部署。"),
    ("03-wallet-transfer-browser.jpg", "原生转账后 Alice 余额与账户序号。"),
    ("04-block-browser.jpg", "原生转账所在区块 #3 的哈希、Merkle 根及交易。"),
    ("05-chat-byte-limit-browser.jpg", "513 字节中文消息超限，Enter 不发送；同时记录修复前流程图误继承转账状态。"),
    ("06-chat-confirmed-browser.jpg", "Alice 消息已上链及其真实位置。"),
    ("07-chat-two-identities-browser.jpg", "两种演示身份、多行文本与 HTML 原样显示。"),
    ("08-room-closed-browser.jpg", "关闭房间显示锁标记并阻止发送。"),
    ("09-stopped-browser.jpg", "修复前：顶部节点已停止，但概览仍错误显示缓存中的 3/3 在线。"),
    ("10-lessons-restart-browser.jpg", "教师端实验引导页面；第 6 项完整截图见图 21。"),
    ("11-member-denied-browser.jpg", "成员限制阻止未授权消息，界面显示合约执行回退。"),
    ("12-member-allowed-browser.jpg", "授权后 Bob 在新房间的消息成功上链。"),
    ("13-readonly-count-browser.jpg", "界面只读查询当时的消息计数 3，不收费。"),
    ("14-network-ipv6-browser.jpg", "教师端三个真实节点连接和一致链头。"),
    ("15-invalid-invite-browser.jpg", "修复前：无效 JSON 错误在弹窗背后，被背景遮罩模糊。"),
    ("16-invalid-invite-fixed-browser.jpg", "修复后：错误直接显示在加入课堂弹窗内。"),
    ("17-student-funded-browser.jpg", "独立学生钱包收到教师发放的一千万测试币。"),
    ("18-student-chat-browser.jpg", "独立学生端消息进入规范链 #15。"),
    ("19-student-ipv6-browser.jpg", "学生只有 [::1]:52411 一个种子，实际连接标记为 IPv6。"),
    ("20-stopped-fixed-browser.jpg", "修复后：停止的学生节点显示 0/1，连接列表清空。"),
    ("21-restart-lesson-fixed-browser.jpg", "修复后：同一课堂和链头恢复，第 6 项显示已完成。"),
    ("22-teacher-received-student-browser.jpg", "教师端看到同一学生消息及 #15，流程图依据真实消息。"),
    ("24-contract-source-browser.jpg", "随应用附带的 Solidity 源码弹窗。"),
    ("25-inspector-fixed-browser.jpg", "修复后：转账回执不会让未选择消息的流程图出现完成勾号。"),
]


def Main():
    ledger = json.loads((DATA / "ledger-checks.json").read_text(encoding="utf-8"))
    artifacts = json.loads((DATA / "artifacts.json").read_text(encoding="utf-8"))
    testxml = ElementTree.parse(DATA / "classroom-ctest.xml").getroot()
    assert testxml.attrib["failures"] == "0" and testxml.attrib["tests"] == "3"
    native_path = DATA / "native-results.json"
    native = json.loads(native_path.read_text(encoding="utf-8")) if native_path.exists() else {}
    native_cases = native.get("cases", [])
    shots = SHOTS + native.get("screenshots", [])
    pending = list(PENDING)
    complete = native.get("core_flow_complete", False)
    if complete:
        assert native_cases and all(row[2].startswith("通过") for row in native_cases)
        pending = [row for row in pending if row[0] != "P01"]
        pending = [(code, title, reason) if code not in {"P03", "P05"} else
                   (code, "高 DPI 与多显示器", "最小窗口与消息弹窗已通过；其他缩放比例和跨显示器组合未覆盖。") if code == "P03" and native.get("minimum_window_tested") else
                   (code, title, reason) if code == "P03" else
                   (code, "钱包备份恢复的完整 UI 流程", "用户手动创建测试钱包已通过；备份恢复仍只有后端回归覆盖。")
                   for code, title, reason in pending]
        if native.get("keyboard_limit"):
            pending.append(("P06", "Windows 原生 Enter 快捷键", native["keyboard_limit"]))
    (DATA/"test-matrix.json").write_text(json.dumps({"gui": CASES, "native": native_cases, "pending": pending}, ensure_ascii=False, indent=2), encoding="utf-8")
    intro = "浏览器图形界面已完成 22 项用例验证，发现并修复 4 处状态/反馈问题；前端 5 项检查和教学网关 3 项回归通过。Windows 原生窗口完整操作与截图仍因电脑锁屏受阻，不能认定整个桌面版验收完成。"
    if complete:
        intro = f"浏览器 22 项用例及 Windows 桌面版 {len(native_cases)} 项主要流程补测通过，发现的 4 处状态/反馈问题已修复。前端 5 项检查和教学网关 3 项回归通过，共保留 {len(shots)} 张实际界面截图。安装向导、校园网入站及额外 DPI 组合不在已通过结论内。"
    lines = ["# Chain 教学图形界面测试报告", "", "日期：2026-10-29（Asia/Shanghai）", "", intro, "",
        "## 环境、方法与证据口径", "", "- 代码基线：b484e511b89dab4309e8d6fdb9736ee516ac0b9d，加本报告对应的修复提交。",
        "- Windows 本机真实 C++ 节点，隔离教师和学生目录；教师 3 节点，学生 1 节点；未部署云端、未做 TPS 压测。",
        "- 浏览器操作使用随安装包发布的 React 静态前端，调用实际 C++ 网关和普通部署的 ClassroomChat 合约；不是静态原型或模拟消息。",
        f"- 浏览器原图 24 张，Windows 原生窗口补测原图 {len(native.get('screenshots', []))} 张。浏览器默认视口 innerWidth 1280、innerHeight 720，带滚动条截图为 1265x712。没有把锁屏画面当作应用截图。",
        "- 浏览器夹具的合成钱包由 API 准备；原生窗口补测由用户手动创建独立测试钱包。密码不进入测试报告。密码/备份恢复的后端正确性与完整 UI 测试分别列示。" if complete else "- 合成钱包由 API 准备并保持本机加密，界面测试没有输入真实用户密码。密码/备份恢复的后端正确性与完整 UI 测试分别列示。",
        "- 前半程验证初版，发现问题后构建前端与网关；学生端使用修复后的网关；截图说明明确标识修复前后。最终安装包包含全部修复。", "",
        "## 测试矩阵", "", "| 编号 | 测试内容 | 结果 | 实际观察 |", "|---|---|---|---|"]
    lines += ["| " + " | ".join(cell.replace("<", "&lt;").replace(">", "&gt;") for cell in row) + " |" for row in CASES]
    lines += ["", "补充回归：npm test 5/5；ClassroomIndex、ClassroomNetwork、ClassroomJoin 3/3，58.20 秒。原有 15 项全量回归在前次实施报告中通过，本轮未重跑无关的共识/账户测试。", ""]
    if native_cases:
        lines += ["## Windows 原生窗口补测", "", f"测试桌面程序 SHA256：`{native['desktop_sha256']}`。使用与当前安装包一致的桌面程序及 runtime 载荷，PATH 仅包含 System32，测试目录独立于浏览器实验。", "", "| 编号 | 测试内容 | 结果 | 实际观察 |", "|---|---|---|---|"]
        lines += ["| " + " | ".join(row) + " |" for row in native_cases]
        lines += ["", native.get("summary", ""), ""]
    lines += [
        "## 问题修复", "",
        "1. 消息流程图会复用转账回执的高度。现在仅从当前房间的消息集合取得选中交易的规范链位置；消息移出索引后不再沿用旧位置；第四步只在观察到多个本机节点同头时显示完成。",
        "2. 第六个实验的 done 固定为 false。现在只在当前界面会话内安全停止后，恢复同一个课堂、同一个链头且合约已校验时点亮；这不是最终性证明。",
        "3. Stop() 只清 running，留下在线节点和速率旧缓存。现在清除在线标志、连接、邻居数、当前 TPS 和合约可用标志，链高/链头保留为历史观测。新增真实网络回归断言。",
        "4. 加入课堂等弹窗失败时，错误显示在被模糊的后台页面。现在错误在当前弹窗内展示，便于本科生修正输入。",
        "另为聊天主区设置 min-height:0、固定输入栏和标题的收缩行为，并让消息详情独立滚动，减少长内容挤压输入栏。", "",
        "## 链上核对结果", "",
        f"- 最终四个节点均为高度 {ledger['height']}，链头 `{ledger['head']}`。",
        "- 教师与学生的创世指纹和合约地址一致；四条成功消息的交易、区块和事件字段一致。本机首次观察时间分别记录，不要求不同节点的墙钟时间相同。",
        f"- 独立学生交易 `{ledger['student_transaction']}` 位于区块 #{ledger['student_message_height']}，教师界面显示相同位置。",
        "- 未授权消息交易被区块收录，但 execution_status=2，没有 MessageSent 事件；加入区块与合约执行成功分开核对。",
        "- API 只读 messageCount 最终为 4；界面截图中的 3 对应独立学生发言之前的状态。",
        "- 原生转账 100 的发送者余额减少 101，接收者增加 100，额外 1 是本链基础费用。",
        "- 提交、入块、最近观测链头相同和不可逆最终性含义不同。本测试不能给出 TPS 上限、确认延迟分布或最终性证明。", "",
        "## 尚未完成与适用范围", "", "| 编号 | 项目 | 状态与原因 |", "|---|---|---|"]
    lines += ["| " + " | ".join(row) + " |" for row in pending]
    lines += ["", "浏览器视口覆盖尝试没有改变实际尺寸，已恢复默认值；未将该尝试计为最小窗口通过。复制配置按钮显示成功，但工具读到的剪贴板为空，因此只将实际下载并解析的 JSON 计为导出通过。", "",
        "此前锁屏时已停止桌面操作；用户解锁后继续完成原生窗口补测。钱包凭据由用户自行输入，未自动化认证对话框。" if complete else "桌面截图受阻依据 computer-use/docs/guidance.md 的要求：\"If the Windows desktop is locked, stop immediately and ask the user to unlock the desktop.\" 已请求解锁，未通过锁屏继续操作。", "",
        "## 可复核材料与安装包", "",
        "- `data/test-matrix.json`：用例、结果及待验证项。",
        "- `data/ledger-checks.json`、teacher-final.json、student-final.json：公开状态和四节点核对。",
        "- `data/invite-downloaded.json`、diagnostics-downloaded.json：从界面实际下载的公开文件。",
        "- `data/frontend-tests.txt`、classroom-ctest.xml、package-build.txt：本轮构建与回归记录。",
        "- `data/tested-files.json`、artifacts.json、screenshots.json：源文件、程序、安装包和原图校验值。",
        "- `data/database-cleanup.json`：停止测试进程后清理独立 RocksDB，保留参数、日志和索引证据。",
        "- `data/native-results.json`、native-after-*.json、native-processes.json：原生窗口用例、节点公开状态和退出核对；存在时纳入本报告。",
        "- `data/native-database-cleanup.json`：本次原生窗口测试的三个节点数据库清理记录。",
        "- `methods/CollectEvidence.py` 仅对已启动的指定本机夹具读取公开状态；`BuildReport.py` 仅从保存的证据重建报告，不启动节点。", "",
        "最新安装包仍为 0.1.0，使用 SHA256 区分本次修复构建：", "", f"`{artifacts['installer']['sha256']}`", "",
        "安装包未签名；构建成功和载荷一致不等于安装向导已验证。", "", "## 截图记录", ""]
    for index, (file, caption) in enumerate(shots, 1):
        lines += [f"### 图 {index:02d} - {caption}", "", f"![{caption}](figures/{file})", ""]
    (ROOT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    pdfmetrics.registerFont(TTFont("Chinese", "C:/Windows/Fonts/simsun.ttc", subfontIndex=0))
    size = landscape(A4)
    width = size[0] - 72
    style = ParagraphStyle("Body", fontName="Chinese", fontSize=11, leading=17, textColor=colors.HexColor("#254653"), wordWrap="CJK", spaceAfter=9)
    heading = ParagraphStyle("Title", parent=style, fontSize=23, leading=31, textColor=colors.HexColor("#156d5b"), spaceAfter=17)
    small = ParagraphStyle("Small", parent=style, fontSize=9, leading=13, spaceAfter=6)
    section = ParagraphStyle("Section", parent=style, fontSize=16, leading=23, spaceAfter=13)
    story = []
    def P(text, kind=style): return Paragraph(escape(text), kind)
    def PageTitle(title, subtitle="2026-10-29 | 本地真实节点 | 浏览器截图"):
        story.extend([P(title, heading), P(subtitle, small), Spacer(1, 8)])
    def Shot(file, caption):
        from PIL import Image as PILImage
        im = PILImage.open(FIG/file)
        ratio = min(width/im.width, 395/im.height)
        story.extend([Image(str(FIG/file), width=im.width*ratio, height=im.height*ratio), Spacer(1, 7), P(caption, small)])
    PageTitle("Chain 教学图形界面测试报告", "验收状态：浏览器及 Windows 桌面主要流程验证通过" if complete else "验收状态：浏览器界面验证完成，Windows 原生窗口待解锁补测")
    story.extend([P(intro), Spacer(1, 7), P("验证结果", section)])
    for t in [f"22 项浏览器用例、{len(native_cases)} 项桌面用例；前端 5/5、教学网关 3/3。", "4 个真实节点最终同头，成功消息 4 条，纯 IPv6 学生入口已验证。", "发现并修复：消息流程图状态串用、重启引导不完成、停止后在线缓存、弹窗错误不可见。", f"{len(shots)} 张实际界面原图、公开状态、测试日志和 SHA256 随 Markdown 报告保存。", "Windows 桌面启动、转账、聊天、区块、节点停止恢复及正常关闭重启已补测。" if complete else "Windows 电脑锁屏，原生窗口输入、截图、安装向导与最小窗口测试尚未完成。"]:
        story.append(P("- " + t))
    story.append(P("范围：本地正确性与使用流程；不涉及十台服务器部署、TPS 压测或校园网 IPv6 可达性结论。", small))
    story.append(PageBreak())
    for start, end in [(0,11),(11,22)]:
        PageTitle(f"测试矩阵 {start+1:02d}-{end:02d}")
        rows = [[P("编号", small), P("测试内容", small), P("结果", small), P("实际观察", small)]]
        rows += [[P(c, small) for c in row] for row in CASES[start:end]]
        tab = Table(rows, colWidths=[37,132,74,width-243], repeatRows=1)
        tab.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#e5f2ec")),("VALIGN",(0,0),(-1,-1),"TOP"),("GRID",(0,0),(-1,-1),.35,colors.HexColor("#d5e3e0")),("LEFTPADDING",(0,0),(-1,-1),7),("RIGHTPADDING",(0,0),(-1,-1),7),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),4)]))
        story.extend([tab,PageBreak()])
    pages = [
        ("真实三节点实验", "02-overview-browser.jpg", "同一创世和真实节点，正常交易部署聊天室。页面 TPS 仅为教学观测值。"),
        ("原生转账与账户序号", "03-wallet-transfer-browser.jpg", "转账 100 后 Alice 减少 101，Bob 增加 100；区块 #3 和回执位置已核对。"),
        ("独立学生与教师的聊天同步", "22-teacher-received-student-browser.jpg", "独立学生消息在教师端显示同一交易与区块 #15；合约执行回退的消息单独展示。"),
        ("纯 IPv6 加入课堂", "19-student-ipv6-browser.jpg", "学生的唯一初始入口为 [::1]:52411；实际连接返回 ipv6=true。仅证明同机回环可用。"),
        ("教师发币到独立学生钱包", "17-student-funded-browser.jpg", "学生从零余额收到默认一千万最小单位测试币，随后可支付聊天室调用费用。"),
        ("修复：错误提示进入前景弹窗", "16-invalid-invite-fixed-browser.jpg", "无效 JSON 的错误现在可直接看到；修复前被背景遮罩模糊，原图 15 保留对照。"),
        ("修复：停止状态与连接缓存", "20-stopped-fixed-browser.jpg", "停止后显示 0/1 和未连接，连接列表清空；原图 09 记录修复前仍显示 3/3 在线。"),
        ("修复：重启教学引导", "21-restart-lesson-fixed-browser.jpg", "当前界面会话内，安全停止后恢复同一课堂、同一链头且合约可用，第 6 项显示已完成。"),
        ("修复：消息流程图状态隔离", "25-inspector-fixed-browser.jpg", "查看原生转账再切换聊天室，未选择消息时流程图显示步骤 1-4，避免误认为消息已经完成。"),
    ]
    for title, file, caption in pages:
        PageTitle(title)
        Shot(file, caption)
        story.append(PageBreak())
    if native_cases:
        PageTitle("Windows 桌面补测矩阵", "同一发布程序 | 独立课堂 | 用户手动创建测试钱包")
        rows = [[P("编号", small), P("测试内容", small), P("结果", small), P("实际观察", small)]]
        rows += [[P(c, small) for c in row] for row in native_cases]
        tab = Table(rows, colWidths=[37,132,74,width-243], repeatRows=1)
        tab.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#e5f2ec")),("VALIGN",(0,0),(-1,-1),"TOP"),("GRID",(0,0),(-1,-1),.35,colors.HexColor("#d5e3e0")),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
        story.extend([tab, PageBreak()])
        for file, caption in native.get("pdf_screenshots", []):
            PageTitle("Windows 桌面窗口实测", "原生 Tauri/WebView2 窗口截图")
            Shot(file, caption)
            story.append(PageBreak())
    PageTitle("尚未验证与材料索引", "保留安装向导、跨网络可达性和额外环境组合的验证边界")
    for code,title,reason in pending:
        story.append(P(f"{code}  {title}：{reason}", small))
    story.extend([Spacer(1,10),P("证据定位",section),P("完整截图与逐项说明见同目录 REPORT.md；机器可读结果、公开链状态、构建日志和文件校验位于 data/。测试结束后停止所属进程，清理独立节点 RocksDB，保留日志、配置、索引与截图。",small),P("安装包 SHA256: "+artifacts["installer"]["sha256"],small)])
    def Footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#d5e3e0"));canvas.line(36,28,size[0]-36,28)
        canvas.setFont("Chinese",8);canvas.setFillColor(colors.HexColor("#687f87"))
        canvas.drawString(36,16,"Chain Classroom | GUI 测试 | 实际界面截图")
        canvas.drawRightString(size[0]-36,16,str(doc.page))
        canvas.restoreState()
    SimpleDocTemplate(str(ROOT/"GUI-TEST-REPORT.pdf"), pagesize=size, leftMargin=36,rightMargin=36,topMargin=30,bottomMargin=38,title="Chain 教学图形界面测试报告",author="Chain Classroom").build(story,onFirstPage=Footer,onLaterPages=Footer)
    print(json.dumps({"markdown":str(ROOT/"REPORT.md"),"pdf":str(ROOT/"GUI-TEST-REPORT.pdf"),"screenshots":len(shots)},ensure_ascii=True))


if __name__ == "__main__":
    Main()
