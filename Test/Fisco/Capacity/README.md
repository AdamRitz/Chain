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

参考：[v3.7.3 节点配置实现](https://github.com/FISCO-BCOS/FISCO-BCOS/blob/v3.7.3/bcos-tool/bcos-tool/NodeConfig.cpp)、[原生转账实现](https://github.com/FISCO-BCOS/FISCO-BCOS/blob/v3.7.3/bcos-executor/src/precompiled/extension/DagTransferPrecompiled.cpp)、[串行调度实现](https://github.com/FISCO-BCOS/FISCO-BCOS/blob/v3.7.3/bcos-scheduler/src/SerialBlockExecutive.cpp)。
