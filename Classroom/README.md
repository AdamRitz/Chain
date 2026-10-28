# Chain 区块链实验室

面向 Windows 本科教学的本地桌面应用。使用真实 C++ 节点、Ed25519 签名和 Shanghai EVM，聊天室通过普通交易部署，不是预编译或特殊权限的内置合约。共识、账户执行和数据库 schema 7 不变，不接入学习模型。

## 学生开始使用

1. 安装 `Chain Classroom_0.1.0_x64-setup.exe`，打开“Chain Classroom”。离线安装包包含 WebView2 安装程序和 C++ 运行依赖；目标为 Windows 10 1903 及以上、Windows 11，x64。
2. 点击“开始本地实验”，设置至少 8 字节的钱包密码。再次选择实验入口，程序生成独立创世配置、三个节点和测试币，并发送一笔真实的聊天室部署交易。
3. 在“我的钱包”给 Bob 转账；在“链上聊天室”发消息。点击消息可查看交易与区块。Alice、Bob 和教师是本机独立的演示钱包，不是其他同学的身份。
4. 关闭窗口会正常停止本应用所属节点，桌面进程异常退出也由网关监测并停止所属节点。重新打开会恢复上次网络；签名之前仍需解锁钱包。启动新实验保留旧实验目录。

金额均为本项目最小单位的测试币。聊天室需要保留足够余额支付 Gas 上限，成功执行后仅实际 Gas 和基础费被销毁。昵称不等于身份认证，身份以钱包地址为准。

## 教师创建课堂

1. 选择“创建教学课堂”。该模式监听 IPv4 `0.0.0.0` 和可用的 IPv6 `::`；本地实验只监听回环地址。
2. 在“课堂管理”填写学生能够连接的入口，例如 `192.168.1.10:端口`、`[校园IPv6地址]:端口`，下载或复制课堂 JSON 配置。软件不自动假设哪个地址可从公网访问。
3. 学生安装应用并创建自己的加密钱包，选择“加入课堂”，导入这份配置。本机会启动一个参与同步和产块的节点，私钥不会发给教师。
4. 学生复制原生账户公钥给教师。教师使用“发放测试币”正常转账，建议默认的一千万最小单位；这不是任意增发。
5. 教师可创建房间、关闭房间、启用成员限制，并按学生的 **20 字节 EVM 地址**授权。转账用的 **32 字节公钥**与之不同。

每次新建实验加入一个零余额的独立创世公钥，使复用同一组本机钱包的不同课堂也具有不同创世指纹。邀请只包含公开的创世配置、入口、合约地址和运行代码摘要，不包含任何私钥。加入时校验创世指纹与随附的合约版本；同步后继续核对实际运行代码。

IPv6 支持不等于校园网络一定允许入站。IPv6 回环检查只说明本机协议栈可用，跨机器需要可路由地址及正确的防火墙规则；应用不自动修改系统防火墙。首版不在发现报文中传播带作用域的链路本地地址。

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

安装包位于 `Classroom/src-tauri/target/release/bundle/nsis/`。`Package-Windows.ps1` 从所选构建目录复制节点和网关，递归检查并收集其 MinGW DLL 依赖。Windows 主程序包含 UTF-8 进程清单，中文目录不需要改变系统区域设置。Tauri 桌面壳与网关均防止同一应用目录重复启动。

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
