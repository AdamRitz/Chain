# 五组多节点实验

统一入口是 `Test/Experiments.py`，实验参数在 `Experiments/plan.json`。节点从清单读取，可以配置 2～512 个节点；目前验收使用本机 2、3 节点。十台服务器及更大规模的运行结果由部署后的实验生成。

## 实验内容

| 实验 | 改变的条件 | 主要输出 |
|---|---|---|
| 时间同步 `sync` | 逻辑时钟初始偏差、漂移，关闭/开启校正 | 节点间时钟差、收敛时间、探测误差范围 |
| 实时性 `latency` | 低延迟/大吞吐两种凑块模式，平稳/突发交易 | 第 50、95、99 百分位延迟，截止时间达标率 |
| 吞吐量 `throughput` | 逐级增加发送速率，两种凑块模式 | 共同链落盘速度、实际发送速度、积压和资源使用 |
| 节点规模 `scale` | 默认 2、4、6、8、10 节点，各组使用相同负载 | 横轴节点数、纵轴每秒交易数；扫描中最高可持续负载 |
| 故障恢复 `fault` | 延迟、丢包、两组网络隔离、末尾节点离线 | 共同链进度、积压、恢复时间、重组次数和深度 |

所有节点均参与出块。低延迟模式等待 10 毫秒、每块最多 512 笔；大吞吐模式等待 50 毫秒、每块最多 6000 笔。满块可提前生成。每个用例使用相同的签名数据、创世资金和独立的新数据库，并开启同步落盘。

五组实验使用原生账户转账。已有的 `Test/BenchCores.py` 和 `Test/PlotCores.py` 可继续测量原生转账、以太坊虚拟机（Ethereum Virtual Machine，EVM）合约调用随核心数的变化，现已适配 Linux 可执行文件名与核心拓扑读取。

## 本机快速验收

先构建项目并安装 Python 依赖：

```powershell
python -m pip install -r Experiments/requirements.txt
python Test/Experiments.py --local 3 --bin build --plan Experiments/smoke.json --work experiment-results/smoke-001
python Test/PlotExperiments.py --work experiment-results/smoke-001
```

在本机已有多个 Python 时，使用装有 `psutil`、`matplotlib` 的解释器完整路径。Windows 节点运行时，将对应 MinGW 动态库目录加入 `PATH`。

快速配置包含 11 个用例，覆盖五组实验的主流程；故障用例执行进程离线和重启。Linux 网络故障另用正式配置执行。`--work` 必须是新目录，末级目录名使用英文字母、数字、短横线、下划线，长度最多 60 个字符。原始结果和数据库均保留在该目录或远端清单指定的 `root` 下。

## Ubuntu 24.04 构建

在每台服务器的代码根目录运行：

```bash
bash scripts/Build-Linux.sh --install
```

脚本安装系统依赖，建立 `.venv`，构建 Release 版本并运行八项回归测试。系统依赖装好后可省略 `--install`。默认使用四个编译任务，`CHAIN_BUILD_JOBS=8 bash scripts/Build-Linux.sh` 可以调整。项目可选用 `-DCHAIN_BOOST_DIR=/path/to/boost` 指定 Boost。

测试包括原有七项功能回归，以及新增的 `ExperimentNetwork`：并行目录创建、网络扰动规则范围、动态节点清单、交易高度索引回滚/重启、同步轮次下交易执行。需要重新运行全部测试时，为 `CHAIN_TEST_DATA_ROOT` 指定新路径；构建脚本会自动设置。

2026-09-30 已在 Ubuntu 24.04.4 / GCC 13.3 / RocksDB 8.9.1 上完成 Release 构建，八项 CTest 全部通过。十台 8 核、16 GiB 云服务器已通过内网部署与十二项短时验收，覆盖五组实验，其中故障组验证进程离线和重启。数据库打开接口兼容 Ubuntu 自带的 RocksDB 8.9，同时保留智能指针管理对象生命周期。

后续完成 54 个重复实验用例：两种模式均在每秒 5,000 笔档位三次通过持续负载判据；每秒 1,000 笔、节点离线 2 秒的恢复用例三次均未收敛。完整数据与图表见 [十节点云服务器实验记录](reports/2026-09-30-cloud/REPORT.md)。

