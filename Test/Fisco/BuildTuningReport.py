"""Reproduce the tuning report and figures from the published measurements."""
import argparse,json,statistics
from pathlib import Path
import matplotlib.pyplot as plt
from matplotlib import font_manager,ticker

def Build(out,font):
    rows=json.loads((out/'RESULTS.json').read_text(encoding='utf-8'))
    baseline=json.loads((out/'BASELINE.json').read_text(encoding='utf-8'))
    continuous=[r for r in rows if r.get('phase')=='final-batch500' and r.get('workload')=='native' and r.get('sustainable')]
    single=[r for r in rows if r.get('node_count')==1 and r.get('state_valid') and r.get('count_valid') and not r.get('failures')]
    best=max(single,key=lambda r:r['common_chain_tps'])
    value=statistics.median(r['common_chain_tps'] for r in continuous)
    previous=baseline['native']['median_common_chain_tps'];gain=100*(value/previous-1)
    resource=[v for r in continuous for v in r['resources'].values() if v and 'node' in v]
    def Range(values,unit='',digits=2):
        values=list(values);return f'{min(values):,.{digits}f}–{max(values):,.{digits}f}{unit}'
    font_manager.fontManager.addfont(font)
    plt.rcParams.update({'font.family':font_manager.FontProperties(fname=font).get_name(),
        'axes.spines.top':False,'axes.spines.right':False,'axes.edgecolor':'#CBD5E1',
        'axes.labelcolor':'#334155','text.color':'#24364B','font.size':12,'axes.titlesize':17,
        'figure.facecolor':'white','axes.facecolor':'white','savefig.facecolor':'white'})
    figures=out/'figures';figures.mkdir(exist_ok=True)
    fig,ax=plt.subplots(figsize=(8.5,4.6));values=[previous,value]
    bars=ax.bar(['原配置\n60 秒 × 3 轮','优化配置\n60 秒 × '+str(len(continuous))+' 轮'],values,color=['#9CB4CF','#2476C8'],width=.48)
    ax.bar_label(bars,labels=[f'{x:,.0f}' for x in values],padding=7,fontsize=17)
    ax.set_ylim(0,max(values)*1.24);ax.set_ylabel('每秒提交交易数');ax.set_title(f'十节点持续吞吐量提升 {gain:.1f}%',pad=18)
    ax.yaxis.set_major_formatter(ticker.StrMethodFormatter('{x:,.0f}'));ax.grid(axis='y',color='#E7EDF5');ax.set_axisbelow(True)
    fig.tight_layout();fig.savefig(figures/'throughput.png',dpi=200);plt.close(fig)
    selected=[next(r for r in rows if r['name']==name) for name in [
        'stable-native-40000-profile-baseline-r2','stable-native-40000-large-block-r1','stable-native-70000-message128-r1']]
    fig,axes=plt.subplots(1,2,figsize=(11.5,4.8));labels=['1 万笔\n200 毫秒\n目标 4 万/秒','5 万笔\n500 毫秒\n目标 4 万/秒','10 万笔\n1,000 毫秒\n目标 7 万/秒']
    for ax,values,title,unit in [(axes[0],[r['common_chain_tps'] for r in selected],'十节点 · 20 秒测量','每秒提交交易数'),
        (axes[1],[r['p99_ms']/1000 for r in selected],'99% 回执延迟','秒')]:
        bars=ax.bar(labels,values,color=['#9CB4CF','#2476C8','#0FA58B'],width=.6)
        ax.bar_label(bars,labels=[f'{v:,.0f}' if unit!='秒' else f'{v:.2f}' for v in values],padding=5)
        ax.set_ylim(0,max(values)*1.23);ax.set_title(title,pad=15);ax.set_ylabel(unit);ax.set_xlabel('区块上限 / 封块配置')
        ax.grid(axis='y',color='#E7EDF5');ax.set_axisbelow(True)
    fig.tight_layout();fig.savefig(figures/'batching.png',dpi=200);plt.close(fig)
    hosts=sorted(continuous[0]['resources'],key=lambda h:int(h.split('.')[-1]));node=[];driver=[]
    for h in hosts:
        node.append(statistics.mean(r['resources'][h]['node']['busy_cores'] for r in continuous))
        driver.append(statistics.mean(r['resources'][h]['driver']['busy_cores'] for r in continuous))
    fig,ax=plt.subplots(figsize=(10,4.5));labels=[h.split('.')[-1] for h in hosts]
    ax.bar(labels,node,color='#2476C8',label='区块链进程');ax.bar(labels,driver,bottom=node,color='#A7B8CC',label='发送进程')
    ax.axhline(8,color='#D99150',linestyle='--');ax.set_ylim(0,9);ax.set_ylabel('平均占用虚拟处理器数')
    ax.set_xlabel('节点内网地址末段');ax.set_title('十节点 · 每秒 40,000 笔负载',pad=15);ax.legend(ncol=2,frameon=False)
    ax.grid(axis='y',color='#E7EDF5');ax.set_axisbelow(True);fig.tight_layout();fig.savefig(figures/'cpu.png',dpi=200);plt.close(fig)
    successful=[r for r in rows if r.get('complete') and r.get('common_chain_tps')]
    table='\n'.join(f"| {r['name']} | {r.get('node_count',10)} | {r['measure_s']} | {r['actual_send_tps']:,.0f} | {r['common_chain_tps']:,.0f} | {r['p99_ms']:,} | {'通过' if r.get('state_valid') and r.get('count_valid') and not r.get('failures') else '异常'} |" for r in successful)
    total=sum(r['success'] for r in continuous)
    summary={'ten_node_tps':value,'baseline_tps':previous,'improvement_pct':gain,'continuous_runs':[r['name'] for r in continuous],
        'single_peak_case':best['name'],'single_peak_tps':best['common_chain_tps'],'continuous_transactions_verified':total}
    (out/'SUMMARY.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    text=f'''# FISCO BCOS 瓶颈分析与性能复测

测试日期：2026 年 9 月 30 日。FISCO BCOS v3.7.3；十台腾讯云服务器，每台 8 个虚拟处理器、16 GiB 内存，通过内网通信。操作系统呈现的处理器拓扑为 4 核、每核 2 线程。GiB 全称为 Gibibyte，表示二进制吉字节。

## 测试结果

十节点内置转账的两轮持续吞吐中位数为 **{value:,.0f} 笔/秒**，相比上次的 {previous:,.0f} 笔/秒提高 **{gain:.1f}%**。每轮预热 10 秒、测量 60 秒，全部 {total:,} 笔转账完成回执、余额、十节点区块哈希和状态根核对。

| 场景 | 测量窗口 | 实际提交速度 | 99% 回执延迟 |
|---|---|---:|---:|
| 原十节点内置转账 | 60 秒 × 3 轮，中位数 | {previous:,.0f} 笔/秒 | 738 毫秒，中位数 |
| 优化后十节点内置转账 | 60 秒 × {len(continuous)} 轮，中位数 | {value:,.0f} 笔/秒 | {statistics.median(r['p99_ms'] for r in continuous):,.0f} 毫秒，中位数 |
| 优化后十节点 Solidity 合约转账 | 20 秒，1 轮 | {next(r['common_chain_tps'] for r in rows if r['name']=='stable-solidity-40000-final-batch500-r1'):,.0f} 笔/秒 | 1,558 毫秒 |
| 单节点内置转账，本次最高短测 | {best['measure_s']} 秒 | {best['common_chain_tps']:,.0f} 笔/秒 | {best['p99_ms']:,} 毫秒 |

每秒交易数的英文全称为 Transactions Per Second，缩写为 TPS。共同链速度取所有共识节点已提交计数的最小值，按测量窗口内的增长计算，每笔交易计数一次。

业务沿用官方内置转账和 ParallelOk Solidity 并行转账，每台发送端使用 2,048 个账户，配对交替转账。签名、账户初始化与合约部署在计时前完成；节点接收、验签、传播、共识、执行和存储提交计入测量。两轮共 560 万笔包含预热期间的转账。完整环境与负载定义见[原报告](../2026-09-30-fisco-ten-node/REPORT.md)。

![持续吞吐量](figures/throughput.png)

原内置转账三轮的 99% 回执延迟范围为 737–5,942 毫秒；本次两轮为 {Range((r['p99_ms'] for r in continuous),' 毫秒',0)}。延迟从实际发送至成功回执，数据同时保留从计划发送时刻起算的延迟。

持续负载沿用此前 `net-backlog-v2` 验收条件：实际发送和共同链吞吐达到目标的 95%，所有交易和余额核对通过，十节点状态一致，最终交易池清空，窗口首末的平均每节点积压净增长不超过目标速率的 1%。两轮积压净增速分别为 {', '.join(f"{r['pending_net_growth_per_node']:.1f}" for r in continuous)} 笔/秒。

## 瓶颈与修改

1. **区块批量开销。** 原参数为每块最多 10,000 笔、封块配置 200 毫秒；本次持续配置采用 50,000 笔和 500 毫秒。目标每秒四万笔的 20 秒对照中，共同链吞吐由 29,746 提高到 42,903 笔/秒。两个对照窗口均附带相同方式的性能采样。
2. **大区块传输容量。** v3.7.3 默认节点间消息容量为 32 MiB。十万笔区块试验中，02 号节点停在 7,664 高度，其他节点达到 7,680。将消息容量改为 128 MiB、接收缓冲改为 256 MiB 后，各节点重新达到一致状态；后续相同七万笔目标负载试验完成全部交易核对。每次网络读取的上限也由 40 KiB 调至 256 KiB。
3. **处理器与数据库开销。** 性能采样中，数据库后台线程占约 13%–17%，网络线程占约 8%–9%，执行入口线程约 10%–13%，约一半样本位于未细分的工作线程。最终写缓冲从 64 MiB 增至 128 MiB，缓存增至 512 MiB，数据库块缓存增至 256 MiB，最多保留 6 个写缓冲；后台任务数保持 4。参数组合的整体效果由持续复测验证。
4. **发压和统计代码。** 增加可配置的发送端和共识节点列表、在途请求上限、实际区块大小与间隔统计、线程采样、准备失败数据保留和最终查询异常处理。重启后提交配置交易，完成执行路径初始化，再开始发压；节点独立启停并行执行，状态核对作为下一步的条件。

MiB 全称为 Mebibyte，KiB 全称为 Kibibyte，分别表示二进制兆字节和二进制千字节。本次节点使用官方 v3.7.3 二进制，修改内容为压测代码和节点运行参数。传输加密、交易签名验证、全部节点执行、状态提交和余额核对保持开启。

| 最终十节点配置 | 取值 |
|---|---|
| 链上 `tx_count_limit` / `consensus.min_seal_time` | 50,000 笔 / 500 毫秒 |
| `p2p.allow_max_msg_size` / `session_recv_buffer_size` | 128 MiB / 256 MiB |
| `p2p.session_max_read_data_size` | 256 KiB |
| `storage.write_buffer_size` / `cache_size` / `block_cache_size` | 128 MiB / 512 MiB / 256 MiB |
| `storage.max_write_buffer_number` / `max_background_jobs` | 6 / 4 |
| `storage.enable_rocksdb_blob` | false |
| 交易池容量 / 验签线程 / 远程调用线程 | 500,000 笔 / 8 / 4 |
| 每台发送端在途请求 / Java 处理器数 / 最大堆 | 15,000 笔 / 2 / 2 GiB |

区块上限通过链上配置交易设置，各节点查询结果一致；已存在链的创世文件仍保留原值。数据库写入语义沿用官方配置。

| 两轮持续测试的资源指标 | 十节点范围 |
|---|---:|
| 区块链进程占用虚拟处理器数 | {Range(v['node']['busy_cores'] for v in resource)} |
| 发送进程占用虚拟处理器数 | {Range(v['driver']['busy_cores'] for v in resource)} |
| 主机处理器空闲比例 | {Range((v['host_idle_pct'] for v in resource),'%')} |
| 磁盘等待比例 | {Range((v['host_iowait_pct'] for v in resource),'%')} |
| 节点常驻内存峰值 | {Range((v['node']['max_rss_mib']/1024 for v in resource),' GiB')} |
| 接收带宽 | {Range((v['rx_bytes_per_s']*8/1e6 for v in resource),' 兆比特/秒',1)} |
| 发送带宽 | {Range((v['tx_bytes_per_s']*8/1e6 for v in resource),' 兆比特/秒',1)} |

![处理器占用](figures/cpu.png)

处理器接近满载，内存和网络带宽仍有余量。官方发布包缺少完整函数符号，本报告按可辨认的线程分类；共享工作线程中的验签、并行执行与调度开销可继续用带符号构建细分。

## 区块大小、实时性和吞吐量

实际吞吐量 = 每块实际提交交易数 ÷ 实际平均区块间隔。

本次持续测试平均每块约 {statistics.mean(r['mean_transactions_per_block'] for r in continuous):,.0f} 笔，实际平均间隔约 {statistics.mean(r['mean_block_interval_ms'] for r in continuous)/1000:.3f} 秒，因此吞吐约为每秒四万笔。增大批量可以分摊每块的固定工作；缩短封块等待可以降低低负载时的等待时间。负载接近处理能力时，排队时间也会进入回执延迟。

封块参数决定等待条件，达到区块交易上限也会触发封块。实际区块间隔还受共识、执行与提交影响，需要用已提交区块测量。小区块下，只要处理能力足够，低延迟和较高吞吐可以同时实现。

![区块参数对照](figures/batching.png)

图中前两组目标负载为每秒四万笔，最后一组为每秒七万笔；实际发送速度和其他参数变化见下表与配置记录。七万笔目标负载下，本次十节点通过状态核对的短时最高共同链吞吐为 43,852 笔/秒，99% 回执延迟 8.91 秒。六七万笔/秒仍需要进一步提高每个节点的实际处理能力，并通过相应负载的持续验证。

单节点运行在 10.206.0.16，使用独立新数据库，十台机器共同发压；十节点运行复用已有历史数据库。两个场景的数据库规模、接入线程数和通信工作量均记录在原始数据中，比较结果包含这些因素。

单节点最高短测中，区块链进程占用 7.46 个虚拟处理器，主机空闲比例约 2.51%，节点常驻内存峰值约 1.52 GiB。远程调用线程从 8 增至 16 后，共同链吞吐降至 42,915 笔/秒，99% 回执延迟增至 43.71 秒，最终恢复为 8 个线程。后续优化优先细分共享工作线程的处理器开销，再评估验签、执行、数据库压缩各自的改进收益。

## 异常与复现记录

- 存储 Blob 模式与缓冲参数组合在准备阶段遇到 14 号节点执行线程崩溃。已保存事件记录，恢复节点，并将最终 Blob 配置设为关闭。
- 十万笔区块、32 MiB 消息容量的用例出现节点同步落后，数据保留为异常用例。增加消息容量后的恢复状态和完整复测单独保存。
- 控制机磁盘空间不足时，对 100 个已完成 Chain 实验的采样日志以及本次完成的错误日志进行压缩。每个文件先验证解压后的 SHA-256（Secure Hash Algorithm 256-bit，256 位安全散列算法）与原文件相同，再移除未压缩副本。数据库保留。原路径和校验值见归档内的压缩记录；可用 `gzip -dk 文件.jsonl.gz` 恢复日志。

| 用例 | 节点数 | 测量秒数 | 实际发送/秒 | 共同链提交/秒 | 99% 回执延迟/毫秒 | 最终核对 |
|---|---:|---:|---:|---:|---:|---|
{table}

完整指标见 [RESULTS.json](RESULTS.json)，配置和恢复记录见 [CONFIGS.json](CONFIGS.json)，采样线程占比见 [PROFILES.json](PROFILES.json)，异常见 [INCIDENTS.json](INCIDENTS.json)，停止状态与归档校验见 [ARCHIVE.json](ARCHIVE.json)。

本次七项统计回归检查通过。图表与文档可用 `python Test/Fisco/BuildTuningReport.py 报告目录 --font 字体路径` 重建。测试结束停止实验进程，保留数据库和结果。

配置依据：[节点配置实现](https://github.com/FISCO-BCOS/FISCO-BCOS/blob/v3.7.3/bcos-tool/bcos-tool/NodeConfig.cpp)、[节点通信容量](https://github.com/FISCO-BCOS/FISCO-BCOS/blob/v3.7.3/bcos-gateway/bcos-gateway/GatewayConfig.cpp)、[数据库选项](https://github.com/FISCO-BCOS/FISCO-BCOS/blob/v3.7.3/libinitializer/StorageInitializer.h)、[封块条件](https://github.com/FISCO-BCOS/FISCO-BCOS/blob/v3.7.3/bcos-sealer/bcos-sealer/SealingManager.cpp)。
'''
    (out/'REPORT.md').write_text(text,encoding='utf-8')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('out',type=Path);p.add_argument('--font',required=True)
    a=p.parse_args();Build(a.out,a.font)
