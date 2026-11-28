# 100 节点 Work 选举 TPS 测试

使用现有 `cluster_quick.py test` 一次分发、一次启动，依次测试 8000、9000、10000、11000 TPS，每档预热 2 秒、测量 30 秒、结束等待 5 秒。四档使用同一个链和签名数据的连续区间，账户 nonce 连续递增。结束后停止全部测试进程，采集结果并删除本轮数据库。

## 控制机一键入口

2026-11-28：100 节点恢复配置已离线检查通过，TPS 测试尚未启动。5282 个区块的真实历史和 100 个身份各 33 份 Work 已生成并验证，控制机与本机均保留备份。Linux 完整回归 48/48 通过；同一部署包在本机 Ubuntu 完成五节点恢复，用时 64.18 秒，五人名单生效、每节点 1024 个账户检查通过。部署包、发送程序和运行库已就绪。测试入口为：

```bash
bash /opt/chain-work-priority-20261128/run-tps.sh test
```

脚本要求已准备的快照存在，自动生成运行名，读取 SSH 密码，校验构建结果与部署包，解包本包的发送程序和运行库，同步时钟，再执行上述四档测试。也可指定运行名：

```bash
bash /opt/chain-work-priority-20261128/run-tps.sh test work100-001
```

脚本使用以下路径，源码与部署包版本均为 `work-priority-003`：

| 内容 | 路径 |
| --- | --- |
| 源码 | `/opt/chain-vdf/releases/vdf/work-priority-003/source` |
| 节点线程配置 | `/opt/chain-work-priority-20261128/node-settings.json` |
| 100 节点计划 | `/opt/chain-continuous32-20261126/plan.json` |
| 部署包 | `/opt/chain-work-priority-20261128/build-v5/work-priority-003.tar.gz` |
| 历史与 Work 快照 | `/opt/chain-work-priority-20261128/history-001/snapshot.json` |
| 发送程序 | `/opt/chain-work-priority-20261128/controller/work-priority-003/bin/sender` |
| 运行库 | `/opt/chain-work-priority-20261128/controller/work-priority-003/lib` |
| 测试结果 | `/opt/chain-work-priority-20261128/results/运行名` |

`test` 要求 `build-v5/build-completed-002.json` 的 `exit_code=0` 且 `package.json` 存在，然后按 SHA256 校验部署包。首次生成原始创世配置和身份使用 `prepare`，快照准备完成后使用 `prepare-snapshot` 检查恢复配置；两者均不连接测试节点：

```bash
bash /opt/chain-work-priority-20261128/run-tps.sh prepare
bash /opt/chain-work-priority-20261128/run-tps.sh prepare-snapshot
```

## 测试参数

| 参数 | 数值 |
| --- | --- |
| 节点 / 普通邻居 / 发送入口 | 100 / 8 / 4 |
| 时间片 / 每块交易上限 | 200 ms / 6000 笔 |
| 委员会 / 连续出块 / 超时接管 | 5 人 / 每人 32 块 / 连续 2 个时间片未出块 |
| Work 迭代次数 / 抽查 | 65536 / 4 组，每组 8 份 |
| IO / 交易验签 / 候选执行 / 区块验证 / 查询线程 | 2 / 2 / 2 / 2 / 1 |
| Work 证明验证线程 | 2，由节点内部共用 |
| 测试端口 | 28089，沿用已有计划 |
| 测量时长 | 每档 30 秒 |

交易打包没有步长。`work_iterations=65536` 沿用当前临时参数，测试结果同时记录迭代次数和 32 份证明的验证成本。

默认准备流程先在一台控制机生成真实历史，并计算 100 个身份各自的 Work。百节点测试使用 `--work-snapshot` 恢复这份空账本、原始创世配置和各自的 Work，保留原 `start_time_ms` 和私钥。启动后核对全部节点的链标识、快照高度与区块哈希，等待恢复后一次完整的五人选举及其名单生效，再开始发压。每张选举票验证 32 份 Work；阶段记录保存在 `benchmark/readiness.json`。

快照恰在一轮末尾时，恢复后约需 32 秒完成选举；快照包含下一轮少量区块时，约需 64 秒。实际时间还包括网络连接和验证。全系列只等待一次。不传 `--work-snapshot` 时仍可从创世开始，约需 **1088 秒，即 18.1 分钟**。

四档共发送 1,216,000 笔，使用约 204.1 MiB 签名数据。数据库按每笔 1024 字节估计约 1.16 GiB/台，再保留 1 GiB，启动前要求每台至少 2.16 GiB 空闲。`test-plan.json` 保存按实际档位计算的预算。恢复快照后的选举与四档测试约 3～3.6 分钟，另计部署、账户核对及清理。

## 提前准备

在控制机编译一次并完成回归，用 `cluster_release.py` 生成部署包；所有节点直接接收同一份二进制。控制机的 `sender` 必须来自同一部署包。复用已有 305 万笔签名数据，不重新签名。

准备命令只读已有交易数据并生成本轮配置，不连接服务器：

```bash
P=/opt/chain-vdf/setup/venv/bin/python
R=/opt/chain-continuous32-20261126
W=/opt/chain-work-priority-20261128
S=/opt/chain-vdf/releases/vdf/work-priority-003/source

"$P" -B "$S/ansible/scripts/cluster_quick.py" prepare \
  --plan "$R/plan.json" \
  --dataset "$R/dataset/transactions.bin" \
  --dataset-genesis "$R/dataset/transactions.bin.genesis.json" \
  --dataset-manifest "$R/dataset.json" \
  --node-settings "$W/node-settings.json" \
  --work-election --work-iterations 65536 --work-groups 4 --work-group-size 8 \
  --rates 8000 9000 10000 11000 --duration 30 --warmup 2 --drain-seconds 5 \
  --wallets 1024 --output "$W/prepared/work100-001"
```

