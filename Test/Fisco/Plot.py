"""Rebuild the report figures from measured JSON (Matplotlib)."""
import argparse,json,statistics
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties,fontManager
from matplotlib.ticker import FuncFormatter

COLORS={'native':'#2476C5','solidity':'#12A184'}
NAMES={'native':'内置转账','solidity':'Solidity 合约'}

def Best(rows,mode):
    groups={}
    for r in rows:
        if r.get('workload')==mode and r.get('measure_s',0)>=60:
            groups.setdefault((r['target_tps'],r.get('phase') or 'initial'),[]).append(r)
    accepted=[(key,rs) for key,rs in groups.items() if len(rs)>=3 and all(r['sustainable'] for r in rs)]
    return max(accepted,key=lambda x:x[0])[1] if accepted else []

def Style(ax):
    ax.spines[['top','right']].set_visible(False)
    ax.spines[['left','bottom']].set_color('#CDD5DF')
    ax.grid(axis='y',color='#E8EDF3',linewidth=.8);ax.set_axisbelow(True)
    ax.tick_params(length=0,pad=8,labelsize=11)

def Plot(source,out,font=None):
    if font:
        fontManager.addfont(font);plt.rcParams['font.family']=FontProperties(fname=font).get_name()
    plt.rcParams.update({'font.size':12,'axes.unicode_minus':False,'figure.facecolor':'white',
                        'axes.labelcolor':'#34465A','text.color':'#22364B','savefig.facecolor':'white'})
    rows=[r for r in json.loads(source.read_text(encoding='utf-8')) if r['name'].startswith('stable-') and
          r.get('complete') and r.get('state_valid') and r.get('count_valid') and r.get('common_chain_tps')]
    out.mkdir(parents=True,exist_ok=True)
    best={m:Best(rows,m) for m in COLORS}
    fig,ax=plt.subplots(figsize=(10.5,6),layout='constrained');Style(ax)
    for mode,color in COLORS.items():
        scan=sorted([r for r in rows if r['workload']==mode and r['measure_s']<60],key=lambda r:r['target_tps'])
        ax.plot([r['target_tps']/10000 for r in scan],[r['common_chain_tps']/10000 for r in scan],
                '-o',color=color,lw=2.5,ms=7,label=NAMES[mode])
        if best[mode]:
            rs=best[mode];v=[r['common_chain_tps']/10000 for r in rs];mid=statistics.median(v)
            ax.errorbar(rs[0]['target_tps']/10000,mid,yerr=[[mid-min(v)],[max(v)-mid]],
                        marker='D',color=color,ms=10,capsize=5,ls='none',
                        label='60 秒 × 3 轮' if mode=='native' else None)
    limit=max(r['target_tps'] for r in rows)/10000*1.08
    ax.plot([0,limit],[0,limit],'--',color='#B6C1CC',lw=1.2,zorder=0,label='发送目标')
    ax.set(xlim=(0,limit),ylim=(0,limit),xlabel='发送目标（万笔 / 秒）',ylabel='共同提交交易（万笔 / 秒）',
           title='十节点吞吐量  ·  8 核 / 节点')
    ax.legend(frameon=False,loc='upper left');fig.savefig(out/'throughput.png',dpi=200);plt.close(fig)

    fig,axes=plt.subplots(1,2,figsize=(12,5.4),layout='constrained')
    labels=[];selected=[]
    for mode in COLORS:
        for i,r in enumerate(best[mode]):labels.append(NAMES[mode]+'\n第 '+str(i+1)+' 轮');selected.append(r)
    for ax,key,title,unit in zip(axes,['common_chain_tps','p99_ms'],['持续提交速度','99% 交易回执延迟'],['笔 / 秒','毫秒']):
        Style(ax);values=[r[key] for r in selected]
        bars=ax.bar(range(len(values)),values,color=[COLORS[r['workload']] for r in selected],width=.68)
        ax.set_xticks(range(len(values)),labels,fontsize=9);ax.set_title(title,pad=14);ax.set_ylabel(unit)
        ax.bar_label(bars,labels=[f'{v:,.0f}' for v in values],padding=5,fontsize=10)
        ax.set_ylim(0,max(values)*1.2 if values else 1)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x,p:f'{x:,.0f}'))
    fig.savefig(out/'sustained.png',dpi=200);plt.close(fig)

    fig,axes=plt.subplots(1,2,figsize=(12,5.2),layout='constrained')
    for ax,mode in zip(axes,COLORS):
        Style(ax)
        r=best[mode][-1] if best[mode] else None
        if r is None:continue
        hosts=sorted(r['resources'],key=lambda h:int(h.rsplit('.',1)[1]));data=[r['resources'][h] or {} for h in hosts]
        node=[d.get('node',{}).get('busy_cores',0) for d in data]
        driver=[d.get('driver',{}).get('busy_cores',0) for d in data]
        ax.bar(range(len(hosts)),node,color=COLORS[mode],label='区块链进程',width=.7)
        ax.bar(range(len(hosts)),driver,bottom=node,color='#A9B8CC',label='发压进程',width=.7)
        ax.axhline(8,color='#D69155',lw=1.4,ls='--')
        ax.set_xticks(range(len(hosts)),[h.rsplit('.',1)[1] for h in hosts])
        ax.set(xlabel='节点（内网地址末段）',ylabel='平均占用核心数',ylim=(0,8.7),
               title=f"{NAMES[mode]}  ·  {r['target_tps']:,} 笔 / 秒")
        ax.legend(frameon=False,loc='upper left',fontsize=10,ncol=2)
    fig.savefig(out/'cpu.png',dpi=200);plt.close(fig)

    fig,ax=plt.subplots(figsize=(10.5,5.5),layout='constrained');Style(ax)
    for name,color,label in [('stable-native-25000-seal200-r2','#2476C5','2.5 万笔 / 秒'),
                             ('stable-native-35000-seal200-r3','#DB8B45','3.5 万笔 / 秒')]:
        r=next((r for r in rows if r['name']==name),None)
        if not r:continue
        data=r.get('pending_series',[])
        ax.plot([p['elapsed_s'] for p in data],[p['pending']/10000 for p in data],color=color,lw=2.4,label=label)
    ax.set(xlim=(0,60),ylim=(0,None),xlabel='测量开始后的时间（秒）',
           ylabel='每节点平均待处理交易（万笔）',title='测量期间的交易积压')
    ax.legend(frameon=False,loc='upper left');fig.savefig(out/'pending.png',dpi=200);plt.close(fig)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('out',type=Path);p.add_argument('--font');a=p.parse_args()
    Plot(a.source,a.out,a.font)