## 十台服务器清单

复制 `Experiments/cluster.example.json` 为 `Experiments/cluster.local.json`，填写实际内网地址、远程登录地址、文件路径和网卡。示例中的 `10.0.0.11`～`10.0.0.20` 为占位地址。

```json
{
  "defaults": {
    "transport": "ssh",
    "binary": "/opt/chain/build/boost",
    "worker": "/opt/chain/Test/ClusterWorker.py",
    "python": "/opt/chain/.venv/bin/python",
    "root": "/var/tmp/chain-experiments",
    "port": 8089,
    "interface": "eth0",
    "allow_netem": false,
    "sudo_netem": true
  },
  "nodes": [
    {"id": "node01", "host": "10.0.0.11", "ssh": "ubuntu@10.0.0.11"},
    {"id": "node02", "host": "10.0.0.12", "ssh": "ubuntu@10.0.0.12"}
  ]
}
```

`host` 是控制机和其他节点均可访问的链节点 IPv4 地址；`ssh` 是安全外壳协议（Secure Shell，SSH）登录目标，可单独使用公网或管理网地址。可选字段为 `ssh_port`、控制机上的 `ssh_key` 路径、监听用的 `bind`，以及覆盖节点参数的 `options`。私钥保留在控制机，清单只填写文件路径。

控制机使用免交互的 SSH 登录，并能访问每个节点的 8089 端口。所有机器提前放好同一版本代码和可执行程序；工作进程按 JSON 请求启动节点、停止节点和收集结果。每次启动记录二进制 SHA-256 摘要、系统、内存、处理器数量和完整运行命令。

增加节点只需扩展 `nodes` 列表，并在计划的 `node_counts` 中增加实验规模，例如 `[2,4,6,8,10,16,20]`。程序自动生成连接列表和连接容量，节点之间采用全连接。节点数增加时，每个节点的广播量随其他节点数增长，扩大规模前同时观察流量、排队和丢包。

## 运行与出图

在有节点内网连通性的控制机上执行：

```bash
.venv/bin/python Test/Experiments.py --inventory Experiments/cluster.local.json --bin build --suite throughput --work experiment-results/throughput-001 --dry-run
.venv/bin/python Test/Experiments.py --inventory Experiments/cluster.local.json --bin build --suite throughput --work experiment-results/throughput-001
.venv/bin/python Test/PlotExperiments.py --work experiment-results/throughput-001
```

将 `--suite` 改为 `sync`、`latency`、`scale`、`fault` 或 `all` 即可切换。`--dry-run` 校验配置并输出节点和用例；正式运行会保存配置副本和随机排列后的用例顺序。失败默认停止，`--keep-going` 保留失败记录并继续其他用例。

正式计划默认重复五次、预热 15 秒、测量 60 秒，最多等待 60 秒清空积压。十节点全部计划共 205 个用例，纯预热与测量约四小时，另计启动、数据准备和积压清理。建议先逐组运行，再根据饱和位置细化发送速率。每一轮完整运行使用新的目录名。

默认最大数据集为 1125 万笔，每笔 176 字节，约 1.98 GB。数据集由控制机逐笔生成并通过内存映射发送。各节点数据库另外占用磁盘，清单中的远端目录保留所有用例数据，长批次要预留相应空间。

结果文件：

| 文件 | 内容 |
|---|---|
| `plan.json`、`inventory.json`、`cases.json` | 本次参数、节点、运行顺序 |
| `environment.json` | 控制机环境、发送端摘要、数据集大小 |
| `results.json`、`summary.csv` | 全部用例状态和指标 |
| 用例目录中的 `machines.json` | 节点程序摘要、进程与机器信息 |
| `timeline.jsonl` | 每次采样的共同前缀、队列、资源、链头 |
| `clock.json` | 时钟差及探测往返时间 |
| `latency-samples.json`、`events.json` | 交易抽样时间及故障时刻 |
| `node*.json` | 远端进程记录、退出统计和日志末尾 |
| `cleanup-errors.json` | 需要继续处理的退出或网络清理失败 |
| `figures/` | PNG 位图、SVG 矢量图和图表口径 |

