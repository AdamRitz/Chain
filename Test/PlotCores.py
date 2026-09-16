"""Publication-sized static charts for the measured core-affinity benchmark."""
import argparse
import json
from pathlib import Path
import statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator

def Main(source,output):
    data=json.loads(source.read_text(encoding='utf-8')); runs=data['runs']
    output.mkdir(parents=True,exist_ok=True)
    plt.rcParams.update({'font.family':'Microsoft YaHei','font.size':10,'axes.unicode_minus':False,
                         'axes.spines.top':False,'axes.spines.right':False,'axes.edgecolor':'#cbd5e1',
                         'text.color':'#172b4d','axes.labelcolor':'#344563','xtick.color':'#526581','ytick.color':'#526581'})
    fig,axes=plt.subplots(1,2,figsize=(12.4,5.4))
    fig.subplots_adjust(left=.08,right=.985,bottom=.19,top=.79,wspace=.25)
    fig.set_facecolor('#ffffff')
    for ax,title in zip(axes,['普通转账','Solidity 合约调用']):
        ax.set_title(title,loc='left',fontweight='bold',fontsize=14,pad=18)
        ax.grid(axis='y',color='#e8edf3',linewidth=.8)
        ax.set_axisbelow(True)
        ax.set_xlabel('物理核心数',labelpad=10)
        ax.set_ylabel('每秒交易数',labelpad=10)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda n,pos:f'{int(n):,}'))
        ax.set_xticks(data['config']['points'])
    for mode,label,color,ax in [('previous','修改前','#97a6ba',axes[0]),('native','当前版本','#087f8c',axes[0]),('evm','计数器合约','#7354c4',axes[1])]:
        points=[]
        for n in data['config']['points']:
            group=[r['tps'] for r in runs if r['workload']==mode and r['cores']==n]
            if group: points.append((n,statistics.median(group),min(group),max(group)))
        if not points: continue
        x,y,low,high=zip(*points)
        ax.plot(x,y,'o-',color=color,linewidth=2.2,markersize=5.5,label=label)
        ax.fill_between(x,low,high,color=color,alpha=.10,linewidth=0)
        if mode!='previous':
            best=max(points,key=lambda p:p[1]); ax.annotate(f'{best[1]:,.0f}',(best[0],best[1]),xytext=(0,12),textcoords='offset points',ha='center',fontsize=12,color=color,fontweight='bold')
    for ax in axes:
        ax.margins(y=.17); ax.set_ylim(bottom=0)
        ax.legend(frameon=False,loc='lower right',fontsize=9)
    fig.suptitle('核心数量与交易吞吐量',x=.08,y=.98,ha='left',fontsize=19,fontweight='bold')
    fig.text(.08,.025,'同步落盘 · 每组 3 次取中位数 · 阴影为实测范围',fontsize=10,color='#62738b')
    fig.savefig(output/'cores-tps.png',dpi=220,facecolor='white'); fig.savefig(output/'cores-tps.svg',facecolor='white'); plt.close(fig)
    svg=output/'cores-tps.svg'
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text(encoding='utf-8').splitlines())+'\n',encoding='utf-8')

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--results',type=Path,required=True); parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(); Main(args.results,args.out)
