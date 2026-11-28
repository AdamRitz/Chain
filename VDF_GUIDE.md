# VDF 新链：协议、参数和部署

2026-11-28 新增历史 Work 与公开承诺、揭露选举，见 [WORK_ELECTION_GUIDE.md](WORK_ELECTION_GUIDE.md)。以下保留既有 VDF 选举的参数和运行说明。

当前实现用类群重复平方和 Wesolowski 证明生成候选值，按候选值选择最多五个正常出块身份。配置写入创世摘要，只用于采用这套协议的新链。实现入口为 [VdfElection.h](Crypto/VdfElection.h)、[VdfRules.h](Consensus/VdfRules.h) 和 [Network/Vdf.h](Network/Vdf.h)。

## 选举与出块

创世块高度为 1。后续高度 `h` 所属组为 `G_e`，其中 `e = (h - 2) / epoch_blocks`，整数除法。

- `G_0`、`G_1` 的证明输入绑定创世摘要和创世块哈希；任意身份提供本组有效证明即可出块。
- 对 `e >= 2`，目标组 `G_e` 的输入绑定链标识、公钥、目标组编号、迭代数和 `G_(e-2)` 的完整区块哈希序列。节点在 `G_(e-1)` 计算并传播目标为 `G_e` 的记录。
- 排序值为带协议域的 `BLAKE2b-256(challenge || Y)`；排序值相同再比较公钥。签名和证明见证本身不参与排序。同一公钥只保留一份记录。
- `G_(e-1)` 每个块携带当时已验证的前五份记录；新块合并父块记录后重新排序，不能移除仍应进入前五的旧记录。该组最后一块的名单用于 `G_e`。它反映已写入这条分支的记录，传播未完成的记录不自动进入名单。
- 新建链设置 `producer_rotation: true`、`consecutive_blocks: 32`、`producer_timeout_slots: 8`、`epoch_blocks: 160`。名单按 VDF 排序值、公钥排序，依次由每人连续生成 32 个实际区块，再交给下一人。正常五人一轮共 160 块，然后使用新名单。
- 每个区块记录本轮的出块顺序编号和该成员已生成的块数；收到区块时根据父块重新计算并验证。未满 32 块时，连续 8 个完整时间片没有新区块，则下一位接管：父块时间片为 `p`，`p+1` 至 `p+8` 仍由当前成员出块，`p+9` 起轮到下一位。时间片为 200 ms 时，8 个空时间片共 1.6 秒。更长的停块按每 8 个空时间片依次跳过成员。
- 旧轮值链省略 `consecutive_blocks` 时保留每个时间片换人的规则：`producers[(slot - start_time_ms / slot_ms) % producers.size()]`。旧链未启用 `producer_rotation` 时，名单中的成员均可出块。
- 名单为空时，任意身份持本组有效证明可以出块。名单非空时，非成员恢复出块需要持本组有效证明；连续出块模式要求父子时间片间隔达到 `max(fallback_slots, producer_timeout_slots * 名单人数 + 1)`，让名单中的成员先依次接管。旧模式继续使用 `fallback_slots`。名单中的成员按对应的轮值规则出块。

名单成员连续出块的计算方式：先读取父块的顺序编号 `n` 和已出块数 `b`，同一组选举名单内若 `b=consecutive_blocks`，先令 `n=n+1,b=0`；更换名单时从 `n=0,b=0` 开始。然后计算：

```text
d = floor((block.slot - parent.slot - 1) / producer_timeout_slots)
block.turn.number = n + d
block.turn.blocks = (d > 0 ? 0 : b) + 1
producer = producers[block.turn.number % producers.size()]
```

顺序编号随区块写入并参与签名；同步、重启和分叉验证从父块恢复同一顺序。离线成员可能少于 32 块，接管者从自己的第 1 块开始。最初两组及空名单的区块记录 `number=0,blocks=0`；非成员恢复出块保留计算得到的顺序编号并记录 `blocks=0`，后续成员从第 1 块继续。

