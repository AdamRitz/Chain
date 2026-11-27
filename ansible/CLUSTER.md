# 1000 个以上节点的部署和测试

新入口使用一个已经通过完整测试的 Linux 构建包。控制机分发相同二进制和源码；目标节点不用安装 C++ 编译器。编译和 GDB 调试仍在构建机进行。生产数据库、节点身份、隔离测试数据库分别由部署记录管理。

## 百节点按小时计费的准备顺序

4 核机器的 100 节点测试，在现有 `node-settings.json` 中设置 `io-threads=2`、`verify-threads=2`、`candidate-threads=2`、`block-verify-threads=2`、`query-threads=1`，通过 `--node-settings` 传入。其余区块、时钟和验证参数保持原配置。2026-11-26 的 30 秒、9000 TPS 测试使用该线程配置后重组为 0；确认时间 P95 为 1104 ms，仍需优化。直接运行节点的默认线程数与压测脚本的默认线程数不同，复测时应保留最终 argv。

查找吞吐量上限时使用 `--duration 30 --warmup 2 --drain-seconds 5`，每个速率使用新输出目录。先核对实际发送速率、确认 TPS、积压变化、确认时间、账户和清理结果，再提高下一轮速率；测量结束前关机的轮次记录为中断。关机导致 SSH 不可达时保留当前 run/owner；机器重开后先按原记录完成清理，再开始新轮次。

1. 在 Windows 执行 `cluster_quick.py prepare`，提前准备连接图、签名交易和可跨系统使用的 `dataset.json`。该动作在本地完成，无需构建包、Ansible 或 SSH。
2. 先只启动一台 Linux 构建机，完成本次源码构建、全部 CTest 和 `cluster_release.py` 打包。同一版本已有构建目录可以增量重试，新源码使用新的 release 标识。
3. 构建包准备完成后启动其余 99 台，使用 `cluster_quick.py stage --forks 32` 并行分发同一份程序和源码。其余节点无需安装编译器或重复运行 CTest。
4. 使用 `cluster_quick.py test --staged` 校验已安装程序，再开始 300 秒测试。通过 `--dataset`、`--dataset-genesis`、`--dataset-manifest` 复用 Windows 准备的数据，无需重新签名或再次分发程序包。
5. 测试结束自动停止节点、收集记录并清理本轮数据库。核对清理结果后关闭服务器；此流程使用 `stage/test`。

离线准备示例：

```powershell
python -B ansible/scripts/cluster_quick.py prepare `
  --nodes D:/ChainTest/nodes.json --cluster continuous32-100 --port 28089 `
  --degree 8 --seed 1 --injectors 4 `
  --dataset D:/ChainTest/transactions.bin `
  --dataset-genesis D:/ChainTest/transactions.bin.genesis.json `
  --node-settings D:/ChainTest/node-settings.json `
  --rate 10000 --warmup 2 --duration 300 --wallets 1024 `
  --output D:/ChainTest/offline-prepare-001
```

数据正文只上传到 Linux 控制机，再通过四个注入节点发送。10,000 TPS、预热 2 秒、测量 300 秒需要 3,020,000 笔交易，即 531,520,000 字节。提前准备更多交易后可复用同一文件的前缀。

默认早停规则：正式测量满 30 秒后，连续三个至少 15 秒的窗口内 TPS 低于输入目标的 70%，且每个窗口积压增加，则停止发送并执行收集和清理。可通过 `--early-stop-ratio`、`--early-stop-start-seconds`、`--early-stop-window-seconds`、`--early-stop-windows` 调整；`--no-early-stop` 关闭早停。

100 台、degree 8 的普通连接共 400 条；出块者另外保持五人直连。已有同一计划时使用 `--plan`，连接图和测试端口保持一致。SSH 凭据使用独立 inventory、Ansible Vault 或 `--extra-vars-file`。

## 快速入口：部署和五分钟测试

准备下面说明的构建包和 `nodes.json` 后，可以直接使用 `cluster_quick.py`。`deploy` 只创建持久服务，`test` 只运行独立测试，`stage` 只分发包。三个动作都复用现有 Ansible playbook，每台节点不重新编译。目标机缓存的包 SHA256 相同时跳过复制；已经发布的构建继续校验文件内容。控制机每次只计算一次包 SHA256。

```bash
# 部署：原样使用这份创世配置，重复执行不会改写数据库、身份或链参数。
python3 -B ansible/scripts/cluster_quick.py deploy \
  --nodes /srv/chain/nodes.json --cluster rotation01 \
  --release-id rotation01 --package /srv/chain/rotation01.tar.gz \
  --package-sha256 PACKAGE_SHA256 --genesis /srv/chain/rotation-genesis.json \
  --output /srv/chain/deploy-rotation01 --python /usr/bin/python3.11 --forks 32

