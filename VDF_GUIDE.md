# VDF 新链：协议、参数和部署

当前实现用类群重复平方和 Wesolowski 证明生成候选值，按候选值选择最多五个正常出块身份。配置写入创世摘要，只用于采用这套协议的新链。实现入口为 [VdfElection.h](Crypto/VdfElection.h)、[VdfRules.h](Consensus/VdfRules.h) 和 [Network/Vdf.h](Network/Vdf.h)。

## 选举与出块

创世块高度为 1。后续高度 `h` 所属组为 `G_e`，其中 `e = (h - 2) / epoch_blocks`，整数除法。

- `G_0`、`G_1` 的证明输入绑定创世摘要和创世块哈希；任意身份提供本组有效证明即可出块。
- 对 `e >= 2`，目标组 `G_e` 的输入绑定链标识、公钥、目标组编号、迭代数和 `G_(e-2)` 的完整区块哈希序列。节点在 `G_(e-1)` 计算并传播目标为 `G_e` 的记录。
- 排序值为带协议域的 `BLAKE2b-256(challenge || Y)`；排序值相同再比较公钥。签名和证明见证本身不参与排序。同一公钥只保留一份记录。
- `G_(e-1)` 每个块携带当时已验证的前五份记录；新块合并父块记录后重新排序，不能移除仍应进入前五的旧记录。该组最后一块的名单用于 `G_e`。它反映已写入这条分支的记录，传播未完成的记录不自动进入名单。
- 名单中的身份可以正常出块。名单为空时，任意身份持本组有效证明可以出块。名单非空时，其他身份只有满足 `block.slot - parent.slot >= fallback_slots` 并携带本组有效证明才能出块。此条件检查槽间隔，不判断其他节点是否实际离线。

`slot = floor(逻辑 Unix 毫秒 / slot_ms)`。区块槽必须大于父槽，不能超过本地当前槽加 `future_slots`，且必须满足：

```text
slot >= start_time_ms / slot_ms + (height - 2)
```

`start_time_ms` 是创世承诺的统一起点，用于限制高度相对于时间的推进速度。所有节点共用同一个值；重启继续读取原创世文件。

节点每槽最多发布一个本地候选。达到交易上限时可以提前构造，否则在槽末构造；VDF 链允许空块，使低负载时的选举继续推进。普通交易的整数倍打包仍由 `--block-tx-step` 控制。分支仍按累计有效交易数、高度、链头哈希选择；本地写入和后续重组沿用原来的账户状态规则。`slot_ms` 是调度参数，亚秒最终确认及千节点吞吐需要对应的网络与规模测试结果。

## 创世参数与身份

先完成源码构建，临近首次启动时生成一次配置，再分发给所有节点；也可以约定将来的统一起点。下面生成可运行空链的创世文件；转账测试前应在 `accounts` 加入所需公钥和初始余额。目标目录 `/srv/chain-vdf` 需已存在。

```bash
python3 - <<'PY'
import json, time
slot_ms = 50
genesis = {
    'base_fee': 1, 'accounts': [],
    'vdf': {
        'enabled': True, 'epoch_blocks': 32, 'iterations': 65536,
        'slot_ms': slot_ms,
        'start_time_ms': int(time.time() * 1000) // slot_ms * slot_ms,
        'fallback_slots': 20, 'future_slots': 2, 'producers': 5,
    },
}
with open('/srv/chain-vdf/genesis.json', 'x') as output:
    json.dump(genesis, output, indent=2)
PY
```

| 参数 | 默认值 / 要求 | 含义 |
|---|---|---|
| `enabled` | 无 `vdf` 对象时关闭；对象内省略时为 `true` | 启用新协议 |
| `epoch_blocks` | 32；范围 1–4096 | 每组的区块数 |
| `iterations` | 65536；范围 4096–100000000 | 每个公钥、组和输入上下文的计算迭代数 |
| `slot_ms` | 50；范围 1–5000 | 一个槽的毫秒数 |
| `start_time_ms` | 启用时必填，正整数且能被 `slot_ms` 整除 | 新链统一时间起点 |
| `fallback_slots` | 20；大于 `future_slots` 且不超过 65536 | 非名单身份出块要求的父子槽间隔 |
| `future_slots` | 2；范围 0–2 | 接收节点允许的未来槽偏差 |
| `producers` | 固定为 5 | 当前协议允许名单的最大人数 |

这些参数都绑定创世摘要，修改后属于另一条链。默认 `50 ms × 20 = 1 s` 是恢复出块的最小父子槽间隔；正常有资格节点不需要等这个间隔。

