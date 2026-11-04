# Windows 一键安装模板

`OneClick.nsi` 基于 [Tauri CLI 2.12.4 的 NSIS 模板](https://github.com/tauri-apps/tauri/blob/tauri-cli-v2.12.4/crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi)，按随附 MIT 许可使用。原文件 SHA256：`d00089d3b1a2c4a30d7b6e098079100cb8f25e980f84d4515aca4597b95e967d`。升级 Tauri CLI 时需复核模板变化；保留上游的依赖检测、资源复制、快捷方式、注册表、升级和卸载路径。

标记为 `Chain:` 的修改：

- 默认无参数启动先显示程序目录选择，再自动安装，成功后启动应用；安装语言跟随系统，未支持的语言回退到中文。账户和链数据目录独立，不随程序目录搬移。
- `/WIZARD` 显示原安装向导；`/NOLAUNCH` 禁止默认安装完成后自动启动。
- `/P` 保留带进度的无人值守模式，`/S` 保留静默模式；两者默认不启动，附加 `/R` 可启动，`/ARGS` 传递应用参数。
- 安装和卸载通过 Windows Restart Manager 检查确切安装目录中的桌面程序、网关及节点；发现仍运行时返回 2，检查初始化失败返回 3，要求先正常关闭。不会使用上游被动模式的强制终止逻辑。
- 普通卸载默认保留账户和链数据；高级卸载页面仍保留明确勾选删除应用数据的选项。默认安装和重复安装不选择该选项。
- 目录页面位于旧版本维护页面之前，选择目标后再决定如何卸载旧版本。同版本更换目录也清理旧程序。被动模式不再从不存在的单选控件读取维护选项。
- 检查注册的旧安装及新目标中的运行进程，包括 `/D` 指定不同目标的情况。选择目录后再次检查目标。
- 文件采用独立 LZMA 压缩，避免跳过 WebView2 后仍需解码其所在的整块压缩流。标准包使用 `embedBootstrapper`；可选离线配置保留 `offlineInstaller`。两种模式沿用上游运行库检测和错误处理。

安装流程是当前用户级别，安装文件默认在 `%LOCALAPPDATA%/Chain Classroom`，数据在独立的 `%LOCALAPPDATA%/edu.chain.classroom`。安装程序不创建钱包，不保存默认密码，不自动修改防火墙。

0.1.3 随包提供 `runtime/norn-network.exe` 与管理员脚本。启动应用并点击网络配置后才按需提权。卸载前调用 helper 的 `remove`：无规则时不提权；清理失败或授权取消返回非零，卸载返回 4 并保留程序。同路径被动版本升级向旧卸载器传 `/UPDATE` 保留规则；更换路径照常清理旧安装规则。运行中 helper 也加入 Restart Manager 检查，避免升级替换权限操作中的文件。

0.1.2 的显示名称为 NornV2 区块链实验室；`INSTALLIDENTITY` 固定为旧名称，仅用于程序目录及安装注册键，应用标识也不变，以便识别旧版本并保留原数据。桌面和开始菜单旧快捷方式只有在目标为该安装的主程序时才迁移为新名称。NSIS 模板的编译输入文件名从 tauri.conf.json 读取，分发文件使用 `NornV2-<version>-windows-x64-setup.exe`。

Tauri 自定义模板入口参见[官方 Windows 安装文档](https://v2.tauri.app/distribute/windows-installer/#installer-template)。
