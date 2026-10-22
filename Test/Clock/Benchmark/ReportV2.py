"""Report all prospective A2/B2 cells, retaining failed observations and retries."""
import argparse
import json
from pathlib import Path
import statistics
from Analyze import Analyze

GROUPS=['N95','A','A2','B2','A2B2']
RATES=[20000,30000,40000,50000,60000]


def Build(raw,output,retries=None):
    original=Analyze(raw)
    attempts=original['cases'][:]
    if retries:
        for path in sorted(retries.glob('*/results.json')):
            attempts.extend(Analyze(path.parent)['cases'])
    selected={}
    for row in attempts:
        key=(row['group'],row['target_tps'])
        if key not in selected or not selected[key]['eligible'] and row['eligible']:
            selected[key]=row
    eligible=[r for r in selected.values() if r['eligible']]
    best=max(eligible,key=lambda r:r['tps']) if eligible else None
    quality={'attempts':len(attempts),'valid_cells':len(eligible),'expected_cells':25,
             'selection':'original valid result first; first eligible diagnostic retry only when original invalid',
             'state_audits_passed':sum(bool(r['state_audit_passed']) for r in attempts),
             'invalid_attempts':[{'case':r['case'],'error':r['error'],'tps_interval':r['tps_interval']} for r in attempts if not r['eligible']]}
    document={'selected':list(selected.values()),'attempts':attempts,'best_observed':best,'quality':quality,
              'interpretation':'single short measurements; acquisition bounds are not statistical confidence intervals; common visibility is not irreversible finality'}
    output.mkdir(parents=True,exist_ok=True)
    (output/'summary.json').write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'quality.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    def F(x,digits=0):return '—' if x is None else f'{x:,.{digits}f}'
    def Cell(g,r):return selected.get((g,r),{})
    lines=['# A2/B2 十节点候选策略实验','',
      f"最高有效观测为 **{best['group']}：{F(best['tps'],2)} TPS**（目标 {best['target_tps']:,}，实际发送 {F(best['actual_send_tps'],1)} TPS）。" if best else '本次没有通过端点与持久化验证的吞吐结果。','',
      '本报告比较 N95（历史 P95）、A（上一版决策预测）、A2（预测缓存与有界残差校准）、B2（父分支/候选保留学习）及 A2B2。最高观测不等于稳定最优；每格一次短测不能建立统计显著性。','',
      '## 实现与训练边界','',
      '- A2 继续使用冻结 preparation-v2.json，在已有 32 维特征上刷新队列和工作历史；新增按状态/轮次/工作版本/负载档位缓存，最长 10ms，以及分拥塞上下文的有界残差 P95 校准。未加入精确候选账户扫描或重新训练真实时间误差头。',
      '- B2 是两个小型在线逻辑回归头：父分支在延后观察中保留，以及父分支保留条件下候选保留。每次进程从零权重开始；两个头各有 12 个已观测样本后才可能让步。旧数据没有这些标签，因此没有冒称完成离线预训练。',
      '- 标签来自准备完成后首次主循环观测，年龄限于 250–500ms；超时、祖先不可见或退出删失。仅实际完成的准备产生标签，不推断被跳过工作的反事实结果。评价概率在决策时保存。',
      '- B2 根据已选目标轮次判断，必须有同父候选可走原提交流程，连续最多让步两次。各组统一开启生命周期跟踪；N95/A/A2 运行影子价值模型，只有 B2/A2B2 允许采取行动。',
      '- 共识排序、签名与账户检查、同步落盘和每轮一次准备约束保持原有规则。默认不开启新策略。','',
      '## 固定协议','',
      '十台物理机、每台一个节点；同一份 720,000 笔签名原生转账、1,024 账户、批次 128、顺序广播。50ms 轮次、12ms 候选窗口、最大 6,000 笔/块，4 个 I/O、8 个验签、2 个查询线程，sync=1。发送端仍与 node01 共享机器。每格预热 2 秒、测量 10 秒、排空最多 5 秒，版本顺序轮换。',
      '',
      '各节点原子发布本地已持久化链头和最多 256 个祖先；观察端在十份快照中求共同前缀。RPC 区间跨度超过 0.5 秒、前缀缺失或停机后锚点核对失败的结果不参与排名。单节点原子快照不是全局原子快照。',
      '',
      '所有组统一以 20Hz 抽样交易，测量期计划 200 笔；延迟为首次在十节点观察到同一交易位置的时间，包含查询和轮询，不能解释为不可逆最终确认。只对已观察完成的样本计算分位数，必须结合未完成样本数阅读。','',
      f"有效单元格 **{len(eligible)}/25**，总负载尝试 {len(attempts)} 次，状态核对通过 {quality['state_audits_passed']}/{len(attempts)} 次。",'',
      '## 共同链 TPS','',
      '| 目标输入 | N95 | A | A2 | B2 | A2B2 |','|---:|---:|---:|---:|---:|---:|']
    for rate in RATES:lines.append('| '+str(rate)+' | '+' | '.join(F(Cell(g,rate).get('tps')) for g in GROUPS)+' |')
    lines+=['','— 表示没有合格结果，不是零 TPS。只有原测量无效时才采用同参数、同数据集、新数据库的诊断复测；全部尝试保存在 summary.json。','',
      '## 50,000 档机制与实时性','',
      '| 版本 | 实际发送 TPS | 共同链 TPS | 迟到/准备 | 重放交易 | CPU 核/节点 | P95 共同可见 ms | 未完成/样本 |',
      '|---|---:|---:|---:|---:|---:|---:|---:|']
    for group in GROUPS:
        row=Cell(group,50000);clock=row.get('clock_counters_all10',{});c=row.get('counters_all10',{})
        lines.append(f"| {group} | {F(row.get('actual_send_tps'),1)} | {F(row.get('tps'),2)} | {clock.get('late_candidates','—')}/{clock.get('rounds','—')} | {F(c.get('fork_reexecute_transactions'))} | {F(row.get('cpu_cores_per_node_median'),2)} | {F(row.get('latency_p95_ms'))} | {row.get('latency_unresolved','—')}/{row.get('latency_samples','—')} |")
    lines+=['','## 决策开销与价值模型行动','',
      '下表为十节点测量端点之间的合计。墙钟累计包含等待/抢占，不等于 CPU 独占时间；B2 标签数与动作数也不是节省 CPU 的直接证明。','',
      '| 50,000 档版本 | 准备预测次数 | 缓存命中 | 预算路径墙钟 ms | 模型推理墙钟 ms | B2 让步 | 父分支/候选标签 |',
      '|---|---:|---:|---:|---:|---:|---:|']
    for group in GROUPS:
        c=Cell(group,50000).get('clock_counters_all10',{})
        lines.append(f"| {group} | {F(c.get('decision_predictions'))} | {F(c.get('decision_cache_hits'))} | {F(c.get('decision_budget_ms'),2)} | {F(c.get('decision_inference_ms'),2)} | {F(c.get('value_deferrals'))} | {c.get('value_survival_labels','—')}/{c.get('value_retention_labels','—')} |")
    lines+=['','## 结论解释范围','',
      '只有在实际发送速率接近、端点合格且状态核对通过时才能进行同档比较。模型降低迟到或重放，但没有提高共同链 TPS，应如实认为尚未验证吞吐收益；模型不触发行动的版本差异也不能归因于其决策。',
      '',
      '五组没有进一步拆分 A2 缓存与校准各自的贡献，也没有覆盖网络抖动、异常报时或恶意节点注入。B2 的局部短期保留标签不是全网最终收益，当前短测没有证明长期稳定容量、跨场景泛化或统计显著改进。','']
    (output/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    return document


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--raw',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--retries',type=Path)
    a=p.parse_args();result=Build(a.raw,a.output,a.retries)
    print(json.dumps({'quality':result['quality'],'best':result['best_observed']},ensure_ascii=False))
