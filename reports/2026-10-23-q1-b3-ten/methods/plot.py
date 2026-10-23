import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

out=Path(__file__).resolve().parents[2]/'outputs/q1-b3-20261023'
data=json.loads((out/'summary.json').read_text(encoding='utf-8'))
colors={'A':'#52525b','Q1':'#2563eb','B3S':'#d97706','B3':'#059669','Q1B3':'#9333ea'}
fig,axes=plt.subplots(1,2,figsize=(11,4.2),layout='constrained')
for group,color in colors.items():
    rows=sorted((r for r in data['selected'] if r['group']==group),key=lambda r:r['target_tps'])
    x=[r['target_tps']/1000 for r in rows]
    axes[0].plot(x,[r['tps']/1000 if r['tps'] is not None else float('nan') for r in rows],marker='o',label=group,color=color,lw=1.8)
    axes[1].plot(x,[r['latency_p95_ms'] if not r['latency_unresolved'] else float('nan') for r in rows],marker='o',label=group,color=color,lw=1.8)
axes[0].set_ylabel('Validated common-chain TPS (thousands)')
axes[1].set_ylabel('P95 common visibility latency (ms)')
axes[1].set_title('Only cells with all sent samples resolved',fontsize=9)
for ax in axes:
    ax.set_xlabel('Target input TPS (thousands)');ax.set_xticks([20,30,40,50,60]);ax.grid(alpha=.2)
    ax.spines[['top','right']].set_visible(False)
axes[0].legend(frameon=False,ncol=3,loc='upper left')
fig.suptitle('10 nodes | 2 s warmup + 10 s measurement | one observation per cell',fontsize=11)
fig.savefig(out/'comparison.png',dpi=180)
fig.savefig(out/'comparison.svg')
svg=out/'comparison.svg'
svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines())+'\n',encoding='utf-8')
print(json.dumps({'figure':str(out/'comparison.png')}))
