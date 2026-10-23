from collections import Counter
import hashlib,json,shutil,xml.etree.ElementTree as ET
from pathlib import Path

here=Path(__file__).resolve().parent
workspace=here.parents[1];out=workspace/'outputs/q1-b3-20261023';raw=out/'raw-main'
repo=Path('D:/Project/Chain');report=repo/'reports/2026-10-23-q1-b3-ten'
data=json.loads((out/'summary.json').read_text(encoding='utf-8'))
cells={(r['group'],r['target_tps']):r for r in data['selected']}
def Delta(group,base,rate,metric):return (cells[group,rate][metric]/cells[base,rate][metric]-1)*100
def Points(name):return [json.loads(line) for line in (raw/name/'timeline.jsonl').read_text().splitlines()]
mechanisms={}
for group,rate in cells:
    name=f'{group}-{rate}-r1';points=Points(name)
    a=next(p for p in points if p['target_seconds']==2);b=next(p for p in points if p['target_seconds']==12)
    labels=[s['clock']['marginal_labels'] for s in b['nodes']]
    per_node=[]
    for i,(x,y) in enumerate(zip(a['nodes'],b['nodes']),1):
        per_node.append({'node':f'node{i:02d}','local_transaction_delta':y['chain_transactions']-x['chain_transactions'],
                         'end_transactions':y['chain_transactions'],'cpu_seconds':y['process_cpu_seconds']-x['process_cpu_seconds'],
                         'verification_wait_seconds':(y['verification_wait_ms']-x['verification_wait_ms'])/1000,
                         'signature_checks':y['signature_checks']-x['signature_checks'],
                         'reorganizations':y['reorganizations']-x['reorganizations'],
                         'fork_reexecute_transactions':y['fork_reexecute_transactions']-x['fork_reexecute_transactions']})
    reasons=Counter();targets=[]
    for p in (raw/name).glob('node*-clock.jsonl'):
        for line in p.read_text().splitlines():
            row=json.loads(line)
            if row['kind']=='marginal_decision':reasons[row['reason']]+=1
            if row['kind']=='candidate_outcome' and row.get('marginal_sample') and not row['censored']:
                targets.append(row['marginal_target'])
    mechanisms[name]={'labels_per_node_at_12_seconds':labels,'nodes_with_16_labels_at_12_seconds':sum(x>=16 for x in labels),
                      'whole_run_decision_reasons':dict(reasons),'whole_run_targets':len(targets),
                      'whole_run_positive_targets':sum(t>0 for t in targets),'measurement_per_node':per_node}