`slot = floor(逻辑 Unix 毫秒 / slot_ms)`。区块槽必须大于父槽，不能超过本地当前槽加 `future_slots`，且必须满足：

```text
slot >= start_time_ms / slot_ms + (height - 2)
```

`start_time_ms` 是创世承诺的统一起点，用于限制高度相对于时间的推进速度。所有节点共用同一个值；重启继续读取原创世文件。

轮流出块时，允许接收的未来区块先进入现有有界分叉缓存，到对应时间片才执行采用。正常轮值区块验证后立即提交；最初两组、空名单和非成员恢复出块仍保留原有比较等待。已在合法时间片生成的区块允许迟到，仍按现有分支规则处理。

节点每槽最多发布一个本地候选。达到交易上限时可以提前构造，否则在槽末构造；VDF 链允许空块，使低负载时的选举继续推进。选中的交易全部装入区块，受交易数、Gas 和字节上限约束。分支仍按累计有效交易数、高度、链头哈希选择；本地写入和后续重组沿用原来的账户状态规则。`slot_ms` 是调度参数，亚秒最终确认及千节点吞吐需要对应的网络与规模测试结果。

## 创世参数与身份

先完成源码构建，临近首次启动时生成一次配置，再分发给所有节点；也可以约定将来的统一起点。下面生成可运行空链的创世文件；转账测试前应在 `accounts` 加入所需公钥和初始余额。目标目录 `/srv/chain-vdf` 需已存在。

```bash
python3 - <<'PY'
import json, time
slot_ms = 200
genesis = {
    'base_fee': 1, 'accounts': [],
    'vdf': {
        'enabled': True, 'epoch_blocks': 160, 'iterations': 65536,
        'slot_ms': slot_ms,
        'start_time_ms': int(time.time() * 1000) // slot_ms * slot_ms,
        'fallback_slots': 20, 'future_slots': 2, 'producers': 5,
        'producer_rotation': True, 'consecutive_blocks': 32, 'producer_timeout_slots': 8,
    },
}
with open('/srv/chain-vdf/genesis.json', 'x') as output:
    json.dump(genesis, output, indent=2)
PY
```

| 参数 | 默认值 / 要求 | 含义 |
|---|---|---|
| `enabled` | 无 `vdf` 对象时关闭；对象内省略时为 `true` | 启用新协议 |
| `epoch_blocks` | 旧配置省略时为 32；新建链为 160；范围 1–4096 | 每组的区块数；连续出块模式要求等于 `5 * consecutive_blocks` |
| `iterations` | 65536；范围 4096–100000000 | 每个公钥、组和输入上下文的计算迭代数 |
| `slot_ms` | 50；范围 1–5000 | 一个槽的毫秒数 |
| `start_time_ms` | 启用时必填，正整数且能被 `slot_ms` 整除 | 新链统一时间起点 |
| `fallback_slots` | 20；大于 `future_slots` 且不超过 65536 | 非名单身份出块要求的父子槽间隔 |
| `future_slots` | 2；范围 0–2 | 接收节点允许的未来槽偏差 |
| `producers` | 固定为 5 | 当前协议允许名单的最大人数 |
| `producer_rotation` | 默认 `false`；新建轮值链设为 `true` | 启用名单轮流出块，绑定新的创世摘要 |
| `consecutive_blocks` | 省略为 0，保留旧规则；新建链为 32；启用时范围 1–819 | 每位成员连续生成的实际区块数，要求启用 `producer_rotation` |
| `producer_timeout_slots` | 8；大于 `future_slots` 且不超过 65536 | 连续多少个完整空时间片后交给下一位成员；仅连续出块模式使用 |

这些参数都绑定创世摘要，修改后属于另一条链。默认 `50 ms × 20 = 1 s` 是恢复出块的最小父子槽间隔；正常有资格节点不需要等这个间隔。

