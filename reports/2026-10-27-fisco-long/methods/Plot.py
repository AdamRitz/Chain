"""Plot medians/ranges and complete queue traces from saved measurements."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties

args=argparse.ArgumentParser();args.add_argument('--font',default='C:/Windows/Fonts/msyh.ttc');args=args.parse_args()
root=Path(__file__).resolve().parents[1];value=json.loads((root/'data/analysis.json').read_text())
font=FontProperties(fname=args.font);groups=value['groups'];x=list(range(5))
plt.rcParams.update({'axes.spines.top':False,'axes.spines.right':False,'axes.unicode_minus':False,'font.size':10})
fig,axes=plt.subplots(1,2,figsize=(12.4,4.8),dpi=170)
for field,label,color,offset in [('actual_send_tps','实际 SDK 调用','#A7B7CC',-.18),('tps','共同链 TPS','#2667A8',.18)]:
    med=[g[field]['median']/1000 for g in groups]
    low=[(g[field]['median']-g[field]['min'])/1000 for g in groups]
    high=[(g[field]['max']-g[field]['median'])/1000 for g in groups]
    axes[0].bar([i+offset for i in x],med,.34,yerr=[low,high],capsize=3,color=color,label=label)
axes[0].set_ylabel('千笔 / 秒',fontproperties=font);axes[0].set_title('三次中位数与最小–最大范围',fontproperties=font)
axes[0].set_ylim(0,26)
for field,label,color in [('receipt_p95_ms','调用起点 P95','#2667A8'),('scheduled_p95_ms','计划起点 P95','#C66D2A')]:
    med=[g[field]['median']/1000 for g in groups]
    low=[(g[field]['median']-g[field]['min'])/1000 for g in groups]
    high=[(g[field]['max']-g[field]['median'])/1000 for g in groups]
    axes[1].errorbar(x,med,yerr=[low,high],capsize=3,marker='o',color=color,label=label)
axes[1].set_ylabel('秒',fontproperties=font);axes[1].set_title('成功请求延迟；未调用请求另计',fontproperties=font)
for ax in axes:
    ax.set_xticks(x,[str(g['rate']//1000)+'k' for g in groups]);ax.set_xlabel('目标输入 TPS',fontproperties=font)
    ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True);ax.legend(prop=font,frameon=False)
axes[0].legend(prop=font,frameon=False,loc='upper right')
fig.text(.5,.015,'每档测量 60 秒 × 3 次；误差线为观测范围，不是置信区间。目标负载与实际发送分开报告。',ha='center',fontproperties=font,fontsize=10)
fig.tight_layout(rect=[0,.055,1,1]);(root/'figures').mkdir(exist_ok=True)
fig.savefig(root/'figures/throughput-latency.png');plt.close(fig)

fig,axes=plt.subplots(2,3,figsize=(13,7),dpi=150,sharex=True,sharey=True)
colors=['#2667A8','#C66D2A','#389378']
for ax,group in zip(axes.flat,groups):
    for row in sorted((r for r in value['cases'] if r['stage']=='formal' and r['plan']['rate']==group['rate']),key=lambda r:r['plan']['repeat']):
        points=sorted(row['common_chain_timeline'],key=lambda p:p['target_seconds'])
        warm=row['plan']['warm'];points=[p for p in points if warm<=p['target_seconds']<=warm+60]
        ax.plot([p['target_seconds']-warm for p in points],[p['pending_mean']/1000 for p in points],
                color=colors[row['plan']['repeat']-1],lw=1.2,label='重复 '+str(row['plan']['repeat']))
    ax.set_title(str(group['rate'])+' TPS 目标',fontproperties=font)
    ax.set_xlabel('测量开始后 / 秒',fontproperties=font);ax.set_ylabel('每节点平均待处理 / 千笔',fontproperties=font)
    ax.grid(alpha=.15)
    ax.tick_params(labelbottom=True);ax.set_ylim(0,60)
axes.flat[-1].axis('off');axes.flat[0].legend(prop=font,frameon=False)
fig.suptitle('完整队列轨迹：有界平台可能来自回压，不能单独视为输入可持续',fontproperties=font,fontsize=14)
fig.tight_layout(rect=[0,0,1,.96]);fig.savefig(root/'figures/queue-traces.png')
