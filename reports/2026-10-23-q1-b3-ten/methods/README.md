# 方法与复核

这里归档本轮实际使用的发布、构建、分发、发压、清理及制图方法。脚本包含本轮固定发布和结果目录，不应直接作为下一次部署入口运行。SSH 凭据没有写入归档；远程连接由工作区的 `remote.py` 从进程环境读取。

本轮原始证据位于：

`C:/Users/61485/Documents/Codex/2026-10-20/182-151-27-42-118-122/outputs/q1-b3-20261023`

`raw-main.tar.gz` 的 SHA-256 是 `9b2667ec49b6c02dd0fefe693f0b8660bc895d855a9aea34f5a80dab4c765bd6`，包含 25 个固定原始单元格，无失败重跑。归档筛选 JSON、JSONL、TXT、LOG，排除钱包私钥和签名二进制；实验数据库在归档前已清理。源码和节点二进制校验见 `../data/linux-validation.json`。

从仓库根目录离线重算基础表格：

```powershell
$evidence = 'C:/Users/61485/Documents/Codex/2026-10-20/182-151-27-42-118-122/outputs/q1-b3-20261023'
python Test/Clock/Benchmark/ReportV3.py --raw "$evidence/raw-main" --output "$evidence/recomputed"
```

该命令不访问服务器，不覆盖最终分析报告。`plot.py` 与 `finalize.py` 原先位于工作区 `work/q1-b3`，通过相对位置定位原始证据；这里的副本用于追溯。若重放，恢复原布局或调整路径，并先生成基础报告再执行一次 finalize，避免重复插入分析段落。

`Run.py --policy-v3` 是正式入口，使用已有 `plan-v2.json` 的相同五档与时长。五组配置、发送数据 SHA-256、应用与观察器版本保存在 `../data/protocol.json`。每格实例参数和采集过程见 raw-main 中的 `case.json`、`machines.json`、`timeline.jsonl`、`sender-batches.json` 和 `offline-common-proof.json`；状态核对与清理分别见 `partial-state-audit.json` 和各节点 `*-database-cleanup.json`。

`postflight.sh` 核对十台常驻服务及本轮残留进程/数据库。`clean_build_tests.py` 仅清理本次 Linux 发布目录中已退出的正确性回归数据库文件，保留输入和诊断日志。常驻原版服务恢复，新实验发布目录保留；没有将 Q1 设为常驻默认。
