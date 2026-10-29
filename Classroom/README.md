# NornV2 区块链实验室

面向 Windows 本科教学的本地桌面应用。使用真实 C++ 节点、Ed25519 签名和 Shanghai EVM，聊天室通过普通交易部署，不是预编译或特殊权限的内置合约。共识、账户执行和数据库 schema 7 不变，不接入学习模型。

## 学生开始使用

1. 双击 `NornV2-0.1.3-windows-x64-setup.exe`，等待进度完成，应用自动打开。默认不需要选择语言、目录或逐页点击“下一步”；自动创建桌面和开始菜单快捷方式。离线安装包包含 WebView2 安装程序和 C++ 运行依赖；目标为 Windows 10 1903 及以上、Windows 11，x64，不需要安装编译器、Node.js 或 Python。
2. 点击“开始本地实验”，设置至少 8 字节的钱包密码。再次选择实验入口，程序生成独立创世配置、三个节点和测试币，并发送一笔真实的聊天室部署交易。
3. 在“我的钱包”给 Bob 转账；在“链上聊天室”发消息。点击消息可查看交易与区块。Alice、Bob 和教师是本机独立的演示钱包，不是其他同学的身份。
4. 关闭窗口会正常停止本应用所属节点，桌面进程异常退出也由网关监测并停止所属节点。重新打开会恢复上次网络；签名之前仍需解锁钱包。启动新实验保留旧实验目录。

金额均为本项目最小单位的测试币。聊天室需要保留足够余额支付 Gas 上限，成功执行后仅实际 Gas 和基础费被销毁。昵称不等于身份认证，身份以钱包地址为准。

### 安装、更新和卸载

安装程序默认只为当前 Windows 用户安装，程序目录为 `%LOCALAPPDATA%/Chain Classroom`，钱包和课堂数据另存在 `%LOCALAPPDATA%/edu.chain.classroom`。首次启动仍需自己创建钱包密码，安装器不设置默认密码。

更新或重复安装前，请正常关闭“NornV2 区块链实验室”并等待节点退出，再运行新安装包。若相关程序仍在运行，安装器会停止并提示，不会强制杀死节点。安装和重复安装保留钱包及课堂数据；Windows“已安装的应用”可以卸载，默认也保留数据，只有明确勾选卸载页面的删除数据选项才会清除。

0.1.2 将界面、窗口、安装器和快捷方式统一命名为 NornV2 区块链实验室，并移除侧栏标语。为兼容旧版，继续沿用原应用标识、安装注册键及上述程序/数据目录；升级时把指向该安装的旧快捷方式改为新名称，不创建第二份钱包目录。

高级使用可以通过 `/WIZARD` 打开完整向导以选择目录；`/NOLAUNCH` 安装后不自动启动；`/P` 显示进度而不自动启动；`/S` 静默安装，附加 `/R` 才自动启动。这些选项供教师批量安装或调试使用，普通学生直接双击即可。

安装包尚未进行发布者代码签名，Windows 可能显示发布者或信誉提示；这类系统提示不属于应用安装步骤，软件不会自动绕过。缺少 WebView2 时会运行包内微软安装程序，系统策略可能要求额外确认。

## 教师创建课堂

1. 选择“创建教学课堂”。该模式监听 IPv4 `0.0.0.0` 和可用的 IPv6 `::`；本地实验只监听回环地址。
2. 在“课堂管理”填写学生能够连接的入口，例如 `192.168.1.10:端口`、`[校园IPv6地址]:端口`，下载或复制课堂 JSON 配置。软件不自动假设哪个地址可从公网访问。
3. 学生安装应用并创建自己的加密钱包，选择“加入课堂”，导入这份配置。本机会启动一个参与同步和产块的节点，私钥不会发给教师。
4. 学生复制原生账户公钥给教师。教师使用“发放测试币”正常转账，建议默认的一千万最小单位；这不是任意增发。
5. 教师可创建房间、关闭房间、启用成员限制，并按学生的 **20 字节 EVM 地址**授权。转账用的 **32 字节公钥**与之不同。

每次新建实验加入一个零余额的独立创世公钥，使复用同一组本机钱包的不同课堂也具有不同创世指纹。邀请只包含公开的创世配置、入口、合约地址和运行代码摘要，不包含任何私钥。加入时校验创世指纹与随附的合约版本；同步后继续核对实际运行代码。

IPv6 支持不等于校园网络一定允许入站。IPv6 回环检查只说明本机协议栈可用，跨机器需要可路由地址及正确的防火墙规则。首版不在发现报文中传播带作用域的链路本地地址。

### 一键配置网络（0.1.3）