# 测试：正式测量默认 300 秒，另加预热 2 秒、等待剩余交易 5 秒。
python3 -B ansible/scripts/cluster_quick.py test \
  --nodes /srv/chain/nodes.json --cluster rotation-test --port 28089 \
  --release-id rotation01 --package /srv/chain/rotation01.tar.gz \
  --package-sha256 PACKAGE_SHA256 --sender /opt/chain/releases/rotation01/bin/sender \
  --rate 10000 --output /srv/chain/results/rotation-test01 \
  --cache /srv/chain/datasets --python /usr/bin/python3.11 --forks 32
```

将 `PACKAGE_SHA256` 替换为打包结果。控制机的 `sender` 必须与包中 `bin/sender` 的 SHA256 相同。可以将 `--nodes` 换成已有的 `--plan /absolute/plan.json`；已有计划的端口不会被改写。额外 SSH inventory 用重复的 `--inventory`，连接参数文件用 `--extra-vars-file`；密码不写入生成的节点计划和实验变量。

测试数据按 `ceil(rate × (warmup + duration))` 自动生成。例如 20,000 TPS、预热 2 秒、正式测量 300 秒，需要 **6,040,000 笔，1,063,040,000 字节**原生交易正文。数据仅保存在控制机，按 `sender` 摘要、账户数和交易数缓存；重复测试校验缓存后直接复用，不再次签名，不向每个节点分发交易文件。各轮只生成新的创世文件和结果目录。

新测试创世参数明确设置 `vdf.producer_rotation=true`、`epoch_blocks=160`、`consecutive_blocks=32`、`producer_timeout_slots=8`、`slot_ms=200`、`iterations=65536`，时间起点以 200 ms 对齐。一次选最多五人，依次每人连续生成 32 个实际区块；连续 8 个完整时间片没有新区块后交给下一人。父块槽为 `p` 且当前成员未出满 32 块时，`p+1..p+8` 仍归该成员，`p+9` 起下一位接管。顺序编号和已出块数随区块保存，同步及重启从父块恢复。

使用 `--consecutive-blocks N`、`--producer-timeout-slots T` 和 `--epoch-blocks E` 调整新测试链，要求 `N` 在 1..819、`T` 在 3..65536、`E=5*N`。省略 `--epoch-blocks` 时自动采用 `5*N`。例如每人 16 块、停块 4 个周期后接管：`--consecutive-blocks 16 --producer-timeout-slots 4 --epoch-blocks 80`。`deploy --genesis` 保留提供的创世参数；新建持久链时在创世文件中设置以上字段。已有链省略 `consecutive_blocks` 时继续使用原有规则。

`--duration`、`--warmup`、`--drain-seconds` 分别设置正式测量、预热和停止发送后的等待时间。启动连接、形成五人名单、最后账户核对和清理时间单独计入命令运行时间。默认 `--query-workers 32`、`--forks 32`，允许范围 1..128。1000 节点使用同一入口，先根据控制机查询耗时调整并发，不能只提高线程数而不记录查询开销。

新轮值链默认只让第一台节点在前两个选举周期出块，其他节点带 `--vdf-bootstrap-produce 0` 启动，但保持 `--produce 1`，正常创建身份、计算和传播 VDF。第三个周期起自动按照名单轮值，无需停止或重启节点。`--bootstrap-producers` 可以调整最初出块的节点数，设为 0 则保留所有节点一起启动的方式。

测试只有在所有节点完成前两个选举周期、五人名单可用、同一周期名单一致且具有共同链时才开始发送；少于五个节点的功能测试要求所有节点均进入名单。跨周期采样分别检查各周期名单，避免正常换届被当作启动失败。每轮输出必须使用新目录，数据和日志保存、进程停止与测试数据库清理沿用下文流程。

## 1. 固定连接数量

准备 `nodes.json`，每条记录对应一台真实机器：

```json
[
  {"id":"node0001","host":"192.168.0.11","ansible_host":"198.51.100.11","ansible_user":"root"},
  {"id":"node0002","host":"192.168.0.12","ansible_host":"198.51.100.12","ansible_user":"root"}
]
```

`host` 是节点相互连接的地址，`ansible_host` 是 SSH 地址。两者支持 IPv4/IPv6。查询默认使用 `host`，也可设置 `query_host`；运行测试的控制机需要能连接该地址。需要 SSH 跳板时，在独立 Ansible inventory/group_vars 中配置 `ansible_ssh_common_args`。密码使用 Ansible Vault 或 SSH 密钥，不写入节点 JSON。

```bash
python3 -B ansible/scripts/cluster_plan.py --nodes /srv/chain/nodes.json \
  --cluster experiment01 --degree 8 --seed 1 --injectors 2 --output /srv/chain/plan01