生产节点使用独立 Ed25519 身份。可通过 `--vdf-key /path/identity.seed` 或 YAML `node.vdf_key` 指定包含 64 个十六进制字符的 32 字节种子文件，按 `0600` 权限保存。未指定时在本节点数据目录创建并保留 `vdf-identity.seed`。每个生产节点使用自己的种子；不要把测试代码中的确定性种子用于实际链。`--produce 0` 只同步和验证，不加载生产身份、不计算或发布自己的候选。

## 出块节点直连

`producer_rotation: true` 时，当前五位出块者分别连接另外四位，形成十条双向 TCP 连接。节点每 200 毫秒更新一次当前名单和下一轮前五份已验证候选；当前或下一轮名单中的本机提前连接这两个名单中的其他成员。下一轮候选变化时，连接目标随之更新，正式出块名单仍取上一轮最后一块的记录。

`--max-peers` 保留普通邻居额度，另为当前和下一轮出块者预留最多九个连接名额。最多四个额外连接可以等待身份握手，三秒未完成即关闭。身份验证通过且公钥仍在当前或下一轮名单内，连接才使用预留名额。名单更新后撤销旧成员的保护，超出普通额度的连接会关闭。底层 `--max-connections` 同时增加最多十三个名额，以容纳这九个连接和四个握手。

节点通过已有邻居传播所选公钥的 IP 和监听端口，支持 IPv4、IPv6。地址记录只用于查找连接目标；每条连接使用独立的随机挑战，验证对端对创世摘要、公钥、监听端口和挑战的 Ed25519 签名。最多保存十个所选公钥，每个公钥最多四个地址，三十秒未更新的地址删除。

发布新区块时，先把摘要加入下一位接替出块的成员的发送队列，再发送给其余当前出块者，随后向普通邻居转发。连续出块期间也优先通知下一位成员，便于提前同步。出块者连接不受 `--block-relay-peers` 的普通转发数量限制。跨轮次的最后一块按它携带的新名单选择第一位成员。各 TCP 连接并行发送。

能力字段 `vdf_peer_protocol: 1` 通过现有消息 13/14 协商。新增消息：56 为 32 字节挑战；57 为 32 字节公钥和 64 字节签名；58 为地址记录，每条 51 字节，每个消息最多十条。未协商的旧节点继续使用原有传播协议。连接身份协议沿用已有格式；连续出块使用下面的新版创世参数和区块扩展。

本地验证使用 `VdfPeerProtocolCorrectness` 和 `VdfProducerPeersNetwork`。后者运行七个节点，普通连接上限设为二，检查当前五人十条直连、下一轮提前连接、发块发送顺序、错误身份拒绝和重启后的账户状态。

## 计算、存储与网络开销

`--vdf-bootstrap-produce 0`（YAML `node.vdf_bootstrap_produce: false`）只关闭本节点在最初两组的出块，保留 VDF 计算和传播。默认值为 `1`。大集群首次启动只让一个节点设为 `1`；从第三组开始所有生产节点自动执行正常轮值，无需重启。这个开关只控制本机生成区块，不改变收到区块的验证规则。

旧链省略 `consecutive_blocks` 或设为 0 时，创世参数编码、摘要和原有出块规则保持。启用连续出块需要创建新链、使用新数据目录。连续出块创世参数采用版本 3 的 96 字节编码，包含 `consecutive_blocks` 和 `producer_timeout_slots`；`GetGenesisConfig` 仅在启用时输出这两个新字段。

当前类群判别式参数为 1024 位。输出 `Y` 占 100 字节，证明见证占 100 字节；完整候选记录加上上下文、公钥和签名共 **382 字节**。旧区块扩展固定部分 **116 字节**，携带五份下一组记录时共 **2026 字节**，另带一份本组生产证明时为 **2408 字节**。连续出块的版本 2 扩展增加两个 8 字节整数（顺序编号、已出块数），对应为 **132、2042、2424 字节**。当前每块保存完整的滚动名单。