生产节点使用独立 Ed25519 身份。可通过 `--vdf-key /path/identity.seed` 或 YAML `node.vdf_key` 指定包含 64 个十六进制字符的 32 字节种子文件，按 `0600` 权限保存。未指定时在本节点数据目录创建并保留 `vdf-identity.seed`。每个生产节点使用自己的种子；不要把测试代码中的确定性种子用于实际链。`--produce 0` 只同步和验证，不加载生产身份、不计算或发布自己的候选。

## 计算、存储与网络开销

当前类群判别式参数为 1024 位。输出 `Y` 占 100 字节，证明见证占 100 字节；完整候选记录加上上下文、公钥和签名共 **382 字节**。区块扩展固定部分 **116 字节**，通常携带五份下一组记录时共 **2026 字节**；再携带一份本组生产证明时为 **2408 字节**。当前每块保存完整的滚动名单，尚未实现跨块记录去重。

公钥绑定挑战意味着更换公钥需要重新计算。对一个挑战，计算包含顺序相关的重复平方；攻击者仍可以在多核、多个设备上同时计算不同公钥的挑战，并在更多结果中挑选较小值。机制给每次候选尝试增加计算成本，没有限制同一主体可创建的公钥数。调整 `iterations` 应依据部署硬件上的计算耗时、验证耗时和每组可用时间；不能仅凭迭代数推导身份公平性或绝对抗女巫能力。

候选通过消息 **49** 传播，负载固定 382 字节。空负载消息 **50** 请求当前组和下一组已保存的记录；回复为 0–12 条独立消息 49，包括前五份和本机自己的证明，去重后发送。包头另占 5 字节。候选记录不计入交易 TPS。

消息 13/14 的 `stats.vdf` 提供：

| 字段 | 解释 |
|---|---|
| `epoch`、`next_epoch` | 下一个区块所属组、再下一组 |
| `producers`、`candidates` | 下一个区块的有效名单、当前分支已写入的下一组记录 |
| `public_key`、`eligible` | 本地生产身份、当前是否能构造符合时间与资格条件的候选 |
| `computed`、`generation_ms` | 本进程生成的记录数、累计生成耗时；重启清零 |
| `proof_checks`、`proof_cache_hits` | 实际证明校验次数、已验证记录缓存命中次数 |
| `invalid_proofs`、`skipped_proofs`、`pending_proofs` | 无效记录、重复或较差记录跳过次数、待验证任务数 |

可用 `generation_ms / computed` 观察本机平均候选生成开销。`vdf_test` 输出一次真实 `T=4096` 的 `prove_ms` 和 `verify_ms`；这两个数是该机器、该输入的原语测量，不包含网络传播和区块执行。

## 一键构建、测试和部署

