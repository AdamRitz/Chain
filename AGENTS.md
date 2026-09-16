# Chain 项目工作规则

## 持续提交与 GitHub 同步

用户已要求本项目每次更新保存到本地 Git 并同步 GitHub。每次完成会修改本项目文件的任务后，应执行以下流程，无需重复询问是否提交或推送；用户在当次任务中明确要求暂不提交时，以当次要求为准。

1. 开始工作先查看 `git status` 和当前分支，记录用户已有修改，不覆盖或丢弃它们。
2. 完成修改后做与变更相关的验证；检查 staged diff，排除数据库、构建产物、运行日志、密钥和凭据。文档修改无需重跑无关的节点测试。
3. 仅暂存该任务范围内的文件；不要把无关的已有修改混入提交。提交标题按用户要求使用 `YYYY-MM-DD 描述`，例如 `2026-09-08 修复交易验证`。原型本身存在的已知问题或测试限制应如实说明。
4. 创建本地提交，并推送到 `origin` 上的当前同名分支。当前仓库为 `https://github.com/AdamRitz/Chain.git`，默认工作分支 `master`。不得擅自改成其他仓库或把其他分支覆盖到 master。
5. 本机配置了 `.githooks/post-commit` 自动推送。提交后检查远端 Hash；如果钩子未启用、离线或认证失败，应显式重试普通 push。不要把本地提交成功误报为 GitHub 同步成功。
6. 出现 non-fast-forward、冲突、认证或权限问题时保留本地提交，说明阻碍。禁止为自动同步使用 force push、reset --hard、自动 stash，或未经检查自动合并远端修改。
7. 最终回复简要报告提交 Hash、分支和推送结果。纯问答/只读检查不创建空提交。

可用辅助命令：先明确 `git add -- <本次文件>`，再运行 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts/Sync-Repo.ps1 -Message "描述本次修改"`。没有修改但需要补推送时，运行不带参数的脚本。

## 项目验证注意事项

- 本项目为 C++20/CMake 原型，数据库由 `--data` 指定。测试应在隔离目录进行，避免启动节点修改已有数据库。构建和测试命令见 `README.md`。
- 修改前先阅读 `MODULE_GUIDE.md` 的模块职责、锁顺序和账户状态约束。当前账户模型与性能记录见 `ACCOUNT_MODEL_REPORT_2026-09-09.md`；2026-09-08 的审查和性能文档保留为历史资料。
- 保持现有 PascalCase 函数命名、按目录组织的头文件和直接的数据流，不为小改动引入框架或多层抽象。
- 当前数据库为 schema 4，支持账户模型、evmone Shanghai 合约和 256 块窗口内的分叉恢复。每块的原生账户、合约状态、回执、费用、交易、链头及回滚记录应原子提交。同父块按交易数和哈希选择；分支按累计有效交易数、高度、链头哈希选择。性能报告说明本地执行与同步落盘的计时口径。
- 测试使用全新数据库和明确的创世分配。旧 schema 2、3 数据库保留原样，采用新目录运行。两个版本比较时固定数据集、发送端、核心亲和性、线程、区块参数和同步写入方式。
- 嵌套取锁顺序为 dbCommitMutex → txpoolMutex；blockBufferLock 单独持有。保持 txpool、txUserPool 和 readyPoolTx 同步，满块触发依据 readyPoolTx。
- 账户、交易池、EVM 或分叉变更应运行全部七项 CTest。网络测试目录每次使用新路径。包含 ForkStateCorrectness、ForkNetwork、EvmNetwork 的状态约束、写盘失败和分区重连覆盖。
- EVM 接入方式、执行环境和费用规则见 EVM_GUIDE.md；依赖版本及上游补丁见 ThirdParty/VERSIONS.md。实际编译不链接上游预编译输入输出占位表。
- 更改 Git 配置只作用于本仓库，不修改其他仓库或用户的全局 Git 配置。
