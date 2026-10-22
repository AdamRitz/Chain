# 本次实验的方法存档

这里保存了本轮实际使用的构建、部署、发压、故障复测、证据打包和制图脚本。它们含本次发布目录、数据目录及控制机位置，是方法存档；不应直接当作下一次部署入口执行。

本地完整证据位于：

`C:/Users/61485/Documents/Codex/2026-10-20/182-151-27-42-118-122/outputs/policy-v2-20261023`

其中 `raw-main` 是修正协议后的 25 格实验，`raw-retry` 是唯一失败端点的诊断复测，`raw-invalid` 保存首次响应长度问题导致的作废测量。三个压缩包的 SHA256 见 `../data/evidence.json`。

从仓库根目录可使用以下命令离线重算基础报告与全部修正批次尝试，不连接服务器；输出应指向新目录，避免覆盖包含人工分析的最终报告：

```powershell
$evidenceRoot = 'C:/Users/61485/Documents/Codex/2026-10-20/182-151-27-42-118-122/outputs/policy-v2-20261023'
python Test/Clock/Benchmark/ReportV2.py --raw "$evidenceRoot/raw-main" --retries "$evidenceRoot/raw-retry" --output "$evidenceRoot/recomputed"
```

`plot.py` 与 `finalize.py` 原先位于本次工作区的 `work/policy-v2`，通过该位置推导工作区。此处副本用于追溯生成方法；若重放，需恢复原相对位置或调整路径。`finalize.py` 在基础报告上补充分析，应先生成基础报告再执行一次，重复执行会重复插入文字。最终报告中的数值以 `../data/summary.json` 为准。

`build.sh`、`deploy.yml` 使用运行代码提交 d28a2fb；`driver.yml`、`driver-check.sh` 和 `run-fixed.sh` 使用 c2c0c38 测量驱动。驱动补丁只放宽统计响应上限并补充回归，没有替换已测试的节点二进制。`postflight.sh` 的最终检查保存在 `../data/services-and-cleanup.json`。
