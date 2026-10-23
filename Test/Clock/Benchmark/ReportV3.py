"""Q1/B3 fixed matrix; retain original valid cells and every diagnostic attempt."""
import argparse
import json
from pathlib import Path
from Analyze import Analyze

GROUPS=['A','Q1','B3S','B3','Q1B3']
RATES=[20000,30000,40000,50000,60000]


def Build(raw,output,retries=None):
    attempts=Analyze(raw)['cases']
    if retries:
        for path in sorted(retries.glob('*/results.json')):
            attempts.extend(Analyze(path.parent)['cases'])
    selected={}
    for row in attempts:
        key=(row['group'],row['target_tps'])
        if key not in selected or not selected[key]['eligible'] and row['eligible']:
            selected[key]=row
    eligible=[row for row in selected.values() if row['eligible']]
    best=max(eligible,key=lambda row:row['tps']) if eligible else None
    quality={'attempts':len(attempts),'valid_cells':len(eligible),'expected_cells':25,
             'state_audits_passed':sum(bool(row['state_audit_passed']) for row in attempts),
             'selection':'original valid result first; first eligible diagnostic retry only when original invalid',
             'invalid_attempts':[{'case':row['case'],'error':row['error'],'tps_interval':row['tps_interval']} for row in attempts if not row['eligible']]}
    document={'selected':list(selected.values()),'attempts':attempts,'best_observed':best,'quality':quality,
              'interpretation':'single short observation per cell; no statistical superiority, stable capacity, or irreversible finality claim'}
    output.mkdir(parents=True,exist_ok=True)
    (output/'summary.json').write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    def F(value,digits=0):return '—' if value is None else f'{value:,.{digits}f}'
    def Cell(group,rate):return selected.get((group,rate),{})
    def Latency(row):return F(row.get('latency_p95_ms'))+(' †' if row.get('latency_unresolved',0) else '')
    lines=['# Q1/B3 十节点部署与测试','',
           f"最高有效观测为 **{best['group']}：{F(best['tps'],2)} TPS**，目标 {best['target_tps']:,}、实际发送 {F(best['actual_send_tps'],1)} TPS。" if best else '本轮未取得有效 TPS 观测。','',
           '## 固定比较','',
           '| 组 | 说明 |','|---|---|',
           '| A | 同一二进制的原 A 策略对照，Q1/B3 关闭 |',
           '| Q1 | A + 独立元数据通道、冷验签预算 3、分段池选择 |',
           '| B3S | A + B3 影子模式，只记录建议 |',
           '| B3 | A + B3 有界行动 |',
           '| Q1B3 | A + Q1 + B3 有界行动 |','',
           '五组均使用 learned、决策时 P95 准备、冻结 preparation-v2、生命周期和 B2 影子模型；B2 不行动。B3 每个进程从零开始在线更新，满块替代规则与学习建议分开计数，未根据本轮结果调整阈值。','',
           '十台服务器，每台一个节点；50ms 轮次、12ms 候选窗口、6,000 笔/块、sync=1、4 个 I/O、8 个交易验证、2 个查询线程。共用 720,000 笔签名原生转账、1,024 个账户、128 笔批次、顺序广播。每格预热 2 秒、测量 10 秒、收尾最多 5 秒，各一次，按速率轮换版本顺序。发送器与 node01 共享机器。','',
           '## 共同链 TPS','',
           '| 目标输入 | A | Q1 | B3S | B3 | Q1B3 |','|---:|---:|---:|---:|---:|---:|']
    for rate in RATES:
        lines.append('| '+str(rate)+' | '+' | '.join(F(Cell(group,rate).get('tps')) for group in GROUPS)+' |')
    lines+=['','## 共同可见 P95 时延（毫秒）','',
            '| 目标输入 | A | Q1 | B3S | B3 | Q1B3 |','|---:|---:|---:|---:|---:|---:|']
    for rate in RATES:
        lines.append('| '+str(rate)+' | '+' | '.join(Latency(Cell(group,rate)) for group in GROUPS)+' |')
    lines+=['','时延按 20Hz 抽样，从发送到首次在十节点观察到同一交易位置，包含查询与轮询等待。只对已完成样本计算分位数；它不是不可逆最终确认时间。† 表示存在未完成样本，显示值仅描述完成子集，不能当作总体 P95 或据此宣称时延改善。','',
            '## 实际输入与样本完成度','',
            '| 目标 | 组 | 实际发送 TPS | 有效共同链 TPS | P95 ms | 未完成/实际样本 |',
            '|---:|---|---:|---:|---:|---:|']
    for rate in RATES:
        for group in GROUPS:
            row=Cell(group,rate)
            lines.append(f"| {rate} | {group} | {F(row.get('actual_send_tps'),1)} | {F(row.get('tps'),2)} | {F(row.get('latency_p95_ms'),1)} | {row.get('latency_unresolved','—')}/{row.get('latency_samples','—')} |")
    lines+=['','## 高负载机制','',
            '以下是测量端点之间十节点累计增量。CPU 为每节点占用核数的中位数；锁/等待均为累计墙钟，不能相加后当成 CPU 时间。','',
            '| 目标 | 组 | CPU 核/节点 | 迟到/准备 | 重放交易 | B3 满块规则建议 | B3 学习建议 | B3 实际让步 | B3 标签 |',
            '|---:|---|---:|---:|---:|---:|---:|---:|---:|']
    for rate in [50000,60000]:
        for group in GROUPS:
            row=Cell(group,rate);c=row.get('counters_all10',{});k=row.get('clock_counters_all10',{})
            rules=k.get('marginal_rule_suggestions',0);learned=k.get('marginal_suggestions',0)-rules
            lines.append(f"| {rate} | {group} | {F(row.get('cpu_cores_per_node_median'),2)} | {k.get('late_candidates','—')}/{k.get('rounds','—')} | {F(c.get('fork_reexecute_transactions'))} | {F(rules)} | {F(learned)} | {F(k.get('marginal_deferrals'))} | {F(k.get('marginal_labels'))} |")
    lines+=['','## 质量与解释','',
            f"有效单元格 {len(eligible)}/25；共 {len(attempts)} 次负载尝试，{quality['state_audits_passed']} 次通过十节点持久化余额、nonce、供应量与费用核对。",'',
            '只有十节点祖先快照交集存在、两个端点采集跨度均不超过 0.5 秒、停止后十份持久化链上锚点复核通过的 TPS 才参与比较。采集区间上下界是计量边界，不是统计置信区间。无效原测量只允许同参数、新数据库的诊断复测；所有尝试保留，不替换有效低成绩。','',
            'A 对 Q1 检验调度与池锁组合；B3S 对 B3 检验相同模型开销下的行动效果；Q1B3 尚未完整拆分交互与推理开销。单次短测只能判断本轮观测，不能证明统计显著提升或长期最高容量。若收益只伴随满块规则动作，应归于规则，而不能称作学习模型提高了 TPS。','']
    (output/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    return document


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--raw',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--retries',type=Path)
    args=parser.parse_args();result=Build(args.raw,args.output,args.retries)
    print(json.dumps({'quality':result['quality'],'best':result['best_observed']},ensure_ascii=False))
