# 轮流出块、五分钟测试与 Ansible 部署

日期：2026-11-25。

## 实现

- 新链设置 `vdf.producer_rotation: true`。每 32 块选出的最多五个公钥按 VDF 排序值、公钥排序，当前生产者为 `producers[(slot - StartSlot) % producers.size()]`。
- 时间片使用 200 ms。某个成员离线时跳过其时间片，下一成员继续；正常成员不能通过附带证明绕过轮值。非成员原有的 20 个时间片恢复条件保留。
- 同一规则用于生成区块和验证收到的区块。未来区块进入有界缓存，到对应时间片才采用，数据库提交再次检查时间。
- 正常轮值块验证后立即提交。最初两轮、空名单和非成员恢复出块保留原来的比较等待。合法迟到块仍按现有分支规则处理。
- 旧链默认关闭轮值，原创世编码和摘要保持不变。启用轮值使用新的创世配置和数据库。
- 集群首次启动默认只由第一台生成前两轮区块，其余节点仍计算并传播 VDF，第三轮起自动轮值。首节点在最初两轮内退出时，控制器会因启动超时结束测试并清理；重新选择第一台后使用新测试目录执行。

## 快速部署和测试

入口：`ansible/scripts/cluster_quick.py`，支持 `stage`、`deploy`、`test`。完整参数见 [集群操作说明](../../ansible/CLUSTER.md)。

- 在构建机编译一次，其他机器直接使用相同发布包。源码和调试信息保留。
- 已缓存包的 SHA256 一致时跳过复制。此次连续执行两次 Ansible stage，第二次 `changed=0`，包的修改时间与 SHA256 都未改变。
- 正式测量默认 300 秒，预热与结束等待单独设置。按 `ceil(rate × (warmup + duration))` 准备交易，按发送工具摘要、交易数、账户数缓存签名数据。
- 所有节点连接、名单和共同链检查通过后开始发送。结果保存后停止测试进程并删除本轮数据库。
- 控制器不保留每次查询的完整节点统计，使用节点统计时间戳计算 CPU 等计数差；确认样本循环查询，收尾检查全部未完成样本。
- 多机部署和查询默认并发 32，支持 1024 节点的固定连接数量拓扑。后续 100/1000 台机器的性能测试可使用该入口执行。

云端已有发布包，准备实际服务器的 `nodes.json` 后可运行：

```bash
/opt/chain-vdf/setup/venv/bin/python -B \
  /opt/chain-vdf/releases/vdf/rotation-001/source/ansible/scripts/cluster_quick.py test \
  --nodes /opt/chain-rotation/nodes.json --cluster rotation-test \
  --release-id rotation-001 \
  --package /opt/chain-rotation/quick-validation-001/rotation-001.tar.gz \
  --package-sha256 b85289ec728e28ed799525727f15137ce3bebb639073fa320017354c098d3d28 \
  --sender /opt/chain-rotation/releases/rotation-001/bin/sender \
  --ansible-playbook /opt/chain-vdf/setup/venv/bin/ansible-playbook \
  --python /usr/bin/python3.11 --rate 10000 \
  --cache /opt/chain-rotation/datasets \
  --output /opt/chain-rotation/results/rotation-test001
```

`nodes.json` 是后续实际服务器清单；SSH 密钥或连接变量按集群文档配置。每次测试使用新的输出目录。交易文件只保存在控制机，不向每个节点复制。

## 本次验证

主机 `118.122.75.11`：2 个 vCPU、约 4 GB 内存。只使用这一台主机。

- 完整 **38 项 CTest 通过**，包含原有账户、分叉、EVM、GUI 网关、钱包、IPv6 回归。
- **111 项 Ansible 工具测试通过**，**12 项测量统计测试通过**。
- 新增七进程轮值回归通过：成员离线后跳过时间片、重启同步、跨选举周期、错误成员拒绝、未来区块到时自动采用、账户状态一致。离线测试的交易在相邻一个时间片后出块，无需等待非成员恢复条件。
- 持续测试使用五个进程、五份独立数据库和身份，最大区块 6000 笔、步长 2000、VDF 迭代 65536、每轮 32 块、时间片 200 ms；每种工作线程数均为 1。

| 指标 | 结果 |
|---|---:|
| 输入速率 | 100 TPS |
| 预热 | 5 秒 |
| 正式测量配置 | 300 秒 |
| 首末测量采样间隔 | 299.875 秒 |
| 发送且确认的交易，包括预热 | 30,500 笔 |
| TPS | 99.98 |
| 确认时间中位数 | 270.20 ms |
| 确认时间 P95 | 376.33 ms |
| 确认时间 P99 | 777.09 ms |
| 一秒内确认的样本比例 | 99.07% |
| 未完成的确认样本 | 0 |
| 测量期间重组，五进程合计 | 36 |
| 每进程测量期间重组 | 7、10、4、8、7 |
| 逐块检查的正常轮值区块 | 1,501 |
| 五个公钥各自出块数 | 299、302、303、298、299 |

全部交易、账户余额、nonce、手续费和共同区块核对通过。此轮完成了 100 TPS 下的持续运行检查；大规模集群的吞吐能力由后续集群测试给出。

五份持续测试数据库已删除，无清理错误。完整回归的数据库也在核对进程、路径和文件锁后清理；保留源码、程序、配置、日志和测试记录。

## 云端状态

- 常驻服务：`chain-vdf-rotation001-node021.service`，已启动并设置开机启动。
- 监听：`0.0.0.0:18089`；链标识 `029d57787c7de024ab413aad075d89fed22c4c4abb9f309520e6c315c62b0e57`。
- 持久数据：`/opt/chain-rotation/persistent/rotation001/node021`。
- 源码：`/opt/chain-vdf/releases/vdf/rotation-001/source`。
- 带调试信息的构建：`/opt/chain-vdf/releases/vdf/rotation-001/build`。
- 部署程序：`/opt/chain-rotation/releases/rotation-001/bin/boost`。
- 原有 `hundred-persistent-01-node021` 服务已停止并取消开机启动，原数据库保留。
- 本机访问公网 `18089` 超时，服务器未收到对应 TCP 请求。主机防火墙已允许该端口，外网连接还需检查云安全组或网络规则；服务器内的服务和查询正常。

本次从工作树打包构建。包中逐文件摘要标识实际源码，`source_commit` 记录构建开始时的基准提交 `014f6e6`。程序 SHA256 为 `b8865698509bb4e164b4c05fcff712d1708f01b2114539d3d9d5124d6c3ffb61`。首次双任务编译触发交换空间读写，随后复用完成的对象，以单任务完成构建；不影响程序运行参数。

证据：[完整构建记录](build-proof.json)、[CTest 输出](ctest-output.txt)、[Python 测试](python-tests.json)、[轮值网络回归](rotation-network-summary.json)、[五分钟结果](five-minute-result.json)、[部署及缓存检查](deployment-and-test.json)、[常驻节点状态](persistent-health.json)。云端完整记录保存在 `/opt/chain-rotation/quick-validation-001`。