公钥绑定挑战意味着更换公钥需要重新计算。对一个挑战，计算包含顺序相关的重复平方；攻击者仍可以在多核、多个设备上同时计算不同公钥的挑战，并在更多结果中挑选较小值。机制给每次候选尝试增加计算成本，没有限制同一主体可创建的公钥数。调整 `iterations` 应依据部署硬件上的计算耗时、验证耗时和每组可用时间；不能仅凭迭代数推导身份公平性或绝对抗女巫能力。

候选通过消息 **49** 传播，负载固定 382 字节。空负载消息 **50** 请求当前组和下一组已保存的记录；回复为 0–12 条独立消息 49，包括前五份和本机自己的证明，去重后发送。包头另占 5 字节。候选记录不计入交易 TPS。

消息 13/14 的 `stats.vdf` 提供：

| 字段 | 解释 |
|---|---|
| `epoch`、`next_epoch` | 下一个区块所属组、再下一组 |
| `producers`、`candidates` | 下一个区块的有效名单、当前分支已写入的下一组记录 |
| `public_key`、`eligible` | 本地生产身份、当前是否能构造符合时间与资格条件的候选 |
| `producer_rotation`、`scheduled_producer` | 是否启用轮流出块、当前时间片对应的公钥 |
| `consecutive_blocks`、`producer_timeout_slots` | 每人连续出块数、停块后交接的空时间片数 |
| `bootstrap_produce` | 本节点是否在最初两组生成区块 |
| `computed`、`generation_ms` | 本进程生成的记录数、累计生成耗时；重启清零 |
| `proof_checks`、`proof_cache_hits` | 实际证明校验次数、已验证记录缓存命中次数 |
| `invalid_proofs`、`skipped_proofs`、`pending_proofs` | 无效记录、重复或较差记录跳过次数、待验证任务数 |
| `producer_peers`、`next_producer_peers` | 已通过连接身份验证的当前、下一轮出块者公钥 |
| `next_producers` | 当前分支记录与本地已验证候选合并后的下一轮前五个公钥 |
| `peer_connections` | 已验证连接的公钥、IP、监听端口及是否使用预留名额 |
| `peer_reserve_limit`、`peer_pending_limit`、`peer_pending` | 出块者预留名额、额外握手上限和当前等待数 |
| `peer_authentication_failures` | 身份签名、链、端口或挑战验证失败的次数 |
| `last_block_hash`、`last_block_height`、`last_block_slot`、`last_block_epoch`、`last_block_recipients` | 本机最近生成区块的信息，以及成功加入发送队列的已验证公钥顺序 |

可用 `generation_ms / computed` 观察本机平均候选生成开销。`vdf_test` 输出一次真实 `T=4096` 的 `prove_ms` 和 `verify_ms`；这两个数是该机器、该输入的原语测量，不包含网络传播和区块执行。

## 一键构建、测试和部署

多服务器快速操作使用 [cluster_quick.py](ansible/scripts/cluster_quick.py) 的 `stage`、`deploy`、`test`，完整命令见 [集群文档](ansible/CLUSTER.md)。程序只编译一次；发布包按 SHA256 复用。`test` 默认正式测量 **300 秒**，预热和结束后的等待单独计算；签名交易按发送速率与总发送时长生成并缓存。

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
  - VdfPeerProtocolCorrectness
  - VdfRulesCorrectness
  - VdfBlockCorrectness
  - VdfStateCorrectness
  - VdfNetwork
  - VdfRotationNetwork
  - VdfContinuousNetwork
  - VdfProducerPeersNetwork
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

默认必需测试列表包含 VDF 原语、选举、节点身份协议、规则、区块编码、数据库状态，以及七进程同步、轮值和出块者连接测试；上面的列表与 playbook 默认值一致。`chain_vdf_prepare_serial` 控制每批构建/验收的主机数，默认 32，可按软件仓库、镜像服务和控制端容量调整；`chain_serial` 控制随后每批启动服务的主机数，默认 1。可显式使用 `-f 16 -e chain_vdf_prepare_serial=8 -e chain_serial=8` 设置 SSH 并发及两阶段批次。

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