assert all(r['verification_budget_respected'] for r in data['selected'])
assert all(not m['whole_run_decision_reasons'].get('low_marginal_value',0) for m in mechanisms.values())
(out/'mechanisms.json').write_text(json.dumps(mechanisms,indent=2)+'\n')
a=cells['A',60000];b=cells['B3',60000];a50=cells['A',50000];q=cells['Q1',50000]
replay_delta=(b['counters_all10']['fork_reexecute_transactions']/a['counters_all10']['fork_reexecute_transactions']-1)*100
intro=f'''## 本轮结论

**保留 A 作为当前性能对照；本轮没有验证 Q1/B3 提高最高 TPS。Q1 的现实现有明显高负载退化，不建议启用为常驻配置。**

- 目标 60,000 时，A 为 {a['tps']:,.2f} TPS；B3 为 {b['tps']:,.2f} TPS，变化 {Delta('B3','A',60000,'tps'):+.2f}%。B3 的共同可见 P95 为 {b['latency_p95_ms']:,.2f}ms，较 A 变化 {Delta('B3','A',60000,'latency_p95_ms'):+.2f}%，重放变化 {replay_delta:+.2f}%；这表示本轮重放和时延有改善迹象，但吞吐没有胜出。
- 目标 50,000、实际输入同为 49,996.8 时，B3 相对 A 的 TPS 变化仅 {Delta('B3','A',50000,'tps'):+.2f}%，相对 B3S 为 {Delta('B3','B3S',50000,'tps'):+.2f}%；不足以宣称行动策略提高吞吐。60,000 档 B3 相对 B3S 的 TPS 变化 {Delta('B3','B3S',60000,'tps'):+.2f}%，但实际发送率也变化 {Delta('B3','B3S',60000,'actual_send_tps'):+.2f}%。
- Q1 在 40k/50k/60k 三档分别仅约 8.2k/4.5k/11.0k TPS，同时压低实际发送率。它的元数据等待确实下降，但新共享验签门控和落后节点成为主要疑点，不能用局部排队改善代替共同链吞吐。
- B3 的全部有效建议均来自 `full_alternative` 满块规则，`low_marginal_value` 学习建议为零。B3 active 在 50k/60k 的第 12 秒，十个进程均未取得 16 个训练标签；因此本轮没有证明学习模型本身产生收益。

![吞吐与共同可见时延](comparison.png)

'''
path=out/'REPORT.md';text=path.read_text(encoding='utf-8')
text=text.replace('## 固定比较',intro+'## 固定比较',1)
text+='''
## 退化定位

Q1 的独立元数据通道达到了局部目标：50k 档每条元数据累计排队均值从 A 的约 17.07ms 降到 0.91ms。但共享验签许可在 10 秒测量窗口内平均每节点累计等待约 34.53 秒，属于多个线程重叠等待；其中 node09 为 108.34 秒。节点验签完成数均值约 240,648，A 为 492,152。不能把这些墙钟等待直接解释为消耗的 CPU 秒数。

Q1/50k 的 node09 测量期只推进 41,216 笔本地链交易，其他节点约 242k–248k；端点时 node09 为 107,776 笔，其他节点均为 318,848 笔。这解释了为什么共同链只有约 4,544 TPS，远低于多数节点的本地推进速度。node09 的交易验签队列达到 32,768，区块队列为 39,392；顺序广播的发送端也受慢节点背压。Q1/40k 的 node06、node09 同样出现明显落后，故不能只将问题归为某一台机器偶然变慢。

结合代码，`VerificationBudget::Enter/Leave` 在每次冷验签上竞争同一互斥锁，并在授予/释放时唤醒等待线程；普通交易与区块优先请求共享三个许可。这是新增同步和队列耦合开销的直接排查点。当前证据支持“共享门控与落后节点放大了拥塞”，但没有独立拆分元数据通道、池分段和验签预算的因果贡献，也没有采集上下文切换或 CPU 火焰图，不能声称已经证明唯一根因。

分段路径的池锁持有时间在 Q1/50k 平均每节点约 0.061 秒；元数据更快、池锁等待下降、迟到更少，却伴随输入受限和共同链退化。这些局部指标不能单独作为优化成功标准。下一轮应先拆开 Q1 三个开关，单测元数据通道与池分段，并改为批次或任务级验签许可，避免逐签名全局锁与反复全体唤醒；本轮没有追加这类消融测试。

B3/60k 的重放从 A 的 968,304 降到 484,368 笔，但 CPU 中位数仍约 3.92/4 核，验签量没有减少，TPS 仍低于 A。它在测量期执行的 99 次让步全部为规则动作。满块规则跳过准备后，恰好也减少了可供在线学习的真实标签；50k/60k active 每节点到第 12 秒只有 0–3 个、0–2 个标签。后续可利用独立训练轮或保留有界探索来解决冷启动，但需与规则单独对照，不能把本轮规则作用记作机器学习效果。

## 时延删失

Q1/50k 只产生 89 个实际抽样交易，其中 87 个在观测截止前未完成，仅两笔完成交易的 P95 约 1,989ms；这个数字不能用于总体实时性比较。Q1/60k 为 53/73 未完成，Q1B3/60k 为 63/65 未完成。曲线中不展示有未完成样本的 P95，完整表格保留完成子集数值并标 †。实际样本数不足计划 200 还受发送速率不足影响，两种缺失原因分别保留。

## 部署、验证与恢复

节点实现基于 `97dc3379431263167fd3068881a09a1252f367cc`；实际发布提交为 `6ca799a49ce1a3a7ab264ccdda2baaa0d0cd71dd`，后者只增加固定实验入口与分析字段。Linux 十四项 CTest 全通过（58.80 秒），开启 Q1 的额外 EVM 十一组检查通过；此前本地 Windows 回归见 `../2026-10-23-q1-b3-local/REPORT.md`。十台部署同一二进制，SHA-256 为 `50e5f3fa0e913383f4e61ab6d89a292d72e4bd31b5569144a01be0dacc760851`。

25 个原始单元格均有效，无诊断重跑、无替换有效低成绩。250 份实验节点数据库全部清理，控制节点额外 26 份回归数据库也已清理，保留签名数据、创世配置、源码、二进制和日志。十台无遗留本轮实验进程，Chain/FISCO 均恢复 active/enabled；常驻原有 1,000 笔交易、每节点 34 个账户、九个邻居复核通过。

新版本保留在每台 `/opt/chain/releases/6ca799a49ce1a3a7ab264ccdda2baaa0d0cd71dd`，已用于全部实验。常驻服务恢复原先 `070fbb4` 发布与配置，没有将退化的 Q1 设置成默认运行策略。

## 复核材料

`data/summary.json` 保留全部 25 个原始结果及排序依据；`data/mechanisms.json` 保留节点端点增量、模型标签及规则/学习动作拆分；`data/protocol.json`、`data/linux-validation.json` 和 `data/evidence.json` 记录固定参数、版本与归档校验。原始证据压缩包及完整节点轨迹保留在本机交付目录的 `raw-main.tar.gz`，未向 Git 提交签名二进制、私钥、运行日志或数据库。方法和复算入口见 [methods/README.md](methods/README.md)。
'''
path.write_text(text,encoding='utf-8')
evidence=json.loads((out/'evidence.json').read_text());meta=json.loads((here/'release.json').read_text())
protocol={**meta,'groups':['A','Q1','B3S','B3','Q1B3'],'rates':[20000,30000,40000,50000,60000],
          'warmup_seconds':2,'measurement_seconds':10,'drain_max_seconds':5,'repeats':1,'nodes':10,'sample_hz':20,
          'dataset':json.loads((raw/'dataset.json').read_text()),'driver':json.loads((raw/'driver.json').read_text()),
          'all_verification_budget_checks_passed':True,'all_learning_action_counts_zero':True,
          'comparison_scope':'fixed prospective matrix; no additional performance runs',
          'service_state':'original 070fbb4 services restored; new release retained independently'}
(out/'protocol.json').write_text(json.dumps(protocol,indent=2)+'\n')
for directory in [report,report/'data',report/'methods']:directory.mkdir(parents=True,exist_ok=True)
for name in ['REPORT.md','comparison.png','comparison.svg']:shutil.copy2(out/name,report/name)
for name in ['summary.json','mechanisms.json','protocol.json','evidence.json','linux-validation.json','linux-ctest.xml','linux-optimized-evm.json','service-state.json']:
    shutil.copy2(out/name,report/'data'/name)
for name in ['preflight.json','postflight.json','build-cleanup.json','dry-run.json']:
    shutil.copy2(here/name,report/'data'/name)
for name in ['build.sh','deploy.yml','deploy.sh','run.sh','postflight.sh','prepare_release.py','package_evidence.py','clean_build_tests.py','download.py','plot.py','finalize.py']:
    shutil.copy2(here/name,report/'methods'/name)
print(json.dumps({'report':str(report),'quality':data['quality'],'best_tps':a['tps'],'rule_actions_60k':b['clock_counters_all10']['marginal_deferrals']}))
