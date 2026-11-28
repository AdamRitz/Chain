# 离线生成历史 Work

`work_prepare` 用已保存的公共区块摘要和节点公钥生成真实 VDF。默认同时处理两个身份；同一身份从第零轮开始顺序计算，后一轮输入包含前一轮输出。不读取私钥，不启动节点，不改变创世参数。

```sh
work_prepare --anchors anchors.json --identities identities.json --out prepared-work --threads 2
```

`anchors.json` 格式：

```json
{
  "chain_id": "64位十六进制创世根哈希",
  "iterations": 65536,
  "anchors": [
    {"round": 0, "anchor": "64位十六进制第零轮摘要"},
    {"round": 1, "anchor": "64位十六进制第一轮摘要"}
  ]
}
```

`chain_id` 是创世块头中的根哈希，位于创世块字节 32～63。第 `r` 轮有 `n=160` 个区块，高度为 `2+rn` 到 `1+(r+1)n`。摘要与 `DBWorkAnchor` 一致：

```text
anchor[r] = BLAKE2b-256(
    "Chain/VdfConsensus/Anchor/v1"
    || chain_id
    || LE64(r)
    || LE64(n)
    || blockHash[2+r*n] || ... || blockHash[1+(r+1)*n])
```

`blockHash` 是完整区块头哈希，每个 32 字节。所有十六进制字段在计算前解码为原始字节。摘要必须从第零轮开始连续排列；工具处理输入的全部轮次，不固定为 32 轮。

`identities.json` 复用集群的公共身份列表：

```json
[
  {"id": "node-0001", "public_key": "64位十六进制公钥"},
  {"id": "node-0002", "public_key": "64位十六进制公钥"}
]
```

也可把该数组放到 `anchors.json` 的 `identities` 字段，并省略 `--identities`。工具只读取公钥，其他身份字段不会进入输出。

每份证明写入：

```text
prepared-work/work-vdf/<chain_id>/<public_key>/<iterations>/proofs/<round>-<anchor>.work
```

文件格式与节点完全相同，共 346 字节：`WORK`（4）、版本（LE16，值 1）、轮次（LE64）、轮摘要（32）、前一轮输出（100）、本轮输出和证明（200）。第零轮的前驱输出为 100 字节零。节点恢复后会再次验证缓存中的证明。

生成完成后输出一行 JSON，包含身份数、每身份轮数、新生成数、复用数、线程数和耗时。失败以非零状态退出，已完成的有效文件保留。重新运行同一命令时，已有文件必须通过轮次、摘要、前驱输出和数学验证才复用；遇到错误文件即停止，不覆盖。新文件先写临时文件，再用同目录硬链接发布，避免中断留下半份正式记录。输出目录需要支持硬链接，Linux 的 ext4/XFS 和 Windows 的 NTFS 均支持。

准备 33 个完整轮次时，账本高度为 `1+33*160=5281`。给每个身份生成 `round=0..32` 共 33 份 Work；恢复后的下一块进入第 33 轮，具有 32 份可抽查历史。每个身份的原始 Work 数据为 `33*346=11418` 字节，一百个身份合计约 1.09 MiB。只分发各节点自己的公钥目录。

账本、创世文件和 Work 必须来自同一次准备。快照恢复后保持创世配置和身份公钥不变。停止节点后再归档数据库；不要把身份种子和 `secrets/` 目录放入公共账本或 Work 包。

定向检查：

```sh
python Test/WorkPrepareTest.py --bin build/work_prepare --work /tmp/work-prepare-check
```

测试只生成两份公开身份、各三轮低难度 Work，检查断点复用、缺失文件补算、错误文件保护和输入约束，不启动区块链节点。