输出包含公开创世配置、节点公钥、拓扑、数据摘要和预算。每个节点的种子保存在 `identities/` 中，目录权限 0700，文件权限 0600；公开 JSON 只包含公钥和文件路径。不要将种子目录加入 Git、论文附件或公开结果包。

## 提前生成并保留历史

只需开启控制机。使用上述 `prepared/work100-001` 的原始配置，在控制机运行五个真实节点生成至少 33 个完整周期，期间不发送交易：

```bash
"$P" -B "$S/ansible/scripts/work_history_prepare.py" \
  --bin "$W/controller/work-priority-003/bin/boost" \
  --prepared "$W/prepared/work100-001" --output "$W/history-001"
```

脚本停止五个进程，保存共同账本 `ledger.tar.gz`、每轮区块摘要 `anchors.json` 和中间记录 `history.json`。随后用 `work_prepare` 计算 100 个身份的全部历史 Work；同一身份逐轮顺序计算，不同身份并行：

```bash
work_prepare --anchors "$W/history-001/anchors.json" \
  --identities "$W/prepared/work100-001/identities.json" \
  --out "$W/history-001/work-cache" --threads 2
```

可以在本地执行该命令，将生成的 `work-cache` 上传到控制机。工具只读取公钥，不需要私钥；已有文件验证通过后直接复用。33 轮每个身份保存 33 份真实证明，原始证明数据为 `100 × 33 × 346 = 1,141,800` 字节，文件系统占用另计。

全部 Work 完成并通过工具验证后，以 `history.json` 为基础写入 `snapshot.json`。其中 `anchors_file`、`anchors_sha256` 指向完整周期摘要；`work_directory` 指向上传后的 `work-cache`。`prepare --work-snapshot "$W/history-001/snapshot.json"` 会核对原创世配置、账本及摘要的 SHA256、全部身份、每轮 Work 的区块摘要和前一输出，然后只为各节点打包自己的证明。`history.json` 阶段不能标记为准备完成。

共享归档只包含停机后的 RocksDB 文件。每台服务器保留一份独立恢复源，实际测试使用新的数据库副本。清理只删除测试副本；共同账本、Work、原始身份和创世配置继续保留，可用于后续多次测试。

## 集群开启后

先使用既有恢复命令清理中断的测试，再在测试进程停止时执行控制机现有 `sync-clock.sh`。启动后脚本通过时间查询检查节点间偏差；超过 20 ms 就停止测试并保存 `clock-preflight.json`。

以下 `PACKAGE`、`RELEASE`、`SENDER`、`SHA` 使用提前构建并校验的部署包。测试复用快照的身份和创世配置，输出目录必须不存在：

```bash
export ANSIBLE_CONFIG="$S/ansible/ansible.cfg"
"$P" -B "$S/ansible/scripts/cluster_quick.py" test \
  --plan "$R/plan.json" --release-id "$RELEASE" \
  --package "$PACKAGE" --package-sha256 "$SHA" --sender "$SENDER" \
  --dataset "$R/dataset/transactions.bin" \
  --dataset-genesis "$R/dataset/transactions.bin.genesis.json" \
  --dataset-manifest "$R/dataset.json" \
  --extra-vars-file "$R/connection-env.json" \
  --node-settings "$W/node-settings.json" \
  --ansible-playbook /opt/chain-vdf/setup/venv/bin/ansible-playbook \
  --python /usr/bin/python3 --forks 32 --query-workers 32 --batch 128 \
  --work-election --work-iterations 65536 --work-groups 4 --work-group-size 8 \
  --work-snapshot "$W/history-001/snapshot.json" \
  --rates 8000 9000 10000 11000 --duration 30 --warmup 2 --drain-seconds 5 \
  --readiness-timeout 180 --consensus-timeout 180 --wallets 1024 \
  --early-stop-start-seconds 5 --early-stop-window-seconds 5 --early-stop-windows 2 \
  --output "$W/results/work100-001"
```

SSH 密码仍通过现有环境变量和连接配置读取。已分发并核对同一部署包时可加 `--staged`。不要使用旧 `optimization/matrix.sh` 跑此系列：该脚本每档都会建立新链。

## 结果和停止条件

- 每档的 `result.json`：实际发送 TPS、提交 TPS、确认时间中位数、重组次数、积压变化、账户和 nonce 校验。
- `resources[].work`：测量期间 Work 生成及验证耗时、证明检查数、票检查数、跳过数、队列拒绝数。
- `timeline.jsonl`：各节点完整统计，包括 Work 历史数量、证明队列和选举状态。
- `series.json`：按档位汇总。通过条件为发送和提交均达到目标的 95%、确认时间中位数不超过 1000 ms、交易与账户核对通过、积压增长不超过配置上限。
- 每档确认完成并核对账户后才发送下一段数据；失败、交易未收齐或持续低吞吐时结束系列。Ansible 的 `always` 步骤继续停止、采集和清理。
- `cleanup-节点名.json`：每台本轮进程停止与数据库删除结果。代码、部署包、签名数据及实验记录保留。

每档基于各自测量区间计算 TPS、CPU 和重组增量，历史预热不计入 30 秒测量时长。
