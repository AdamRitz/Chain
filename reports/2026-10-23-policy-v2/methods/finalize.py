from pathlib import Path
import json,shutil,hashlib,xml.etree.ElementTree as ET
workspace=Path(__file__).resolve().parents[2]
out=workspace/'outputs/policy-v2-20261023'
repo=Path('D:/Project/Chain');report=repo/'reports/2026-10-23-policy-v2'
data=report/'data';methods=report/'methods';data.mkdir(parents=True,exist_ok=True);methods.mkdir(exist_ok=True)
s=json.loads((out/'summary.json').read_text());rows={(r['group'],r['target_tps']):r for r in s['selected']}
initial=json.loads((out/'raw-invalid/interruption-audit.json').read_text())
evidence=json.loads((out/'evidence.json').read_text())
services=json.loads((out/'services-and-cleanup.json').read_text())
assert len(services)==10 and all(json.loads(r['stdout'])['passed'] and r['exit_code']==0 for r in services)
assert s['quality']['valid_cells']==25 and s['quality']['state_audits_passed']==26
assert initial['completed_loads']==9 and initial['audits_passed']==9 and initial['eligible_tps']==0
assert sum(r['databases_removed'] for r in evidence)==360
for file in ['windows-ctest.xml','windows-driver-ctest.xml','linux-ctest.xml']:
    tests=list(ET.parse(out/file).getroot().iter('testcase'))
    assert len(tests)==14 and all(t.find('failure') is None and t.find('error') is None for t in tests)
runtime=json.loads((out/'release.json').read_text());driver=json.loads((out/'driver.json').read_text())
dataset=json.loads((out/'raw-main/dataset.json').read_text())
protocol={'runtime_revision':runtime['revision'],'measurement_driver_revision':driver['driver_revision'],
    'dataset':dataset,'groups':['N95','A','A2','B2','A2B2'],'rates':[20000,30000,40000,50000,60000],
    'warmup_seconds':2,'measurement_seconds':10,'max_drain_seconds':5,'per_cell_runs':1,
    'model':'frozen preparation-v2; B2 bounded online heads reset per process',
    'measurement':'intersection of ten locally atomic published canonical tails; RPC intervals <=0.5s; persisted anchors rechecked after stopping',
    'latency':'20Hz planned sampling; first all-node identical transaction position; no irreversible finality claim',
    'completed_load_attempts':35,'initial_invalid_measurement_loads':9,'pretraffic_interrupted_cases':1,
    'corrected_grid_attempts':25,'invalid_endpoint_retries':1,'state_audited_load_attempts':35,
    'removed_databases':360,'services_restored_nodes':10,'new_policy_enabled_in_resident_services':False,
    'one_retry_cell':'A2/40000: original 12s endpoint interval 0.67549s exceeds 0.5s bound'}
