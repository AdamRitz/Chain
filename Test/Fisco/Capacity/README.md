# FISCO 3.7.3 原生转账容量测试

固定官方 v3.7.3、十个 PBFT 验证节点、每台 4 vCPU。测试对象为内置 `0x100c` 的 `userTransfer`；十个发送器共用 1,024 个逻辑账户，金额 1，保持全局环形路由。每个发送器只提交全局下标 `local_index * 10 + shard` 的交易，初始化也按账户编号分区，避免重复创建或把账户数扩大十倍。

`Run.py` 在原控制机运行，通过已有 SSH 密钥管理隔离目录。`Aligned/NativeAlignedBench.java` 默认仍为单发送器；容量测试通过环境变量 `FISCO_BENCH_SHARDS`、`FISCO_BENCH_SHARD`、`FISCO_BENCH_PREFIX` 开启十路发送。签名在开始计时前生成，SDK 和节点共用所在机器，报告必须保留这种资源竞争。

每个用例使用新数据库；共识成员、密码算法、TLS、存储参数及校验保持原样。配置对照包括封块间隔、区块上限、串行/DMC 执行、验签线程和交易树传播。调整参数只写入 `/var/tmp/fisco-experiments/<case>/`；常驻配置和数据库保留。结束时保存证据，核对停机身份，再删除该用例数据库并恢复原服务。

```bash
python3 Run.py --hosts /opt/fisco-control/automation/fisco-adapter/hosts.json \
  --name unique-batch --plan plan.json
```

计划示例（一次短测，不等于可持续容量证明）：

```json
[
  {"profile":"serial50","rate":20000,"warm":2,"seconds":10,"repeat":1,"inflight":3000}
]
```

`rate` 为十个发送器合计目标；`inflight` 为每个发送器未完成请求上限。程序实行回压，目标值不能替代实际提交速率。正式五档沿用 20k、30k、40k、50k、60k；延长时长或增加重复按仓库规则取得用户选择。

主 TPS 为十节点最小累计已落块计数在测量两个端点的增量除以 RPC 区间中点的时间差。端点须在停机后规范账本中核对哈希与累计计数；采集区间跨度超过 0.5 秒的端点不能进入正式排名。短测可消化预热积压，不能解释为长期容量。

成功交易必须同时满足回执状态 0 和原生返回码 0。停止全部节点后逐一无 quorum 重启，保存每个持久化链头；枚举最长规范账本，匹配预签名哈希并重建全部账户余额。已入块但成功回调缺失的交易逐一补查业务返回值。所有节点在各自高度的哈希、状态根和计数必须匹配规范前缀；同时报告是否全部追平。

回执延迟从各发送器实际调用 SDK 到业务成功回调，以各进程单调时钟测量；合并 1 ms 向上取整直方图。该指标仅覆盖观察预算内成功回调，必须同时报告拒绝、未回调和未发出数量。它不是十节点共同可见延迟，也不是纯 PBFT 耗时。

`Sample.py` 每秒记录节点和发送器 CPU、常驻内存、线程 CPU、磁盘与网卡计数。`TestCapacity.py` 验证分区初始化、全局环形路由及回执下标重组；云端原生冒烟测试补充真实 SDK、配置和账本验证。

`Analyze.py --raw <解压后的批次父目录> --output <分析.json>` 从保存证据重算采样跨度、资源使用、队列、延迟及测量期内 PBFT 超时；日志采用服务器 Asia/Shanghai 时区，排除初始化和停机后无 quorum 审计期间的超时。报告同时保留实际 SDK 调用起点与计划发送起点的延迟，防止回压下只报告低回执延迟。

2026-10-26 已完成 1 次冒烟、12 次不同配置/压力对照及 5 档正式短测，见 [容量实验报告](../../../reports/2026-10-26-fisco-capacity/REPORT.md)。正式批次最高 19,453 TPS，调参短测最高 20,534 TPS；均不是长期可持续容量的证明。

2026-10-27 用户已明确选择五档各测量 60 秒、重复 3 次。长测固定 `serial50`、每发送器在途阈值 3000，预热仍为 2 秒、收尾最多 5 秒；各重复改变档位顺序，保持其余条件一致。计划为第 1 轮 20/30/40/50/60k，第 2 轮 60/50/40/30/20k，第 3 轮 30/50/20/60/40k。

长测在隔离 `config.genesis` 设置 `chain.block_limit=5000`，通过 `FISCO_BENCH_BLOCK_LIMIT_OFFSET=4000` 将预签名交易固定为初始化高度 +4000。SDK 接口代理仅覆盖签名过程使用的 `getBlockLimit`，其余方法转交原客户端；节点签名与 blockLimit 校验保留。准备时检查首尾实际编码值和剩余高度预算，审计时再核对最终高度。默认 offset=0，原短测行为不变。该版本不能把这个参数仅写在 `config.ini`；首次冒烟对此返回 BlockLimitCheckFail，修正后的冒烟及状态审计通过，两次记录均保留。

`compact_records=true` 在该轮完成状态核对和数据库清理后，将较大的 JSONL 证据无损 gzip 压缩。逐个解压校验 SHA256 后才移除原始文本副本，记录于 `compressed-records.json`；签名数据、所有证据内容均保留。分析器同时支持文本及压缩账本。控制机每轮开始前要求至少 2 GiB 可用空间。

`window_screen_passed` 只是单个 60 秒窗口的初筛，不自动命名为可持续上限。正式结论还需合并三个重复、查看完整队列时间序列、确认实际调用达到目标，并说明回压与未提交请求。

五档 60 秒 ×3 已完成，见 [长测报告](../../../reports/2026-10-27-fisco-long/REPORT.md)。最高三次中位数为 20,002 TPS，单次最高 20,214 TPS；各档均未持续接纳目标输入。全部 17,130,898 笔实际调用最终成功入块，其中 80,720 笔有过 SDK 错误回调，均经逐笔审计确认成功，不能将这些回调直接算作最终拒绝或丢单。用户选择只完成这 15 轮，未增加其他容量档位。

参考：[v3.7.3 节点配置实现](https://github.com/FISCO-BCOS/FISCO-BCOS/blob/v3.7.3/bcos-tool/bcos-tool/NodeConfig.cpp)、[原生转账实现](https://github.com/FISCO-BCOS/FISCO-BCOS/blob/v3.7.3/bcos-executor/src/precompiled/extension/DagTransferPrecompiled.cpp)、[串行调度实现](https://github.com/FISCO-BCOS/FISCO-BCOS/blob/v3.7.3/bcos-scheduler/src/SerialBlockExecutive.cpp)。