创建或加入联网课堂后进入“节点与网络”，在“联网课堂配置”查看当前网络类型、规则和本机地址。单机演示无需开放入站；学生仅主动连接老师通常也无需开放。老师或需要接收入站的学生选择“一键配置网络”，必要时手动确认 Windows 管理员授权。安装阶段不添加规则，桌面应用与网关保持普通用户权限。

- 默认 `Private + LocalSubnet`，只允许当前安装的 `runtime/boost.exe` 接受同一子网 TCP 连接，兼容 IPv4/IPv6 和动态节点端口。不包含管理网关、UDP、全局端口规则或出站规则，不修改网络类型和防火墙开关。
- 校园网可能属于 `Public` 或跨子网：先核对网卡实际类型，再选择对应类型和学校提供的 IPv4/IPv6 前缀。公用网络需要在界面确认地址范围。最多 16 个 IP/CIDR，英文逗号分隔，不接受 `Any`、`*`、`/0` 或命令文本。
- 已有相同规则直接返回，无需重复授权；修改范围会更新同一条规则。规则名称绑定规范化安装路径，撤销时只删除当前安装拥有的规则，不清理其他软件或另一份 NornV2 安装的规则。
- “已保存本应用规则”不代表实际入站可达。界面分别显示网络类型不匹配、组策略限制、规则检查失败和授权取消。只读检查及 UAC 等待异步执行，不暂停节点和消息索引。
- “检测另一台电脑的节点”发起一次有超时的节点协议查询，支持 `IPv4:端口`、`[IPv6]:端口` 和主机名。成功只说明本机能查询该节点，不证明同一创世或外部能连回本机；让另一台电脑填写你的入口才能验证该方向。
- “撤销本应用规则”按需请求授权；无规则时无需授权。普通卸载先清理自己的规则，取消或清理失败则停止卸载并保留程序，方便重试。同路径默认带进度的版本升级保留规则；高级向导移除旧版本时会清理规则，新安装按需重新配置。

独立脚本在安装目录 `runtime/Configure-Network.ps1`，脚本与 GUI 调用同一个 `norn-network.exe`。管理员可在 PowerShell 中执行：

```powershell
.\Configure-Network.ps1 -Action status
.\Configure-Network.ps1 -Action configure -Profile Private -Remote LocalSubnet
.\Configure-Network.ps1 -Action configure -Profile Public -Remote '192.168.1.0/24,2001:db8::/64'
.\Configure-Network.ps1 -Action remove
```

示例网段需替换。若学校禁用本地规则、PowerShell 或管理员授权，请把诊断交给学校管理员；脚本不绕过学校策略。UAC 出现时由用户操作。网络 helper 使用编译内嵌的规则实现，提权实例仅处理相邻节点的规则，不接收任意程序路径、脚本或输出文件路径；结果由原用户重新读取。

验证和限制见 [网络配置测试报告](../reports/2026-10-29-network-setup/REPORT.md)。

## 数据与钱包

- 默认应用目录：Windows `%LOCALAPPDATA%/edu.chain.classroom`。开发和便携使用可传 `chain-classroom.exe --data <独立目录>`。
- 钱包：`wallet.vault.json`，Argon2id 派生密钥、随机盐和 nonce、libsodium secretbox 认证加密；明文种子不写磁盘或进入前端。钱包锁定会清除服务端保存的明文种子字段。
- “加密备份”导出可跨电脑恢复的加密文件。首次创建钱包的页面支持恢复到尚无钱包的新应用目录，禁止悄悄覆盖已有钱包。遗忘密码无法恢复私钥。
- 网络：`networks/<随机ID>/profile.json`、`node*/db`、`node.log`、`genesis.json`。不要将这些运行目录加入 Git。
- 界面索引：每个网络的 `index.sqlite`。可在课堂管理中从链上重建；重建索引不删除区块链数据库。
- 诊断包：公开状态、连接与交易信息；排除钱包种子、原始签名发件箱字节和节点日志正文。课堂公钥、公开消息仍可能包含使用者自己输入的内容，导出前可查看 JSON。

## 状态含义

“已提交”表示签名交易已保存到本机发件箱并尝试发送，不等于节点已承诺入池。重试使用相同交易字节与序号；每个钱包有未确认序号时暂停分配下一笔，原生与合约交易共用序号管理。

“本机已上链”表示在当前本机规范链上观察到交易。多节点一致展示最近观测的链头，后续区块数不被标注为不可逆最终性。交易执行回退仍消耗序号与实际费用。当前协议不提供严格的最终性证明。

聊天排序依据区块高度、交易位置、事件位置。本机首次观察时间仅用于显示，不将 EVM 的逻辑区块时间当成现实日期。消息正文公开存在交易及事件中，隐藏界面记录不等于删除链上历史。

