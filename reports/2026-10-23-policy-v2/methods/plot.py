from pathlib import Path
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
root=Path(__file__).resolve().parents[2]/'outputs/policy-v2-20261023'
data=json.loads((root/'summary.json').read_text())
groups=['N95','A','A2','B2','A2B2'];colors=['#667085','#2e90fa','#12b76a','#f79009','#7f56d9']
fig,axes=plt.subplots(1,2,figsize=(11,4.2),layout='constrained')
for group,color in zip(groups,colors):
    rows=sorted((r for r in data['selected'] if r['group']==group and r['eligible']),key=lambda r:r['target_tps'])
    xs=[r['target_tps']/1000 for r in rows];ys=[r['tps']/1000 for r in rows]
    axes[0].plot(xs,ys,'o-',label=group,color=color,lw=1.7)
    if rows:axes[0].errorbar(xs,ys,yerr=[[max(0,(r['tps']-r['tps_interval'][0])/1000) for r in rows],[max(0,(r['tps_interval'][1]-r['tps'])/1000) for r in rows]],fmt='none',ecolor=color,capsize=3)
    high=next((r for r in rows if r['target_tps']==50000),None)
    if high:axes[1].bar(group,high['counters_all10']['fork_reexecute_transactions']/1e6,color=color)
axes[0].set(xlabel='Target input (thousand TPS)',ylabel='Proven common-prefix TPS (thousands)',title='Ten nodes, one 10-second measurement per cell')
axes[0].legend(ncol=2,frameon=False);axes[0].grid(alpha=.2)
axes[1].set(ylabel='Reexecuted transactions across 10 nodes (millions)',title='Replay work at target 50,000 TPS')
axes[1].grid(axis='y',alpha=.2);axes[1].set_axisbelow(True)
fig.suptitle('A2 / B2 candidate policies — observed results',fontsize=13)
fig.text(.02,-.04,'Error bars: RPC acquisition-time bounds, not statistical confidence intervals. Replay counts are replica work, not new transactions.',fontsize=8)
fig.savefig(root/'comparison.png',dpi=170,bbox_inches='tight');fig.savefig(root/'comparison.svg',bbox_inches='tight')
