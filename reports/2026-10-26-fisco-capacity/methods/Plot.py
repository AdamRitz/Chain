"""Render the saved formal results; does not contact or start servers."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties

p=argparse.ArgumentParser()
p.add_argument('--font',type=Path,default=Path('C:/Windows/Fonts/msyh.ttc'))
args=p.parse_args()
root=Path(__file__).resolve().parents[1]
rows=[r for r in json.loads((root/'data/analysis.json').read_text())['cases'] if r['stage']=='formal']
font=FontProperties(fname=str(args.font))
plt.rcParams.update({'axes.spines.top':False,'axes.spines.right':False,'axes.unicode_minus':False,'font.size':11})
x=list(range(len(rows)));fig,axes=plt.subplots(1,2,figsize=(12.8,4.6),dpi=170)
ax=axes[0];w=.34
ax.bar([i-w/2 for i in x],[r['actual_send_tps']/1000 for r in rows],w,color='#A7B7CC',label='实际 SDK 调用')
ax.bar([i+w/2 for i in x],[r['tps']/1000 for r in rows],w,color='#2667A8',label='十节点共同链')
for i,r in enumerate(rows):ax.text(i+w/2,r['tps']/1000+.25,f"{r['tps']/1000:.2f}",ha='center',fontsize=9)
ax.set_ylim(0,27);ax.set_ylabel('千笔 / 秒',fontproperties=font)
ax.set_title('固定 serial200：实际输入与已确认吞吐量',fontproperties=font,pad=12)
ax.legend(prop=font,frameon=False,loc='upper right')
ax=axes[1]
ax.plot(x,[r['receipt_p95_ms']/1000 for r in rows],marker='o',lw=2,color='#2667A8',label='SDK 调用 → 成功回执 P95')
ax.plot(x,[r['scheduled_p95_ms']/1000 for r in rows],marker='s',lw=2,color='#C66D2A',label='计划发送 → 成功回执 P95')
ax.axhline(1,color='#999999',ls=':',lw=1);ax.set_ylabel('秒',fontproperties=font);ax.set_ylim(0,10.3)
ax.set_title('回压造成的等待须计入解释',fontproperties=font,pad=12)
ax.legend(prop=font,frameon=False,loc='upper left')
for ax in axes:
    ax.set_xticks(x,[str(r['plan']['rate']//1000)+'k' for r in rows])
    ax.set_xlabel('目标输入 TPS',fontproperties=font);ax.grid(axis='y',alpha=.15);ax.set_axisbelow(True)
fig.text(.5,.015,'每档 2 秒预热 + 10 秒测量，各一次；高档实际输入未达到目标。延迟仅统计已发出且成功的请求。',ha='center',fontproperties=font,fontsize=10,color='#505050')
fig.tight_layout(rect=[0,.055,1,1]);(root/'figures').mkdir(exist_ok=True)
fig.savefig(root/'figures/formal-throughput-latency.png')