索引器逐块按哈希分页读取规范链及回执，发现重组后回退到共同祖先、删除失效索引、重放新分支。被移出的本机发件箱交易标记为退出主链；前端清除旧分页缓存，并优先使用最新规范链位置。

## 开发构建

需要已有节点构建依赖、SQLite3 开发库、Node.js 22.12+、Rust 1.90.0 MSVC 工具链、Windows SDK 与 MSVC Build Tools。`rust-toolchain.toml` 固定工具链，npm/Cargo lockfile 固定解析结果。学生使用安装包不需要这些开发工具。

```powershell
cd D:\Project\Chain\Classroom
npm ci
npm run contract      # 仅修改 Solidity 时需要；固定 solc 0.8.28 / Shanghai / optimizer 200
npm run build

cd ..
cmake -S . -B build/classroom -G Ninja -DCMAKE_BUILD_TYPE=Release `
  -DCHAIN_BUILD_CLASSROOM=ON -DCHAIN_TEST_DATA_ROOT=D:/Project/Chain/build/classroom-check-new
cmake --build build/classroom --parallel 3
ctest --test-dir build/classroom --output-on-failure

cd Classroom
powershell -NoProfile -File scripts/Package-Windows.ps1 `
  -BuildDirectory ../../build/classroom -CompilerBin <MinGW运行库目录> -Installer
```

已有本机 MinGW 构建可参考仓库根 README。`CHAIN_BUILD_CLASSROOM` 默认 OFF，原节点构建不增加 SQLite/npm/Rust 依赖；开启后新增网关、索引测试和两项真实教学网络测试。原有十一项继续保留，另有默认 IPv6 网络回归；开启教学网关后共十五项。

安装包位于 `Classroom/src-tauri/target/release/bundle/nsis/`，打包脚本同时将可分发的同一文件和 SHA256 校验文件写入 `Classroom/release/`。`Package-Windows.ps1` 从所选构建目录复制节点和网关，递归检查并收集其 MinGW DLL 依赖；清理生成资源中的旧哈希文件后，记录包含前端的完整运行文件校验清单。Windows 主程序包含 UTF-8 进程清单，中文目录不需要改变系统区域设置。Tauri 桌面壳与网关均防止同一应用目录重复启动。一键安装模板及上游来源见 [installer/README.md](src-tauri/installer/README.md)。

本地浏览器调试也可使用：

```powershell
build/classroom/chain_classroom.exe --node build/classroom/boost.exe `
  --web Classroom/dist --contract Contracts/ClassroomChat.compiled.json `
  --data build/classroom-manual-demo --open
```

网关默认只监听随机的 `127.0.0.1` 端口。启动输出的 URL 带临时访问令牌，桌面壳读取后打开；前端移除地址栏中的令牌并在当前会话内保存。HTTP 校验 Host、Origin 和令牌，不提供任意命令执行或用户指定进程终止接口。不要把管理网关反向代理到公网。

## 网络接口与范围

- 原 IPv4 种子语法保留；新增 `[IPv6]:port`、DNS 种子以及 `--bind-v6`、`--peer-protocol 2`。YAML 对应 `bind_v6`、`peer_protocol`。
- IPv4 默认使用原握手，纯 IPv6 连接自动使用新版握手；教学节点显式启用 v2。旧发现类型 9 保持六字节记录，新类型 48 包含地址族、地址和端口。
- 新类型 46/47 为有界教学只读请求/响应，支持 peers、code、分页 block 和静态 call。沿用有界查询线程池，未把 HTTP 放入共识主循环。
- 不实现完整 Ethereum JSON-RPC 或 MetaMask 兼容。静态 call 用于教学合约读取，不收费、不提交状态，也不提供转账仿真或 Gas 估算服务。
- 只提供文字群聊、房间和成员控制；首版不包含附件、端到端加密私聊或区块链最终性改造。
- 本次在本地 Windows 验证，未部署云端，未重跑十节点 TPS；不能把界面观测速率当成之前的性能实验结果。

验证记录见 [2026-10-29 实施报告](../reports/2026-10-29-classroom/REPORT.md)。

图形界面的操作、截图与问题修复见 [2026-10-29 GUI 测试报告](../reports/2026-10-29-classroom-gui/REPORT.md)。实验引导的重启完成标记仅在当前界面会话中，完成安全停止、恢复并重新观测到同一课堂和相同链头后点亮；刷新窗口后重新检查。

0.1.1 一键安装、旧版升级、重复安装、快捷方式及卸载的数据保留验证见 [安装测试报告](../reports/2026-10-29-classroom-install/REPORT.md)。
