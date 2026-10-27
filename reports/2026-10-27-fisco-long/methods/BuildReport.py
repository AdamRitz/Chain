"""Build the fixed-five-rate report from audited long-run results."""
import json
from pathlib import Path
import statistics

root=Path(__file__).resolve().parents[1]
analysis=json.loads((root/'data/analysis.json').read_text())
rows=[r for r in analysis['cases'] if r['stage']=='formal']
groups=analysis['groups']
assert len(rows)==15 and all(g['complete_three'] for g in groups)
assert all(r['eligible'] and r['all_heads_equal'] and r['signing_limit_above_terminal_height'] for r in rows)
assert not any(g['all_repeats_pass_window'] for g in groups)
assert all(r['submitted']==r['balance_audit']['confirmed_workload_transactions'] for r in rows)
best=max(groups,key=lambda g:g['tps']['median']);peak=max(rows,key=lambda r:r['tps'])
f=lambda v:f'{v:,.0f}'
lines=[
'# FISCO BCOS 3.7.3 原生转账：五档 60 秒 × 3 次复测',
'',
'日期：2026-10-27。批次 `cap-formal-20261027`。已按用户选择完成 20k、30k、40k、50k、60k 五档，每档测量 60 秒、重复 3 次，共 15 个正式用例。用户随后明确只完成这 15 轮，因此没有增加 14k、16k、18k 或其他容量档位。',
'',
f'**本轮最高的三次中位吞吐量为 {f(best["tps"]["median"])} TPS（目标 {f(best["rate"])} 档），单次最高为 {f(peak["tps"])} TPS。** 这些是十台原机器、1024 账户原生转账、固定调优配置下的实测处理能力。高压力时发送器受到回压，不能把目标输入 30k–60k 当成系统实际承载量。',
'',
'**没有一档在三个重复中满足持续接纳目标速率的条件。** 最低目标 20k 也存在未发出请求和队列增长。因此结果支持“饱和处理能力约 2 万 TPS 的量级”，不支持“可以稳定、无拒绝地接收每秒 2 万笔”，也不能证明已经穷尽所有配置的全局最大 TPS。',
'',
'## 三次统计',
'',
'| 目标 TPS | 实际 SDK 调用/秒，中位数 | 共同链 TPS，中位数 | 共同链 TPS，最小–最大 | 回执 P95，中位数 | 回执 P99，中位数 | 计划起点 P95，中位数 |',
'|---:|---:|---:|---:|---:|---:|---:|',
]
for g in groups:
    lines.append(f'| {f(g["rate"])} | {f(g["actual_send_tps"]["median"])} | **{f(g["tps"]["median"])}** | {f(g["tps"]["min"])}–{f(g["tps"]["max"])} | {g["receipt_p95_ms"]["median"]/1000:.3f} s | {g["receipt_p99_ms"]["median"]/1000:.3f} s | {g["scheduled_p95_ms"]["median"]/1000:.3f} s |')
lines += ['', '范围为三个实际观测值的最小–最大，不是置信区间。每轮先独立计算分位数，再报告三个分位数的中位数。测量窗口可以消化预热积压或较慢节点的落后量，因此部分窗口共同链 TPS 略高于当期实际调用速率，不能据此虚增长期接纳能力。', '',
'![吞吐量与延迟](figures/throughput-latency.png)', '',
'## 全部 15 轮', '',
'| 目标 | 重复 | 共同 TPS | 实际调用/秒 | 回执 P95 / P99（秒） | 未调用数 | SDK 错误回调 | 未决回调 |',
'|---:|---:|---:|---:|---:|---:|---:|---:|']
for r in sorted(rows,key=lambda r:(r['plan']['rate'],r['plan']['repeat'])):
    lines.append(f'| {f(r["plan"]["rate"])} | {r["plan"]["repeat"]} | {f(r["tps"])} | {f(r["actual_send_tps"])} | {r["receipt_p95_ms"]/1000:.3f} / {r["receipt_p99_ms"]/1000:.3f} | {f(r["unsubmitted"])} | {f(sum(r["callback_errors"].values()))} | {r["unresolved"]} |')
