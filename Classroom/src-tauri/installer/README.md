# Windows 一键安装模板

`OneClick.nsi` 基于 [Tauri CLI 2.12.4 的 NSIS 模板](https://github.com/tauri-apps/tauri/blob/tauri-cli-v2.12.4/crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi)，按随附 MIT 许可使用。原文件 SHA256：`d00089d3b1a2c4a30d7b6e098079100cb8f25e980f84d4515aca4597b95e967d`。升级 Tauri CLI 时需复核模板变化；保留上游的依赖检测、资源复制、快捷方式、注册表、升级和卸载路径。

标记为 `Chain:` 的修改：

- 默认无参数启动使用带进度条的自动安装，成功后启动应用；安装语言跟随系统，未支持的语言回退到中文。
- `/WIZARD` 显示原安装向导；`/NOLAUNCH` 禁止默认安装完成后自动启动。
- `/P` 保留带进度的无人值守模式，`/S` 保留静默模式；两者默认不启动，附加 `/R` 可启动，`/ARGS` 传递应用参数。
- 安装和卸载通过 Windows Restart Manager 检查确切安装目录中的桌面程序、网关及节点；发现仍运行时返回 2，检查初始化失败返回 3，要求先正常关闭。不会使用上游被动模式的强制终止逻辑。
- 普通卸载默认保留钱包和课堂；高级卸载页面仍保留明确勾选删除应用数据的选项。一键安装和重复安装不选择该选项。

安装流程是当前用户级别，安装文件默认在 `%LOCALAPPDATA%/Chain Classroom`，数据在独立的 `%LOCALAPPDATA%/edu.chain.classroom`。安装程序不创建钱包，不保存默认密码，不自动修改防火墙。

Tauri 自定义模板入口参见[官方 Windows 安装文档](https://v2.tauri.app/distribute/windows-installer/#installer-template)。