(out/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
s['excluded_initial_batch']=initial;s['protocol']=protocol
(out/'summary.json').write_text(json.dumps(s,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
text=(out/'REPORT.md').read_text(encoding='utf-8')
text=text.replace('| 40000 | 36,951 | 37,818 | 40,201 |','| 40000 | 36,951 | 37,818 | 40,201 † |')
text=text.replace('## 实现与训练边界','''## 本轮判断

**最大观测继续由 A 取得；新方案中，B2 在相同 50,000 输入档最有价值，但尚未稳定胜出。** 本轮 A 是旧策略在同一个新二进制及共同测量配置下的对照，不是直接使用上轮历史数字。

- 50,000 档实际发送均为 49,996.8 TPS：B2 为 49,034.09 TPS，相对 N95 的 46,458.33 提高 5.54%；共同可见 P95 从 1,872.63ms 降至 1,690.15ms，下降 9.74%。它执行了 14 次让步，重放从 1,221,456 降至 1,098,384 笔（下降 10.08%）。迟到并未下降：105→106，不能把这里的收益解释为更准确的时钟或更少迟到。以上是一次同负载观测及机制关联，不是完整因果证明。
- A2 的缓存确实有效：50,000 档预测从 A 的 17,494 次降至 465 次，减少 97.34%，另有 14,961 次缓存命中。但十节点合计预算路径墙钟仅从 113.12ms 降至 18.20ms，相当于平均每节点每秒约 0.95ms，远小于节点约 3.9/4 核的整体忙碌程度。它不是主要 CPU 瓶颈。A2 的重放反而比 A 多（1,615,952 对 870,976），所以不能从调用次数下降推导明显 TPS 收益。
- 60,000 目标档，A 观测 51,065.42 TPS；B2 只有 45,707.13 TPS，且测量期只让步 2 次。A2B2 在该档没有价值模型让步，却与 A2 有明显差异，这再次说明短测的分支演化及运行波动不能忽略。组合策略没有取得最高成绩。
- A 在 60,000 档的迟到率为 27/463≈5.8%，N95 为 104/453≈23.0%；重放分别为 962,416 和 1,313,728 笔。这支持更合理准备预算减少无效工作的解释。两组实际发送不同（50,854.4 与 49,728.0 TPS），不能将全部吞吐差异归因于模型。A 的共同可见 P95 达 3,595.19ms，也说明最高短窗 TPS 不等于最好实时性。

下一步更值得补充“相对已经存在候选的增量贡献/成本”标签。当前 B2 学的是局部候选保留概率；一个区块即使最终保留，也可能已经被其他节点准备好，不能证明再次本地准备有额外价值。现有数据可以支撑后续独立训练，但本轮没有在线调阈值追逐最高分，也没有增加统计重复次数。

## 实现与训练边界''')
text=text.replace('## 结论解释范围','''## 60,000 目标档的实际输入与实时性

| 版本 | 实际发送 TPS | 共同链 TPS | 共同可见 P95 ms | 实际延迟样本 |
|---|---:|---:|---:|---:|
'''+ '\n'.join(f"| {g} | {rows[g,60000]['actual_send_tps']:,.1f} | {rows[g,60000]['tps']:,.2f} | {rows[g,60000]['latency_p95_ms']:,.2f} | {rows[g,60000]['latency_samples']} |" for g in ['N95','A','A2','B2','A2B2'])+'''

60,000 只是目标速率，发送器实际约为 49.7k–51.6k。短窗口内共同链也可能消化预热阶段积压，因此 A 的观测 TPS 略高于窗口内发送率不代表额外交易或长期容量。各格已发送的延迟样本均被观察到；高目标档因发送器达不到目标而没有产生全部计划样本。

![吞吐与重放对照](comparison.png)

## 测试、计量修复与恢复

运行代码提交 `d28a2fb2146a9afcff4f8f0758cd7c248e1023b6`；修正测量驱动提交 `c2c0c38536768f2d8a596c4c4f215e89470555a6`。Windows 与 Linux 核心十四项 CTest 全部通过；响应协议修复后 Windows 全套再次通过，Linux 补跑 ClockTools、ClockNetwork、ExperimentNetwork 对应脚本通过。初次本地回归两项因复用旧目录拒绝启动，随后使用全新隔离目录验证。

首次云端批次有 9 个已完成负载轮次因旧客户端 16KiB 响应上限拒收大祖先快照，没有有效 TPS；第十格在发压前中断。原始证据保留。修复为统计响应最多 64KiB、其他查询维持 16KiB，并补足大响应及超限拒收回归后重跑固定矩阵。

修正批次 25 轮中只有 A2/40,000 的采集跨度超过预设上限，所有已捕获锚点均在十份持久化状态中匹配；该格同参数、原始签名数据、新数据库诊断复测一次，表中标 †。未替换任何原本有效的结果。

包括作废测量在内共 **35 个负载轮次全部通过十节点持久化余额、nonce、费用核对**；连同一次发压前中断，共 360 份独立测试数据库均已删除。十台常驻 Chain/FISCO 服务均恢复 active/enabled，无遗留实验进程或数据库，常驻服务未切换为新实验策略。

## 结论解释范围''')
text+='''
## 复核材料

`data/summary.json` 包含每格固定选择结果及全部修正批次尝试；`data/protocol.json`、`data/evidence.json` 保存版本、数据集和原始证据 SHA256。完整原始记录另保存在本次交付目录的 `raw-invalid.tar.gz`、`raw-main.tar.gz`、`raw-retry.tar.gz`，没有提交签名工作负载二进制、私钥或实验数据库到 Git。
'''
(out/'REPORT.md').write_text(text,encoding='utf-8')
for name in ['REPORT.md','comparison.png','comparison.svg']:shutil.copy2(out/name,report/name)
for name in ['summary.json','quality.json','protocol.json','evidence.json','release.json','driver.json','windows-validation.json','linux-validation.json','windows-ctest.xml','windows-driver-ctest.xml','linux-ctest.xml','services-and-cleanup.json']:
    shutil.copy2(out/name,data/name)
shutil.copy2(out/'raw-invalid/interruption-audit.json',data/'initial-measurement-failure.json')
for name in ['build.sh','deploy.yml','run.sh','run-fixed.sh','driver-check.sh','driver.yml','retry.py','run-retry.sh','package_evidence.py','postflight.sh','plot.py','finalize.py']:
    shutil.copy2(workspace/'work/policy-v2'/name,methods/name)
print(json.dumps({'report':str(report),'cells':25,'state_audited_loads':35,'databases_removed':360,'services_restored':10}))