```

输出 `inventory.json` 和 `plan.json`。节点数支持 2..16384；度数支持 2、4、6、8、12、16、32、64。节点较少时采用全连接。固定图包含一条覆盖所有节点的环及若干长距离连接，seed 固定后可以重建同一张图。每条无向边只由一端发起 TCP 连接，连接双向传输。关闭自动发现，节点重连只访问配置的地址。

每个节点另外保存最多与度数相同数量的备用地址，排除自身和原图邻居，固定 seed 后可以重建同一份地址列表。部署配置写入 `node.target_peers` 和 `node.backup_peers`；压测通过 `--target-peers` 和重复的 `--backup-seed` 使用相同设置。正常运行仍使用原图；邻居数量不足持续 5 秒后，节点才尝试备用地址，恢复到目标数量后停止主动补充。入站连接受 `max-peers` 限制。小型全连接集群没有备用节点。已有计划使用新版本 `cluster_plan.py` 重新生成；已有持久服务的配置不会被二进制升级覆盖。

1024 个节点、度数 8 时共有 4096 条连接；每个节点维持 8 个邻居。`max-peers` 为度数加 4，`max-connections` 为度数加 36。`plan.json` 保存全部边、连通性、度数和首个节点到其他节点的最大距离。图生成和校验不启动服务器，不提供 TPS 结果。

## 2. 在一台机器构建并分发

先按 `README.md` 的 VDF 构建步骤，仅在构建机运行 `vdf_prepare.yml`。构建目录必须具有成功且 `complete=true` 的 `build-proof.json`。打包会重新检查源码和二进制摘要。

```bash
python3 -B ansible/scripts/cluster_release.py \
  --release-dir /opt/chain-vdf/releases/vdf/propagation01 \
  --output /srv/chain/propagation01.tar.gz