lines += ['',
'未调用数、错误回调和未决数统计整个 62 秒发送段；TPS 与回执延迟统计中间 60 秒测量窗。SDK 超时不是最终链上失败：停机审计对所有已入块但缺少业务成功回调的交易逐一查询返回码。错误回调不会被补入成功延迟样本，未调用请求也不会被算成吞吐量。', '',
f'本轮实际调用的 {f(sum(r["submitted"] for r in rows))} 笔转账最终全部进入审计账本，包括 {f(sum(sum(r["callback_errors"].values()) for r in rows))} 笔曾收到 SDK 错误回调的交易；这部分逐笔复查均为回执状态 0、原生返回码 0。它们不是最终丢单或失败交易。“交易池满”错误回调也不能直接解释为整个集群最终拒绝。早期进度中使用的“拒绝”应理解为 SDK 层错误反馈，最终业务结果以本次审计为准。', '',
'回执延迟从实际调用 SDK 开始，到状态 0 且原生返回码 0 的回调；计划起点延迟包含发送器回压排队，但仍仅覆盖最终观察到成功回调的已调用请求。队列受到在途阈值约束后形成平台，不代表目标输入可持续。', '',
'![队列轨迹](figures/queue-traces.png)', '',
'## 资源和停顿证据', '',
'| 目标 | 重复 | 节点 CPU 核数/台 | 发送器 CPU 核数/台 | 整机空闲 | 提案验证超时日志 | 视图超时日志 |',
'|---:|---:|---:|---:|---:|---:|---:|']
for r in sorted(rows,key=lambda r:(r['plan']['rate'],r['plan']['repeat'])):
    lines.append(f'| {f(r["plan"]["rate"])} | {r["plan"]["repeat"]} | {r["cpu_node_mean"]:.3f} | {r["cpu_sender_mean"]:.3f} | {100*r["host_idle_mean"]:.1f}% | {r["measurement_log_events"]["proposal_verify_timeout"]} | {r["measurement_log_events"]["view_timeout"]} |')
