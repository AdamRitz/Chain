# Chain Git 同步说明

配置日期：2026-09-08。远端：`https://github.com/AdamRitz/Chain.git`。当前分支：`master`。

本项目采用“完成一次修改后提交，再推送”的方式。`AGENTS.md` 要求 Codex 在任务完成时提交并核验远端；本机 Git 的 post-commit 钩子会在普通 `git commit`（包括 IDE 发起的提交）完成后尝试推送。仅在编辑器中保存文件不会触发提交，未设置文件监控或定时后台任务。

项目规则通过 Codex 的 AGENTS.md 机制加载。在其他目录启动、仅用路径引用本项目时，应让任务先读取本项目规则。[OpenAI 官方说明](https://learn.chatgpt.com/docs/agent-configuration/agents-md)

## 已配置

```text
core.hooksPath = .githooks
chain.autoPush = true
chain.syncRemote = origin
remote.pushDefault = origin
push.default = current
```

推送目标始终为 origin 的当前同名分支；新分支首次提交会建立 upstream。所有推送均为普通推送，不使用 force、不自动 pull/rebase。

`.gitignore` 排除构建文件、RocksDB 运行数据、日志、临时探针和常见凭据/钱包文件名，同时保留 `Key/*.h` 及 `Test/*.cpp` 源码。原已跟踪的 `.idea` 配置已停止跟踪，本机文件保留。忽略规则只按路径/文件名工作，提交前仍应查看 diff，不能依靠它识别任意源码中的秘密内容。Git 的 ignore 规则不自动移除已经跟踪的文件。[Git 官方说明](https://git-scm.com/docs/gitignore)

## 使用

提交标题使用 `YYYY-MM-DD 描述`。`commit-msg` 钩子检查格式；同步脚本在只提供描述时自动加上本机当天日期。完整日期标题也可以原样传入。

在项目根目录，提交明确的文件：

```powershell
git add -- Transaction/Transaction.h
powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts/Sync-Repo.ps1 -Message "2026-09-08 修复交易内容校验"
```

该脚本提交当前暂存区的全部内容，所以运行前应查看 `git diff --cached`。如果确认当前所有未忽略修改都要提交，可以运行：

```powershell
powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts/Sync-Repo.ps1 -All -Message "保存项目更新"
```

脚本在提交时暂时避免重复调用推送钩子，然后显式推送并比较本地/远端 Hash。没有新提交时也能用于补推送：

```powershell
powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts/Sync-Repo.ps1
```

直接执行 `git commit` 时，自动推送的最近日志在 `.git/chain-sync.log`。离线或被远端拒绝时，提交仍保存在本地，钩子会显示错误；解决问题后再运行同步脚本。post-commit 在本地提交产生之后执行，它的推送失败不会撤销本地提交。[Git 钩子文档](https://git-scm.com/docs/githooks)

## 暂停与重新启用

暂停当前克隆的自动推送：`git config --local chain.autoPush false`；重新启用：`git config --local chain.autoPush true`。这只控制提交钩子；同步脚本本身是显式推送命令。若也不希望 Codex 提交/推送，在当次任务中明确说明即可。

新电脑或新 clone 不会继承本地 Git 配置，先审阅钩子文件，再执行：

```powershell
git config --local core.hooksPath .githooks
git config --local chain.autoPush true
git config --local chain.syncRemote origin
git config --local remote.pushDefault origin
git config --local push.default current
```

凭据继续由 Git Credential Manager 管理，不写入仓库。首次在新电脑推送可能需要登录 GitHub。
