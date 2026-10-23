# Q1 / B3 本地实现与验证

日期：2026-10-23。基于 `a53f5663f9e7b344f496dbd97b7e843f513b9608`，仅本地实现与正确性回归，未部署十台服务器、未开展新一轮 TPS 实验。机器上的无关既有修改未纳入本次提交。

## 实现结果

| 独立路线 | 实现 | 针对的问题 |
|---|---|---|
| Q1：工作调度与池锁 | 摘要/补传独立线程和字节预留；冷验签共享并发预算；账户轮询分段复制后在池锁外选择 | 完整块与摘要互相排队、多个线程池争抢 CPU、候选准备长时间持有池锁 |
| B3：候选边际价值 | 分开的积压特征、同父候选交易数与抽样重合度、满块替代规则、在线学习本地保留的计数优势 | B2 高负载输入饱和、仅学习候选保留概率而未衡量相对已有候选的收益 |

Q1 的许可只覆盖实际冷 Ed25519 验签。普通交易与区块共用并发上限，有普通交易等待时最多连续授予四次区块优先许可。摘要独立预留 64 条消息与 8 MiB；重建后仍进入完整块验证。交易池选择每段复制最多 64 个账户的队首交易，保持父状态读锁，账户检查与排序在池锁外执行；无池迭代器跨越解锁。

B3 具有 12 维输入和一个从零权重开始的在线逻辑回归模型。标签取准备完成后 250–500ms 内观测到的本地主链：只有候选保留时计算 `max(本地候选交易数 - 决策时已有候选交易数, 0) / 本地产块上限`，否则为零。无法观测、超时、退出或队列溢出样本删失；跳过的准备没有标签。至少 16 个真实样本才能启用未满块替代的学习决策。

满块替代是单独可观测的冷启动规则：同父、同高度且仍留在缓冲区的候选已达到本地产块数量上限，预计准备至少 5ms，窗口仍开放时可以让步。连续两次后恢复普通准备。它不能保证交易内容、哈希排名或未来分支完全相同，不能将这部分收益归因于模型训练。

## 配置

所有新功能默认关闭。以下是附加参数，需与已有创世、端口、数据目录等配置一同使用。

```text
# Q1，预算 3 只是后续四核机器的待验证配置
--work-scheduler 1 --verification-budget 3 --pool-snapshot 1

# B3 影子观测，要求已有有效的 bounded/连续时钟配置
--candidate-marginal 1

# B3 实际有界行动
--candidate-marginal 2
```

B3 必须使用 `--round-scheduler bounded`，不能同时开启 `--candidate-risk 1` 或 `--candidate-value 2`。Q1 与 B3 可分别启用或组合启用。`--verification-budget 0` 在 work-scheduler 开启时自动取硬件线程数减一、限制在 1–64；work-scheduler 关闭时表示不限制。

指标新增共享验签许可等待与峰值、元数据字节在途量、分段池锁持有时间，以及 B3 预测、规则建议、总建议、实际让步、标签、删失和绝对误差。学习建议数等于总建议减规则建议。等待与锁计时均为累计墙钟口径，不能当成 CPU 独占时间。

## 验证

Windows Release 构建成功。完整 14 项 CTest 全通过，总耗时约 94.69 秒，网络测试使用全新的隔离根目录。随后补充 B3 实际行动回归，并让两条选择路径从完全相同、未被另一条路径清理过的交易池独立开始；重新编译并分别运行 ClockCorrectness 与 AccountCorrectness，均通过。节点运行代码在完整回归后没有再修改。

| 验证 | 结果与范围 |
|---|---|
| 原有 14 项 CTest | 全部通过；账户、分叉恢复、EVM、测量索引、接收背压和时钟网络路径 |
| FlowCorrectness 新用例 | 共享许可上限、普通交易不被持续饿死、异常释放、完整块线程和字节预留被占满时元数据仍推进、130 个账户跨段选择一致性 |
| AccountCorrectness / ForkStateCorrectness 新比较 | 原生账户、nonce 缺口、余额限制，以及包含 EVM 输入的静态池选择与原实现一致 |
| ClockCorrectness 新用例 | 高负载特征区分、规则与学习门槛、低收益让步与恢复、延后标签和删失、实际连续两次让步后正常准备、已有候选正常提交 |
| ClockNetwork | 共 17 个场景；新增 Q1、B3 shadow、B3 active、Q1+B3，均核对两节点账本；同时检查参数生效与验签峰值 |
| EvmNetwork `--optimized` | 额外 11 组检查通过：部署、Gas、REVERT、预编译、Solidity 调用、输入篡改、补传、复制、重启和分叉状态收敛 |

汇总、各项耗时、二进制 SHA-256 和源文件摘要见 [validation.json](validation.json)。本地原始 JUnit 保留于 `build/policy-v3-local-ctest.xml`、`build/policy-v3-clock-active-ctest.xml` 与 `build/policy-v3-pool-equivalence-ctest.xml`；JUnit 成功输出存在截断，配置记录另外取自本地进程记录。额外 EVM 结果位于 `build/policy-v3-evm-optimized-20261023-160428665/result.json`。所有测试进程已退出；Cluster.Close 管理的数据库已在测试内清理。额外数据库清理被工具自动安全检查以 `blocked by policy` 拒绝，包含后续账户回归共 28 个本地数据库目录暂时保留，输入、结果与日志继续保留在忽略的 build 目录，未纳入提交。

复现入口：

```powershell
$testRoot = 'D:/Project/Chain/build/q1-b3-check-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff')
cmake -S . -B cmake-build-release "-DCHAIN_TEST_DATA_ROOT=$testRoot"
cmake --build cmake-build-release --target boost chain_test user_test fork_state_test throughput_test flow_test clock_test sender evm_sender -j 4
ctest --test-dir cmake-build-release --output-on-failure -j 1
python Test/EvmNetworkTest.py --bin cmake-build-release --work "$testRoot/evm-optimized" --optimized
```

## 解释边界与下一步

此次实现没有改变块有效性、同父/分支排名、签名与合约输入绑定、账户状态验证或同步落盘方式。Q1 初始账户集合仍需持有池锁，父状态读锁也保留；它不是异步提交管线或全池原子快照。并发到达可以影响分段选择的候选内容，最终仍走正常验证。

B3 的计数优势是局部代理，没有反事实全网 TPS 标签。成本评分与误差余量是固定启发式，短测内的有效样本可能不足；每个进程重新学习。它没有重新训练现有 preparation-v2，也没有重训真实时间误差模型。

因此目前可以确认功能接入和回归通过，不能确认 Q1、B3 或组合的 TPS 高于原版。后续获准测试时，先固定发送端、同步落盘、区块参数、线程和数据集，分别比较原版、Q1、B3 shadow、B3 active 和组合；核对共同链 TPS、P95 确认时延、分支重放、队列、池锁和模型/规则各自的行动次数。