lines += ['',
'CPU 为测量窗口内累计 CPU 时间的增量，1.0 表示占用一个核。日志计数只包含测量窗口，排除初始化和停机后无 quorum 审计；是十个副本的事件总数，不是独立全网故障次数。日志、CPU 与队列关联属于诊断证据，不能仅凭相关性认定某个函数或配置是全部波动的根因。', '',
'表现最好的一次 40k/重复2 达到 20,214 TPS，节点平均 3.108 核、发送器 0.311 核，整机空闲约 5.5%，接近四核预算。较差的 30k/重复2 只有 14,569 TPS，整机空闲却上升至约 25.7%；测量期内记录 27 条视图超时和 17 条提案验证超时。其后半段三个 10 秒窗口分别约 10,266、6,948、7,754 TPS，说明共识停顿和队列变化是长测波动的重要线索，不能把性能下降全部归因于 CPU 算力。', '',
'20k/重复1 更明显：中间两个 10 秒窗口只推进约 1,042 和 1,391 TPS，后续追赶窗口达到 31,203 TPS。31,203 是落后进度恢复期间的短窗增量，不是本次可持续容量。相反，部分高吞吐轮次也有提案验证超时而没有视图超时，因此不能把每一条验证 timeout 都当成一次全网停顿。', '',
'## 固定配置与计量口径', '',
'官方 FISCO BCOS 3.7.3，十台原服务器各一个 PBFT 验证节点、4 vCPU、约 3.6 GiB 内存。SDK 3.7.0、JDK 17，每台一个预签名发送器连接本机节点；节点与发送器共享 CPU。1024 个账户共享全局环，金额 1、初始余额各 1,000,000,000；总初始化仍为 1024 笔。地址 `0x100c`、attribute=5，业务为内置 C++ 预编译 `userTransfer`，没有部署 Solidity 转账合约。', '',
'固定使用上一轮的 `serial50` 候选：串行执行、最小封块 50 ms、区块上限 6000、交易池 50000、leader 周期 5、验签线程 4、RPC 线程 4、notify 线程 2、warning 日志。每个发送器在途阈值 3000，全局约 30000；发送线程 4、SDK 线程 2、JVM 最大堆 512 MiB、ActiveProcessorCount=1。TLS、签名校验、存储参数及节点二进制保持原样。', '',
'每轮新数据库，预热 2 秒、测量 60 秒、最多 5 秒收尾。第 1 遍按 20/30/40/50/60k，第 2 遍按 60/50/40/30/20k，第 3 遍按 30/50/20/60/40k。配置在 15 轮之间没有改变，未因低结果重跑或剔除某轮。', '',
'长测预签名有效期设置为初始化高度 +4000，在隔离 config.genesis 设置 chain.block_limit=5000。节点的有效期和签名检查仍开启，签名前后检查实际编码值，全部用例最终高度也低于签名上限。首次准备冒烟误将参数仅写入 config.ini，出现 BlockLimitCheckFail，并遇到一次 SCP 传输错误；修正后的第二次冒烟通过。两次准备记录均保留，不进入正式排名。', '',
'主 TPS 为十节点最小累计已入块计数在测量端点的增量，除以端点采集区间中点的时间差；各端点在规范账本中核对哈希与累计数。采集跨度不超过 0.5 秒才进入排名，tps_bounds 只表示采集时刻不确定性。本轮最大采集跨度约 137 ms，全部发送器记录的启动时间差为 0 ms（毫秒分辨率）。分析数据另外保留至少七节点的进度指标，不能替代主指标，也不是单独采集的 PBFT 提交证书；由于各自起点不同，其区间增量未必高于十节点最小值增量。', '',
'单窗筛选使用：实际调用至少达到目标 99.9%、共同 TPS 至少达到目标 99%、未调用比例至多 0.1%、零错误/未决回调，以及完整窗口和后半 30 秒队列回归斜率均不超过目标速率的 0.1%/秒。还要求不少于 58 个合格队列观测、状态与签名有效期核对通过。同一档三个重复都满足才称在本次有限窗口内通过；这些工程容差不是无限时长稳定性的数学证明。', '',
'本轮未运行 Chain 压测。与 Chain 既有 10 秒结果比较时，持续时长、发送器资源预算、持久化和确认语义仍需一致；本轮成功回执 P95 不是全部十节点共同可见的逐笔确认 P95。不能直接据此宣称某条链快若干倍，或认定 FISCO 在所有条件下都无法做到亚秒确认。', '',
'## 正确性、清理与复现', '',
f'15 轮共计划 {f(sum(r["plan"]["rate"]*62 for r in rows))} 笔请求，实际调用 {f(sum(r["submitted"] for r in rows))} 笔；审计账本内成功测试转账共 {f(sum(r["balance_audit"]["confirmed_workload_transactions"] for r in rows))} 笔。全部正式用例通过 1024 账户余额重建、总量守恒、原生业务返回值核对和十节点规范前缀证明，停止时全部链头追平。', '',
'正式实验的 150 份数据库及两次准备冒烟的 20 份数据库均按停止身份和路径检查后清理。最终服务状态与原始配置哈希见 postflight.json；完整逐笔签名保留在各发送器，交易哈希清单、完整规范账本和 RPC 日志保留在控制机。为避免重复占满控制机磁盘，已完成的大 JSONL 证据无损压缩，并解压校验 SHA256 后才移除文本副本。', '',
'本地保存分析证据包、所有节点的配置/日志/资源档案、客户端结果、审计结果和区块索引；它不是完整逐笔原始资料的第二份复制。未下载的大记录在 retained-records.json 中列出远端路径、大小和 SHA256，可按需重新核验。分析包本身也逐字节核对 SHA256。', '',
'实测驱动提交 553eb092f4633d96617a73175931f49a687dbcdb；节点二进制 SHA256 为 7c29bd2afe615a57af02a440fc4d665c3beec3cf7301ec4007cc07caf5b683f4。各批次还保存实际源码快照和独立校验值。', '',
'[统计数据](data/analysis.json) · [实验计划](data/plan.json) · [构建和准备验证](data/validation.json) · [最终检查](data/postflight.json) · [归档来源](data/archives.json)。原始批次位于控制机 /opt/fisco-control/results/cap-formal-20261027；本地分析包位于 C:\\Users\\61485\\Documents\\Codex\\2026-10-20\\182-151-27-42-118-122\\outputs\\fisco-long-20261027。', '',
'复算入口：methods/AnalyzeLong.py --raw <分析包解压目录> --output data/analysis.json；图表由 methods/Plot.py 生成。', '',
]
(root/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
print('report written',best['rate'],best['tps']['median'],peak['tps'])