```

如果节点缺少共享库，可用多个 `--library /absolute/path/libNAME.so.VERSION` 将普通库文件放入包中的 `lib/`；文件名要与运行时 SONAME 对应。不要打包 glibc 或动态加载器。目标系统和架构应与构建机匹配；`stage.yml` 在每台节点执行 `ldd`，缺库时停止部署。现有构建生成的调试信息和源码保留在包内。包中包含 `boost`、`sender`、构建中存在的 `evm_sender`；完整 GUI 构建还必须包含 Linux 网关 `chain_classroom`，源码和前端资产一并保留。

百节点可以显式使用 `--runtime-dir /opt/chain-hundred-20261125/runtime-libraries` 自动收集运行库。打包前对所有程序运行 `ldd -r`，检查 ELF64 x86-64 和 GLIBC 不超过 2.34；非 glibc 依赖必须按 SONAME 从指定目录的普通文件解析，传递依赖一并打包。旧的 `--library` 手动方式继续可用。

创建 `/srv/chain/cluster-vars.json`：

```json
{
  "chain_release_id": "propagation01",
  "chain_package": "/srv/chain/propagation01.tar.gz",
  "chain_package_sha256": "填入打包命令返回的 sha256",
  "chain_python": "/usr/bin/python3.11",
  "chain_cluster_genesis": {"base_fee":0,"accounts":[]},
  "chain_cluster_arguments": ["--sync","1","--produce","1","--io-threads","1","--verify-threads","1","--candidate-threads","1","--block-verify-threads","1","--block-ms","50","--max-block-txs","6000"]
}
```

这里的创世配置仅说明结构。实际运行使用准备好的相同创世文件；VDF 链的 `start_time_ms` 等参数必须在所有节点完全一致。控制机安装 `ansible/requirements.txt` 和 `Experiments/requirements.txt`，节点安装 Python 3.9+、psutil 和节点运行库。新部署默认一次处理 32 台，SSH 并发默认 32：

```bash
ansible-playbook -i /srv/chain/plan01/inventory.json ansible/cluster_deploy.yml \
  -e @/srv/chain/cluster-vars.json --forks 32
```

每个节点使用独立 systemd 服务 `chain-vdf-<cluster>-<node>.service`，以非 root 用户运行。数据库在 `/var/lib/chain-clusters/<cluster>/<node>/data`，VDF 身份由节点生成并保存在该持久目录中。重复运行同一部署不会覆盖数据库、创世或身份；配置发生变化时拒绝覆盖并保留原请求文件。

已有节点升级二进制，复用 `deploy.yml` 的滚动升级和失败自动恢复功能。生成的 inventory 已指定各节点服务名和端口：

```bash
ansible-playbook -i /srv/chain/plan01/inventory.json ansible/deploy.yml \
  -e @/srv/chain/new-release-vars.json -e chain_deployment_id=upgrade02 \
  -e chain_serial=16 --forks 32
```

## 3. 集群启停和日志

```bash
ansible-playbook -i /srv/chain/plan01/inventory.json ansible/cluster_control.yml \
  -e chain_cluster_action=status -e chain_cluster_results=/srv/chain/status01
```

`chain_cluster_action` 可选 `start`、`status`、`stop`、`collect`。每次操作核对原始部署记录、服务定义和进程参数；`collect` 保存每节点状态、最后 200 行服务日志和已写出的 metrics。启停不删除数据库。

## 4. 自动测试及清理

测试使用 `managed_worker.py`，每轮创建独立数据库。持久服务和测试必须使用不同端口。如果持久服务已占用 18089，生成测试计划时给所有节点指定另一个相同端口，例如 28089，并使用不同的输出目录。

先用同一个 `sender` 生成签名数据和创世账户，再记录两份文件的 SHA256：

```bash
/opt/chain/releases/propagation01/bin/sender --prepare /srv/chain/transactions.bin \
  --count 6040000 --wallets 1024
