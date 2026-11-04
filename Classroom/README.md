# NornV2 学术聊天室

用于研究成员之间建立链上聊天室的 Windows 桌面应用。创建链的人负责初始化链和部署公共聊天室；所有成员使用同一套界面和合约接口，没有教师、学生或审批角色。每台电脑运行一个真实 C++ 节点，支持 IPv4/IPv6。

## 安装

下载 [NornV2 0.2.1 Windows x64 安装包](https://github.com/AdamRitz/Chain/releases/tag/v0.2.1)。通常选择 `NornV2-0.2.1-windows-x64-setup.exe`，双击后选择程序目录并继续安装。关闭正在运行的应用后再安装更新。支持 Windows 10 1903 及以上和 Windows 11 x64，包含 C++ 运行依赖，不需要额外安装编译器、Node.js 或 Python。

标准包复用电脑上已有的 WebView2；缺少时通过内置的微软引导程序联网下载安装。无网环境选择带 `-offline-setup.exe` 的完整离线包，它包含同一应用和完整 WebView2 安装程序，体积较大。两种包检测到已有 WebView2 时均跳过运行库安装。

安装保留应用标识 `edu.chain.classroom`、既有程序目录 `%LOCALAPPDATA%/Chain Classroom` 和数据目录 `%LOCALAPPDATA%/edu.chain.classroom`，避免重建账户或丢失链数据。显示名称改为“NornV2 学术聊天室”。安装包未做发布者代码签名。

默认双击先显示安装目录，可浏览或输入 D 盘等有写入权限的目录，之后显示进度，成功后自动打开并创建快捷方式。更换程序目录时清理旧程序，账户和链数据仍保留在原来的独立数据目录，不会自动迁移至新盘。高级参数 `/WIZARD` 打开完整向导，`/NOLAUNCH` 安装后不启动，`/P` 显示进度而不启动，`/S` 静默安装，附加 `/R` 才自动启动。无人值守可用最后一个参数 `/D=D:\Apps\NornV2` 指定目录（NSIS 要求不加引号）。普通卸载默认保留账户和链数据。

## 创建链并邀请成员

1. 点击“创建链与聊天室”，填写链名称和显示昵称。首次使用设置本机账户密码。
2. 程序启动一个节点，并通过普通链上交易部署公共聊天室。默认直接打开聊天室。
3. 在“节点与连接”点击“允许所有人连接”，按需确认 Windows 管理员授权。
4. 打开“邀请加入”，从本机候选地址中选择对方可访问的网卡地址，或填写 `IPv4:端口`、`[IPv6]:端口`。生成并分享邀请 JSON。

候选地址只是本机地址列表，不保证对方可达；回环地址只适合同机测试，链路本地 IPv6 不作为跨网段邀请入口。连接检测方向是本机到填写的节点；对方是否能连入本机，需要对方电脑检测。

## 加入链与聊天室

1. 点击“加入链与聊天室”，粘贴或导入任意已连接成员提供的邀请，填写自己的昵称和本机账户密码。
2. 本机启动一个节点，同步链、聊天室合约和消息。重复导入当前链的邀请会复用现有连接和账本。
3. 加入者使用自己的独立密钥，邀请不包含私钥。昵称用于显示，身份以钱包地址为准。
4. 消息和创建讨论分组都属于正常合约交易，需要链上余额支付费用。余额不足时界面给出收款地址，任意有余额的成员都可以在“我的账户”执行普通转账补充费用，无发币审批接口。

创建链时，创世配置给创建者的当前账户分配 `1000000000000` 个最小单位，再加入一个零余额随机公钥区分独立链。新生成的其他账户默认零余额。部署与消息费用按现有节点规则扣除；普通转账不增加总供应量。当前原生转账协议仍不绑定链标识，复用同一账户参与独立链时存在此前审查记录的跨链重放问题，本次产品角色调整未修改该协议。

## 平等成员的聊天室

- 新聊天室合约没有 owner、成员白名单、教师审批或关闭其他成员发言的接口。所有有足够费用余额的成员均可发送消息和创建讨论分组。
- 所有已连接成员都可以生成邀请；创建者离线后，已互连的其他成员仍可同步和交流。首次加入至少需要一个仍可达、持有该链记录的成员节点。
- 每条消息对应普通 Solidity 合约交易和事件，公开记录在当前链上。界面区分等待入块、执行失败和本机已上链；本机已上链不是不可逆最终确认。
- 已执行失败的交易不能通过重发同一签名字节重新执行。聊天页的“重新发送”会创建新交易；传输中未入块的交易才使用“重试原交易”。
- “链上记录”和“链状态”供查阅，不再提供教学引导、课堂管理或演示钱包切换。新建账户只生成一个身份；既有多个密钥以普通账户名称保留。

## 连接配置

默认 `Profile=Any + RemoteAddress=Any`，允许所有来源连接当前安装的 `runtime/boost.exe`，适用于专用、公用和域网络，兼容 IPv4/IPv6 和动态节点端口。仅配置本应用节点的 TCP 入站规则，本机管理网关继续只监听回环地址，不更改防火墙开关。

相同规则不会重复请求授权；撤销和卸载只清理当前安装拥有的规则。独立脚本位于安装目录 `runtime/Configure-Network.ps1`，与界面使用相同工具：

```powershell
.\Configure-Network.ps1 -Action status
.\Configure-Network.ps1 -Action configure
.\Configure-Network.ps1 -Action remove
```

防火墙规则不能代替路由器端口映射或校园/机构网络的入站许可。

## 已有数据兼容

恢复既有链时，使用原主节点端口、node1 账本、钱包和合约，其他节点目录保留。旧版加密文件中的三个密钥不删除，但没有特殊管理角色名称或客户端管理接口。

支持原 v1 合约与邀请，以及新的 v2 平等成员合约与邀请。已部署的 v1 合约字节码和历史记录不会被客户端改写；其既有链上规则继续生效。新创建的链均部署 v2 合约。邀请被绑定到当前连接，切换连接后不会继续下载另一条链的邀请；无效邀请在停止节点之前完成校验。

新 v2 邀请需要 0.2.0 或更新客户端。兼容用的原合约编译结果保存在 `Contracts/ClassroomChat.legacy-v1.compiled.json`，不再作为新链部署目标。

## 开发构建

内部 `Classroom/` 目录和既有二进制名称保留，以兼容安装标识和构建脚本；产品界面使用学术聊天室名称。节点共识和账户数据库 schema 7 保持现有无学习模型基线，FISCO 测试方法不受影响。

需要现有 C++20/CMake/MinGW 节点依赖、SQLite3、Node.js 22.12+、Rust 1.90.0 MSVC 工具链和 Windows SDK/MSVC Build Tools。

```powershell
cd D:\Project\Chain\Classroom
npm ci
npm run contract   # 更改 Solidity 后执行，固定 solc 0.8.28 / Shanghai / optimizer 200
npm test
npm run build
cd ..
cmake -S . -B build/classroom -G Ninja -DCMAKE_BUILD_TYPE=Release `
  -DCHAIN_BUILD_CLASSROOM=ON -DCHAIN_TEST_DATA_ROOT=D:/Project/Chain/build/chat-check-new
cmake --build build/classroom --parallel 3
ctest --test-dir build/classroom --output-on-failure
cd Classroom
powershell -NoProfile -File scripts/Package-Windows.ps1 `
  -BuildDirectory ../../build/classroom -CompilerBin <MinGW运行库目录> -Installer
```

安装包和校验文件输出至 `Classroom/release/`。`-Installer` 构建标准包；增加 `-OfflineInstaller` 同时构建完整离线包，也可单独指定它。包内包含节点、网关、网络工具、前端以及 v1/v2 两份合约编译结果，并生成运行文件 SHA256 清单。离线配置通过 `src-tauri/tauri.offline.conf.json` 合并，不改写主配置。

验证记录见 [0.2.0 验证数据](../reports/2026-11-03-peer-chat/validation.json) 和 [实际界面](../reports/2026-11-03-peer-chat/figures/peer-chat.png)。
