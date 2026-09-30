# FISCO BCOS 多节点性能测试

使用独立测试链，测量十节点共同链的每秒交易数（Transactions Per Second，TPS）。原 Chain 数据目录保持独立。本次数据与结论见 [实验报告](../../reports/2026-09-30-fisco-ten-node/REPORT.md)。

## 文件

| 文件 | 功能 |
|---|---|
| `Deploy.py` | 通过内存中的密码连接十台机器，部署隔离目录；默认不修改 SSH 认证配置 |
| `StableDeploy.py` | 部署 v3.7.3 新链和每节点一个发压进程所需的环境 |
| `Seal200.py` | 本次调整封块间隔的过程记录，包含已使用的用例名称；新一轮实验使用新计划名称 |
| `Distributed.py` | 十台机器共同发压，原子发布开始时刻，合并回执与延迟直方图 |
| `Configure.py`、`Classic.py` | 早期 v3.17.1 探索：对照新版并行执行器与经典执行器 |
| `FiscoBench.java` | 创建账户、预生成签名、限速发送、统计回执、按哈希补查、核对全部账户余额 |
| `Run.py` | 顺序执行实验计划，每秒查询所有节点，记录共同链和运行参数 |
| `NodeSample.py`、`Inspect.py` | 采集进程处理器时间、内存、磁盘写入和网卡计数，汇总各节点数据 |
| `Analyze.py` | 计算共同链吞吐、积压斜率、资源占用和持续负载判据 |
| `Plot.py`、`BuildReport.py` | 从指标生成四张数据图和 Markdown 报告 |
| `BuildTuningReport.py` | 从归档指标重建调优报告、吞吐量、区块参数与处理器占用图 |
| `TestAnalyze.py` | 核对部署计数、积压增长、短暂排队恢复、余额错误和时间窗口 |
| `Tune.py`、`ConfigTx.java` | 调整封块、消息容量与存储参数，提交动态区块上限，采样热点，建立独立单节点对照链 |
| `vendor/` | 固定版本的上游合约包装类及许可证 |

函数沿用 PascalCase。Java 的 `main` 与 SDK 回调保持接口要求的名称。

## 性能调优与单节点对照

本次后续调优见 [瓶颈与复测报告](../../reports/2026-09-30-fisco-tuning/REPORT.md)。`Tune.py` 在控制机交互运行，密码通过隐藏提示输入。使用它之前先启动现有测试节点，并确保所有交易已处理完毕。

```bash
python Tune.py
```

每行输入一个 JSON 操作，等待 `ACTION_DONE` 后再执行下一项。例如：

```json
{"action":"configure","name":"batch500","ini":{"consensus":{"min_seal_time":"500"},"p2p":{"allow_max_msg_size":"134217728","session_recv_buffer_size":"268435456","session_max_read_data_size":"262144"}},"block":50000}
{"action":"sample"}
{"action":"run","case":{"mode":"native","rate":40000,"warm":10,"seconds":60,"users":2048,"phase":"batch500","name":"stable-native-40000-batch500-new","inflight":15000}}
{"action":"stop"}
{"action":"exit"}
```

`configure` 先核对状态，再停止和启动节点。区块上限由 `ConfigTx` 向链上系统合约提交，所有节点查询结果一致后开始实验；这笔配置交易也用于完成重启后的执行器初始化。`recover:true` 支持在已停止发压、交易池清空的情况下调整落后节点的网络参数，要求恢复到原最高交易计数。每组参数保存独立记录。

`inflight` 是每个发送端的在途请求上限；增加它会扩大排队容量。`heap`、`driver_cpus` 可分别设置 Java 最大堆与处理器数。`profile:true` 在控制机节点测量窗口内采集 12 秒 Linux perf 样本，正式吞吐复测省略此项。调整 `min_seal_time` 时同时查看测得的实际平均区块间隔、每块交易数和回执延迟。

支持以下环境变量，默认仍使用原十节点环境：

- `FISCO_BENCH_ROOT`：本次服务器实验目录。
- `FISCO_BENCH_HOSTS`：参与共识和状态核对的节点地址，用逗号分隔。
- `FISCO_BENCH_SENDERS`：运行发送端的机器地址，用逗号分隔；默认与共识节点相同。

单节点对照使用同级新目录、一个共识节点和原十台发送端。先停止原十节点链，在控制机新目录下创建指向原 `nodes` 目录的符号链接以读取测试证书，然后设置上述环境变量并执行 `{"action":"single"}`。此操作复制配置与程序、建立空数据库，原数据库保留。再通过 `configure` 设置区块上限、通过 `sample` 启动资源采样，按相同方式发压。报告分别标明节点数量、发送端数量和数据库历史规模。

`Analyze.py` 自动从实验前的节点清单确定节点数量，同时统计实际平均区块间隔和每块交易数。`NodeSample.py` 额外记录按线程名称汇总的处理器时间。准备失败与最终节点查询失败的用例保留诊断和回执数据。