图表覆盖时钟同步、延迟、吞吐量、节点规模、故障恢复，并附资源图。曲线采用重复实验的中位数，阴影为实际最小值至最大值。缺少某组数据时跳过该组图；单次运行只显示测量值。

## 计量口径

每秒交易数（Transactions Per Second，TPS）采用全部在线实验节点一致的已落盘共同链前缀，按累计唯一交易数的增量除以采样间隔。分叉上的重复提交、广播副本与回滚后重新执行分别由原始计数器记录。节点离线期间，全节点共同前缀观察值留空；节点恢复后继续核对。

延迟从控制机计划产生该笔交易的单调时钟时刻，计算到所有节点首次查询到相同入块位置的时刻。它包含发送排队、网络、执行、落盘和采样等待。额外保存实际发包后的延迟、发送迟到量、未完成样本数量。默认每秒抽样约 20 笔，正式轮询间隔 10 毫秒；实际轮询周期还包含查询处理时间，可从时间线核对。需要比较更小延迟时，调整轮询与批量发送参数，并对比采样开销。

所有节点的查询并行发出，网络传输仍会产生观察时间差。共同前缀与入块延迟描述本次观察窗口内的一致性，后续分叉可能改变确认位置。最终清空积压后，独立计算所有测试账户的余额和序号，核对每个节点、手续费、唯一交易数、链头以及原生和合约状态摘要。

可持续标记要求：实际发送速度和共同链落盘速度都达到目标的 95%，交易状态核对通过、拒绝与非法计数为零、抽样全部完成、积压增长低于 `max_backlog_growth`。积压增长采用测量窗口内全部采样点的线性回归斜率，单位为各节点排队副本合计每秒增长量。节点规模图显示扫描中通过这些条件的最高实测速率；接近饱和处增加档位可以缩小测量范围。`status=complete` 表示流程结束，`drained` 和 `sustainable` 分别表示状态核对和负载条件是否通过。

低延迟组的 `latency_batch=8`，吞吐量组的 `batch=128`。突发流量每秒集中在前四分之一秒到达，平均目标速率与平稳流量相同。运行完整测量窗口后再清理积压。输入模式 `broadcast` 将相同交易发送给所有节点；`shard` 按账户分发给一个入口并启用节点转发。网络分区用例固定使用 `shard`，让两侧自然形成不同交易视图。

中央处理器（Central Processing Unit，CPU）的平均忙碌核心数计算为进程 CPU 秒数增量除以墙钟测量秒数。例如 6.4 表示平均占用约 6.4 个逻辑处理器。资源结果同时包含驻留内存、应用层收发字节、进程写入量和数据库写入耗时。Linux 写入量来自 `/proc/self/io`；Windows 进程输入输出计数也包括其他设备操作，解释磁盘瓶颈时结合主机磁盘观测。应用层网络字节不包含协议头和重传。

先观察 `achieved_send_tps` 和 `send_lateness_p99_ms` 判断负载是否到达节点，再结合忙碌核心数、排队、网络流量和数据库耗时定位瓶颈。控制机使用单发送线程，`broadcast` 的总发包量随节点数增长；接近控制机上限时可测试 `shard`，并将两种输入方式分别报告。控制机 CPU 使用另存为 `controller_cpu_core_equivalents`。

## 时间同步与轮次

当前新增同步器为“四时间戳测偏差＋邻居中位数校正”，标识为 `four_timestamp_median_coupling`，可作为萤火虫同步研究的测量基线。接口位于 `Time/Time.h` 的 `RecordClockSample` 和 `AdjustClock`，后续可以在这里接入具体的萤火虫相位响应公式。

逻辑时钟由启动墙钟、单调经过时间、注入偏差、频率漂移和校正量组成。四个时间戳计算邻居偏差，每个邻居只保留最新样本，超过四个采样周期失效。节点加入自身零偏差后取中位数，乘以增益并限幅后应用。收敛指标采用连续五次观测低于门限的第一时刻，同时给出探测半往返时间作为误差尺度。

