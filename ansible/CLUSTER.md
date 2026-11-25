# 1000 个以上节点的部署和测试

新入口使用一个已经通过完整测试的 Linux 构建包。控制机分发相同二进制和源码；目标节点不用安装 C++ 编译器。编译和 GDB 调试仍在构建机进行。生产数据库、节点身份、隔离测试数据库分别由部署记录管理。

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

创建 `/srv/chain/cluster-vars.json`：

```json
{
  "chain_release_id": "propagation01",
  "chain_package": "/srv/chain/propagation01.tar.gz",
  "chain_package_sha256": "填入打包命令返回的 sha256",
  "chain_python": "/usr/bin/python3.11",
  "chain_cluster_genesis": {"base_fee":0,"accounts":[]},
  "chain_cluster_arguments": ["--sync","1","--produce","1","--io-threads","1","--verify-threads","1","--candidate-threads","1","--block-verify-threads","1","--block-ms","50","--max-block-txs","6000","--block-tx-step","2000"]
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
  --count 240000 --wallets 1024
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
  "chain_cluster_duration":10,
  "chain_cluster_drain_seconds":5,
  "chain_cluster_poll_seconds":0.1,
  "chain_cluster_metrics_period":1.0,
  "chain_cluster_sample_hz":20,
  "chain_cluster_query_workers":32,
  "chain_cluster_forks":32,
  "chain_cluster_node_settings":{"block-tx-step":2000}
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