## 固定版本与依赖

- 正式测试 FISCO BCOS：`v3.7.3`，Linux x86-64 Release。官方仓库列为生产环境稳定版本。
- Java SDK：`3.7.0`；合约来自官方 `java-sdk-demo v3.10.0`。
- 每台机器：Java 17、Python 3、官方原生节点。控制机额外使用 Maven、Paramiko。
- v3.7.3 压缩包 SHA-256：`b2b0121722a612ded5ccf4209bf88d12f224f4ebc75a5f2d9bafeac898b27d89`。
- v3.7.3 二进制 SHA-256：`7c29bd2afe615a57af02a440fc4d665c3beec3cf7301ec4007cc07caf5b683f4`。
- 节点源码提交：`5811f123b0a82928de8ec662e84763d67c16fb1e`。

下载 [v3.7.3 官方发布包](https://github.com/FISCO-BCOS/FISCO-BCOS/releases/tag/v3.7.3)，记录并核对传输前后的 SHA-256。此旧版发布资产未附 GitHub digest，本次校验值用于文件传输核对。版本选择见[官方版本说明](https://github.com/FISCO-BCOS/FISCO-BCOS#版本信息)。本次固定使用十个 PBFT 共识节点，PBFT 全称为 Practical Byzantine Fault Tolerance，实用拜占庭容错。

## 复测现有稳定版环境

所有路径位于控制机 `/home/ubuntu/fisco-bench/20260930`。十台机器的稳定节点位于 `runtime-stable/node0`，发压程序位于 `driver-stable`。测试结束节点已停止，数据库保留。重新测试前，在每台机器执行 `bash runtime-stable/node0/start.sh`，移除本目录的 `sample.stop`，启动 `python3 NodeSample.py`。控制机用 `python Run.py --check` 确认十节点一致，再执行：

```bash
python Distributed.py plan.json
python Inspect.py --stop
python Analyze.py . RESULTS.json
```

使用带 Paramiko 的 Python 环境，交互输入服务器密码。每个用例名称必须唯一。`rate` 为十个发送端的合计速率，能被十整除；`users` 为每台发送端的账户数量。

```json
[
  {"mode":"native", "rate":20000, "warm":10, "seconds":60,
   "users":2048, "name":"stable-native-20000-new-r1"}
]
```

`mode` 可选 `solidity` 或 `native`。每台发送端只连接本机节点，使用 2 个 SDK 回调线程、4 个发送线程、2 个 Java 可用处理器，最大堆 2 GiB，最多约 10,000 笔在途请求。十台发压准备完成后，统一开始计时。各端先写临时开始时间文件，再原子改名，避免读取未写完的时间戳。每轮保存实际开始时间差。

`solidity` 每台发送端部署一个 ParallelOk 合约，总计 10 个；`native` 共用预编译转账合约，每台使用独立账户名前缀。签名、部署和账户初始化均在计时前完成，结束后逐一核对共 20,480 个账户。

原报告的持续测试将 `runtime-stable/node0/config.ini` 中的 `consensus.min_seal_time` 设为 `200`，阶段记为 `seal200`。前期扫描为 `100`。后续调优报告使用 `500` 和每块最多 50,000 笔，阶段为 `final-batch500`。预签名交易在随后 500 个区块内有效；本次 70 秒发压的区块增长保持在有效期内。延长测试时间时需使用滚动生成签名的发送方式，同时记录发送端处理器开销。

正式用例的 `phase` 字段记录配置阶段，默认 `initial`。`Distributed.py` 在至少 60 秒的用例结束后自动检查持续负载条件；未通过时结束当前计划，保留全部数据。三轮统计在相同业务、速率和阶段中分组。

## 本次部署过程记录

以下记录保留了 v3.17.1 探索环境的搭建步骤。正式稳定版从其节点证书和配置模板建立独立数据库，具体见本节末尾的 `StableDeploy.py` 步骤。

1. 在 `Deploy.py` 中设置测试目录 `ROOT` 和机器列表 `HOSTS`。本次目录为 `/home/ubuntu/fisco-bench/20260930`，控制机内网地址为 `10.206.0.2`。更换目录时同步调整 `NodeSample.py` 中的 `ROOT` 和 Java SDK 配置中的证书路径。
2. 使用官方建链脚本生成每台一个节点：`-l "IP1:1,IP2:1,..." -p 30300,20200 -v v3.17.1 -T pbft -R false -g group0 -I chain0`。指定 `-e` 为已经校验的二进制，`-o` 为 `ROOT/nodes`。
3. 将官方二进制压缩包放到 `ROOT/downloads/fisco-bcos-upload.tar.gz`，将本目录 Python 脚本放到 `ROOT`。运行 `Deploy.py`，在交互提示中输入 SSH 密码。证书、账户私钥和运行数据库仅保存在测试机。
4. `Deploy.py` 配置单块最多 10,000 笔、最小出块间隔 100 毫秒、交易池 200,000 笔、8 个验签线程、4 个 RPC 线程。RPC 全称为 Remote Procedure Call，远程过程调用。TLS（Transport Layer Security，传输层安全协议）保持启用，节点绑定内网地址。
5. 新版执行器使用创世配置 `executor.version=1`。`baseline_scheduler_parallel` 控制其并行开关；`is_serial_execute=false` 控制经典执行路径。运行 `Classic.py` 可停止旧链，并在 `runtime-classic` 中启动 `executor.version=0` 的全新链，旧链数据库保留。
6. 在 `ROOT/driver` 中放入 `FiscoBench.java` 和 `pom.xml`，创建 `classes`、`lib`，将两个包装类放到 `src/org/fisco/bcos/sdk/demo/contract/`。运行：

```bash
mvn -B -ntp org.apache.maven.plugins:maven-dependency-plugin:3.6.1:copy-dependencies -DoutputDirectory=lib
javac -proc:none -cp 'lib/*' -d classes src/org/fisco/bcos/sdk/demo/contract/*.java FiscoBench.java
```

`-proc:none` 使编译只处理这三个 Java 源文件。示例 SDK 配置保存为 `ROOT/driver/config.toml`：

```toml
[cryptoMaterial]
certPath = "/home/ubuntu/fisco-bench/20260930/nodes/10.206.0.2/sdk"
disableSsl = "false"
useSMCrypto = "false"
[network]
peers = ["IP1:20200", "IP2:20200", "IP3:20200"] # 填写全部十个节点
sendRpcRequestToHighestBlockNode = "false"
messageTimeout = "120000"
[account]
keyStoreDir = "/home/ubuntu/fisco-bench/20260930/driver/account"
[threadPool]
threadPoolSize = "8"
```

7. 启动采样：`python Run.py --sample`。实验计划采用如下 JSON；`name` 使用新名称，已有结果目录会拒绝覆盖。经典执行器用例名称以 `classic-` 开头，以便记录对应配置。

```json
[
  {"mode":"native", "rate":18000, "warm":10, "seconds":60,
   "users":16384, "name":"classic-native-18000-r1"}
]
```

运行 `python Run.py --plan plan.json`。`mode` 可取 `solidity` 或 `native`。`users` 为偶数，转账在相邻账户之间交替进行，金额为 1，初始余额为 1,000,000,000。每轮账户名称和合约实例独立。

## 统计口径

- 签名在计时前生成。发送端最多保留约 100,000 笔未完成请求，同时记录目标速率和实际发送速率。
- 每秒查询所有节点，取已提交交易计数的最小值。使用测量区间内首末两个完整观测的差值除以观测间隔，得到共同链吞吐量。
- 交易回执同时检查执行状态与预编译返回值；异步回执遗漏或超时时，按预先保存的交易哈希查询链上回执。查询结果需匹配哈希且执行成功，才计入成功交易。
- 延迟从实际发送开始，到取得成功回执结束；补查包含等待时间。另保存从计划发送时刻开始的 99% 分位延迟。
- 结束后核对十节点同高度区块哈希、状态根、累计交易数、共识节点数量，并逐一核对本轮所有账户余额。计数增量应等于成功转账、账户初始化和合约部署之和。
- 持续负载判据：正式测量至少 60 秒；实际发送与共同链速度达到目标的 95%；交易全部确认；执行失败为零；最终状态和计数核对通过；交易池清空；每节点平均积压的首末净增长率不超过 `max(10, 目标速率 × 1%)` 笔/秒。线性拟合斜率同时保留，帮助观察中途波峰。最终判据版本为 `net-backlog-v2`，原斜率判据结果见 `slope_acceptance_passed`。
- 同时查看回执延迟和补查数量。较高的 TPS 可伴随较长的回执等待时间。

稳定版部署：将 v3.7.3 包放入 `downloads/fisco-bcos-stable.tar.gz`；在 `driver/conf` 中放入建链生成的 SDK 证书，打包 `classes lib conf FiscoBench.java pom.xml` 为 `driver-package.tar.gz`。运行 `StableDeploy.py`，脚本建立 `runtime-stable` 与 `driver-stable`，将兼容版本设为 3.7.3、执行器版本设为 0，并在各机器安装 Java 运行时。既有的测试目录会拒绝覆盖。打包文件含 SDK 私钥，仅在服务器之间分发。

原始结果导出后运行 `python Analyze.py RAW_DIRECTORY RESULTS.json`。`RAW_DIRECTORY` 包含 `results` 和 `collected`。`Inspect.py --stop` 只停止本次测试目录内的节点、FiscoBench 发压进程和采样器，并收集资源与退出状态。数据库和原始结果保留。

正式测试延迟由各端 1 毫秒直方图合并，分位数最多向上取整 1 毫秒。共同链窗口取十个发送端实际计时区间的交集。账户初始化和 10 次 Solidity 部署均纳入结束后的总计数核对，并从吞吐计时中排除。