使用 [ansible/vdf_prepare.yml](ansible/vdf_prepare.yml) 构建和测试，[ansible/vdf_deploy.yml](ansible/vdf_deploy.yml) 在全量通过后创建独立服务。完整清单、源码包和变量说明见 [Ansible 文档](ansible/README.md#vdf-新链的源码构建与独立部署)。目标为 EL9 x86-64，安装 GCC 13、CMake、Ninja、GMP、GDB 及项目依赖，构建 `RelWithDebInfo`，保留源码和调试段。

在仓库根目录打包，输出文件必须尚不存在：

```bash
python -B ansible/scripts/vdf_source.py pack --source "$PWD" \
  --output /srv/chain-vdf/source.tar.gz --source-commit "$(git rev-parse HEAD)"
```

开发中的未跟踪文件需逐个加 `--include relative/path`；默认包成员来自 Git 已跟踪文件。包哈希和逐文件清单标识实际工作树内容。

控制端 `/srv/chain-vdf/run.yml` 示例：

```yaml
chain_vdf_source_package: /srv/chain-vdf/source.tar.gz
chain_vdf_source_sha256: <打包输出的64位sha256>
chain_vdf_source_commit: <打包时的40位基准提交>
chain_vdf_release_id: vdf-001
chain_vdf_build_run_id: full-001
chain_vdf_build_jobs: 1
chain_vdf_prepare_serial: 32
chain_serial: 1
chain_vdf_classroom: false
# 如使用独立新版 Boost，填写目标机实际的头文件目录：
# chain_vdf_boost_dir: /opt/chain-deps/boost_1_86_0
chain_vdf_required_tests:
  - ChainCorrectness
  - ForkStateCorrectness
  - ForkNetwork
  - EvmNetwork
  - IPv6Network
  - VdfCorrectness
  - VdfElectionCorrectness
  - VdfRulesCorrectness
  - VdfBlockCorrectness
  - VdfStateCorrectness
  - VdfNetwork
chain_vdf_instance_id: experiment-001
chain_vdf_service_name: chain-vdf-experiment-001.service
chain_vdf_instance_dir: /var/lib/chain-vdf/experiment-001
chain_vdf_port: 18089
chain_vdf_arguments: ['--produce', '1', '--io-threads', '2', '--verify-threads', '2']
chain_vdf_node_config:
  node:
    bind: 0.0.0.0
    peers: [] # 多机时在各主机变量中填写可达的其他节点地址和端口
chain_vdf_genesis: "{{ lookup('file', '/srv/chain-vdf/genesis.json') | from_json }}"
```

清单使用 `chain` 组，每个主机设置唯一 `chain_node_id`。SSH 地址、用户和认证配置放在本地清单或 Vault；多机复用同一份创世。分别执行：

```bash
ansible-playbook -i /srv/chain/inventory.yml ansible/vdf_prepare.yml -e @/srv/chain-vdf/run.yml
ansible-playbook -i /srv/chain/inventory.yml ansible/vdf_deploy.yml -e @/srv/chain-vdf/run.yml
```

`prepare` 只构建测试；`deploy` 会先执行相同 prepare 验收，然后启动新实例。默认路径为 `/opt/chain/releases/vdf/<release_id>/source` 和 `build`；每次构建使用新的 `build_run_id`，日志、JUnit 和隔离测试数据在 `build-attempts/<run_id>/`。全量成功生成 `build-proof.json`；测试过滤得到的部分成功不允许部署。

默认必需测试列表包含 VDF 原语、选举、规则、区块编码、数据库状态和七进程网络六项测试；上面的列表与 playbook 默认值一致。`chain_vdf_prepare_serial` 控制每批构建/验收的主机数，默认 32，可按软件仓库、镜像服务和控制端容量调整；`chain_serial` 控制随后每批启动服务的主机数，默认 1。可显式使用 `-f 16 -e chain_vdf_prepare_serial=8 -e chain_serial=8` 设置 SSH 并发及两阶段批次。

每台的 `chain_vdf_build_jobs` 默认 1，CTest 固定串行；网络测试内部仍有七个节点进程。节点工作线程通过 `chain_vdf_arguments` 中的 `--io-threads`、`--verify-threads`、`--candidate-threads`、`--block-verify-threads` 和 `--query-threads` 设置。VDF 生产节点另有一个计算线程，每个启用 VDF 的节点有一个证明验证线程；当前服务没有 CPU 亲和性或配额参数。完整说明见 [Ansible 文档](ansible/README.md#vdf-新链的源码构建与独立部署)。

清单可以批量描述 1000 台机器；当前流程会在每台机器实际编译和测试，部署耗时随这些工作量增长。规模下的节点通信、TPS 和区块确认时间需要另外实测。

实例创建拒绝已存在的不同配置、创世、数据库、服务或占用端口。失败时停止本次新服务并保留证据和数据库，不替换旧链。主机防火墙和安全组需要允许节点之间访问配置端口。

## 网络测试与源码调试

`VdfNetwork` 启动七个真实进程，默认执行 `election`、`rejection`、`restart`、`partition`、`fallback` 五个独立场景。测试配置为 `T=4096`、每组 8 块、槽长 250 ms；每个场景创建新的共用创世和七个独立数据库。它检查名单、同步节点、非法证明/身份、重启、分区重组和五名生产者停止后的恢复，不代表千节点性能结果。

单步等待默认最多 90 秒，整个 suite 默认预算 480 秒，CTest 外层超时 600 秒。内部预算到期先保存失败结果并停止子进程；自定义预算时保留至少 60 秒收尾时间。实际耗时还包括六个身份的真实 VDF 计算、同步写盘和进程启动。

```bash
# 新目录逐次更名；不要复用已有 --work。
<release>/venv/bin/python -B <release>/source/Test/VdfNetworkTest.py \
  --bin <release>/build --work /var/tmp/vdf-network-debug-001 --case rejection

# Ansible 中只编译节点并筛选网络测试，用新 run ID：
ansible-playbook -i /srv/chain/inventory.yml ansible/vdf_prepare.yml -e @/srv/chain-vdf/run.yml \
  -e chain_vdf_build_run_id=network-001 -e 'chain_vdf_ctest_regex=^VdfNetwork$' \
  -e '{"chain_vdf_build_targets":["boost"],"chain_vdf_required_tests":["VdfNetwork"]}'
```

每个场景保存 `result.json`、每次启动的节点日志和参数、原始区块与候选；顶层 `result.json` 给出整体状态。重跑使用新的 work/run ID。清理测试 DB 前核对路径归属、停止状态并保留这些证据。

调试二进制例如 `gdb --args <release>/build/boost --config <debug-node.yaml> --genesis <debug-genesis.json> --data <fresh-debug-data> --port 18090`，使用独立端口和数据目录。最终移除测试过滤和目标过滤，恢复完整测试名单，再用新的 run ID 完成全量验收。