| 参数 | 功能 |
|---|---|
| `--clock-sync 0/1` | 探测与统计；值为 1 时应用校正 |
| `--clock-observe 0/1` | 仅控制是否启动周期探测 |
| `--clock-offset-ms` | 注入初始时钟偏差，单位毫秒 |
| `--clock-drift-ppm` | 注入频率漂移，百万分率（Parts Per Million，PPM） |
| `--sync-period-ms`、`--sync-gain` | 探测周期和校正增益 |
| `--round-ms` | 按逻辑时钟对齐候选生成时刻，0 为原有凑块流程 |
| `--candidate-ms` | 候选收集时间，默认轮长的四分之一且至少 1 毫秒 |

正式计划默认 `round_ms=0`，吞吐量和实时性组复用当前出块方式。研究同步对出块的影响时，保存两份计划，固定 `round_ms` 为相同值，例如 50，分别设置 `clock_sync=false/true`。偏差和漂移注入目前用于 `sync` 组；带负载的偏差实验可通过各节点 `options` 配置，但清单自动生成的 `clock-offset-ms`、`clock-drift-ppm` 会以 `offsets_ms`、`drifts_ppm` 数组为准。例如 `"options":{"offsets_ms":[-40,40],"drifts_ppm":[-20,20]}`。

轮次开关控制候选生成和收集，分支选择仍按现有累计有效交易数、高度及哈希比较。当前结果用于衡量同步和调度效果，最终确认协议、身份权重与恶意时钟处理属于后续协议研究。

## 网络扰动与清理

Linux 网络仿真（Network Emulator，netem）通过流量控制工具（Traffic Control，tc）在接收端入口施加延迟或丢包。程序创建独立的中间功能块设备（Intermediate Functional Block，IFB），仅重定向“实验对端地址＋链端口”匹配的传输控制协议（Transmission Control Protocol，TCP）流量。两组隔离采用组间 100% 丢包。

选好实验网卡后，将清单 `allow_netem` 设为 `true`。`sudo_netem=true` 使用 `sudo -n`，执行账号需要已配置的 `ip`、`tc` 权限。控制机建议使用独立管理地址，确保控制与观测连接不落入节点间扰动规则。脚本检测已有入口规则和优先级冲突，每个用例结束删除自己的过滤器与设备，保留共享的 `clsact` 队列结构。

异常退出时，按对应节点的真实目录手动恢复：

```bash
printf '%s' '{"action":"clear-fault","root":"/var/tmp/chain-experiments","run":"替换为本次运行ID","node":"node01"}' | /opt/chain/.venv/bin/python /opt/chain/Test/ClusterWorker.py
```

同样可把 `action` 改为 `stop` 停止对应节点。程序按进程编号、创建时间和可执行文件共同验证归属。清理失败记录保留以便继续处理。若控制机被强制结束，远端节点仍会根据 `--run-seconds` 到期退出，网络扰动租约需要执行上述清理。

故障施加和撤销在节点之间逐个执行，时间线记录操作完成时刻。恢复时间从开始执行恢复操作，计到连续三次观测到全部节点链头一致的首个时刻，包含节点启动或撤销网络规则的时间；开始时刻另存为 `recovery_started_seconds`。默认分区持续 3 秒；当前分叉恢复窗口为 256 块，扩大隔离时长时一并核对分叉深度。

## 后续修改入口

`Experiments.py` 负责负载、采样、核对和实验矩阵；`Cluster.py` 负责节点清单、连接和生命周期；`ClusterWorker.py` 负责本地或远程进程操作；`Netem.py` 负责有范围的内核扰动；`PlotExperiments.py` 负责出图。模块之间直接传递字典与文件路径。

交易确认观测通过可选 `--experiment-index 1` 写入 `txheight/<hash>`。索引和区块一起原子提交，随分叉回滚恢复。所有对比组使用相同设置。已有数据库开启该开关后，只为新提交交易建立索引，正式测量始终使用新数据库。批量观测协议和锁规则见 `MODULE_GUIDE.md`。