sha256sum /srv/chain/transactions.bin /srv/chain/transactions.bin.genesis.json
```

创建测试变量文件 `/srv/chain/benchmark-vars.json`，继承构建包参数，并添加：

```json
{
  "chain_release_id":"propagation01",
  "chain_package":"/srv/chain/propagation01.tar.gz",
  "chain_package_sha256":"打包返回的 sha256",
  "chain_python":"/usr/bin/python3.11",
  "chain_run_id":"test01",
  "chain_result_dir":"/srv/chain/results/test01",
  "chain_cluster_plan":"/srv/chain/testplan01/plan.json",
  "chain_dataset":"/srv/chain/transactions.bin",
  "chain_dataset_sha256":"交易文件 sha256",
  "chain_genesis_file":"/srv/chain/transactions.bin.genesis.json",
  "chain_genesis_sha256":"创世文件 sha256",
  "chain_cluster_rate":20000,
  "chain_cluster_wallets":1024,
  "chain_cluster_warmup":2,
  "chain_cluster_duration":300,
  "chain_cluster_drain_seconds":5,
  "chain_cluster_poll_seconds":0.1,
  "chain_cluster_metrics_period":1.0,
  "chain_cluster_sample_hz":20,
  "chain_cluster_query_workers":32,
  "chain_cluster_forks":32,
  "chain_cluster_readiness_timeout":300,
  "chain_cluster_bootstrap_producers":1,
  "chain_cluster_node_settings":{"max-block-txs":6000}
}
```

```bash
ansible-playbook -i /srv/chain/testplan01/inventory.json ansible/cluster_benchmark.yml \
  -e @/srv/chain/benchmark-vars.json --forks 32
```

测试控制机按账户将交易分配给计划中的 `injectors`，同一账户始终由同一个入口节点接收，其他节点通过交易传播获得数据。入口数默认 4、最多 64。发压时只使用节点 TCP 协议；SSH 用于整批启动、停止和取回文件。

多个 `-i` inventory 会传给测试中的 Ansible 调用。若通过 `-e @文件` 提供 SSH 连接变量，同时设置 `chain_extra_vars_file` 为该文件的绝对路径；该文件内容不会复制到结果中。Vault 的解密使用 Ansible 配置或 `ANSIBLE_VAULT_PASSWORD_FILE`，使子进程可以使用同一配置。

测量复用 `Test/Experiments.py`：TPS 使用共同链的交易数增量除以时间；确认时间从交易计划发送时间计算到抽样交易在所有节点进入同一区块，另保留发送完成后的时间。最终核对全部节点的账户余额、序号、销毁费用、唯一交易数和账户/合约状态摘要。结果保存 P50/P95/P99、实际发送速率、网络发送/接收字节、重组次数与深度、资源占用、未完成交易和完整时间序列。

每个查询批次最多使用配置的查询线程数。完整统计默认每秒读取一次，中间使用 80 字节链头回复；`query-sweeps.json` 和 `result.json` 保存查询实际耗时。确认时间的采样间隔由 `poll_seconds` 与实际查询耗时共同决定，测试报告需要保留这两个值。1000 个节点的亚秒测试应先测查询耗时，再调整控制机、查询并发和采样频率。

测试结束后，无论成功或失败，Ansible 都执行 `cleanup-group`：核对每轮所有权记录和 PID 创建时间，停止进程，保存日志/统计，再删除该轮数据库。原始创世文件、进程记录、部署包、节点身份文件和结果保留。控制机中断时，进程看守程序在一小时后停止所属测试进程；恢复控制机后用已有 `cleanup.yml` 和保留的 run/owner 参数完成清理。每轮使用新的 run ID 和结果目录，禁止重复使用旧数据库比较版本。

## 5. 本地工具回归

```bash
python3 -B -m unittest discover -s ansible/tests -v
```

回归覆盖 1000/1024 节点、6/8/12 度数的连通性与连接总量，IPv6、单端发起每条连接、重复地址拒绝、敏感字段拒绝、构建证明和文件篡改检测、服务所有权、幂等停止和隔离测试参数。真实 1000 节点 TPS 需要对应机器和测试记录。
