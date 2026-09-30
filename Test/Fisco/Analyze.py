"""Derive committed TPS and resource use from retained observations."""
import argparse,json,statistics,math
from pathlib import Path

def Load(path):return json.loads(path.read_text(encoding='utf-8'))
def Lines(path):return [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()]
def Slope(pairs):
    if len(pairs)<3:return None
    mx=statistics.mean(x for x,y in pairs);my=statistics.mean(y for x,y in pairs)
    den=sum((x-mx)**2 for x,y in pairs)
    return sum((x-mx)*(y-my) for x,y in pairs)/den if den else None

def Resource(rows,start,end):
    rows=[x for x in rows if start<=x['epoch']<=end]
    if len(rows)<2:return None
    a,b=rows[0],rows[-1];dt=b['epoch']-a['epoch'];result={}
    for role in ['node','driver']:
        first=next((x for x in a['processes'] if x['role']==role),None)
        last=next((x for x in b['processes'] if first and x['pid']==first['pid']),None)
        if not first or not last:continue
        result[role]={'busy_cores':(last['cpu_s']-first['cpu_s'])/dt,
            'max_rss_mib':max(x['rss_bytes']/2**20 for r in rows for x in r['processes'] if x['role']==role),
            'write_MBps':(last['write_bytes']-first['write_bytes'])/dt/1e6}
    ticks=[y-x for x,y in zip(a['cpu_ticks'],b['cpu_ticks'])];total=sum(ticks[:8])
    result['host_idle_pct']=100*ticks[3]/total;result['host_iowait_pct']=100*ticks[4]/total
    for key in ['rx_bytes','tx_bytes','rx_packets','tx_packets']:
        result[key+'_per_s']=(b['net'][key]-a['net'][key])/dt
    result['disks']={}
    for name,first in a.get('disks',{}).items():
        if name not in b.get('disks',{}):continue
        delta={k:b['disks'][name][k]-v for k,v in first.items()}
        count=delta['read_ios']+delta['write_ios']
        result['disks'][name]={'busy_pct':delta['busy_ms']/dt/10,
            'await_ms':(delta['read_ms']+delta['write_ms'])/count if count else 0,
            'write_MBps':delta['write_sectors']*512/dt/1e6,'mean_queue':delta['queue_ms']/dt/1000}
    return result

def Analyze(raw):
    resources={p.parent.name:Lines(p) for p in (raw/'collected').glob('*/resources.jsonl')}
    results=[]
    for folder in sorted((raw/'results').iterdir()):
        if not folder.is_dir():continue
        row={'name':folder.name,'configuration':('stable-3.7.3-distributed' if folder.name.startswith('stable-') else
            ('classic-v0' if folder.name.startswith('classic-') else
            ('serial-v1' if folder.name.startswith('scan-') else 'parallel-v1')))}
        if not (folder/'driver.json').exists():row['complete']=False;results.append(row);continue
        d=Load(folder/'driver.json');row['complete']=True
        for key in ['workload','target_tps','warmup_s','measure_s','users','requested','submitted','success','failures','unresolved',
                    'actual_send_tps','receipt_tps','overall_tps','p50_ms','p95_ms','p99_ms','scheduled_p99_ms',
                    'send_s','drain_s','balance_mismatches','balances_checked','presigned','start_epoch_ms',
                    'queried_receipts','recovered_callback_failures','funding_delayed_receipts',
                    'distributed','driver_count','start_skew_ms','max_driver_scheduled_p99_ms','phase']:
            row[key]=d.get(key)
        start=d['start_epoch_ms']/1000+d['warmup_s'];end=d.get('end_epoch_ms',(start+d['measure_s'])*1000)/1000
        node_count=len(Load(folder/'before.json')['nodes'])
        row['node_count']=node_count
        samples=[]
        for s in Lines(folder/'chain.jsonl'):
            if 'nodes' not in s or len(s['nodes'])!=node_count:continue
            # Use the latest response time of the all-node observation.
            epoch=max(x['end_epoch'] for x in s['nodes'])
            if not start<=epoch<=end:continue
            samples.append({'epoch':epoch,'count':min(x['transactionCount'] for x in s['nodes']),
                            'height':min(x.get('blockNumber',0) for x in s['nodes']),
                            'pending':statistics.mean(x['pending'] for x in s['nodes'])})
        row['common_chain_samples']=len(samples)
        row['common_chain_tps']=None
        if len(samples)>1:
            a,b=samples[0],samples[-1];row['common_chain_window_s']=b['epoch']-a['epoch']
            row['common_chain_tps']=(b['count']-a['count'])/(b['epoch']-a['epoch'])
            blocks=b['height']-a['height']
            if blocks>0:
                row['mean_transactions_per_block']=(b['count']-a['count'])/blocks
                row['mean_block_interval_ms']=(b['epoch']-a['epoch'])*1000/blocks
            row['pending_slope_per_node']=Slope([(s['epoch'],s['pending']) for s in samples])
            row['pending_net_growth_per_node']=(b['pending']-a['pending'])/(b['epoch']-a['epoch'])
            row['pending_series']=[{'elapsed_s':s['epoch']-start,'pending':s['pending']} for s in samples]
            row['pending_mean_per_node']=statistics.mean(s['pending'] for s in samples)
            row['pending_max_per_node']=max(s['pending'] for s in samples)
        row['state_valid']=False;row['count_valid']=False
        if (folder/'after.json').exists() and Load(folder/'after.json').get('nodes'):
            before=Load(folder/'before.json');after=Load(folder/'after.json')
            row['state_valid']=after['valid']
            if 'blockNumber' in before['nodes'][0]:
                row['block_height_delta']=after['nodes'][0]['blockNumber']-before['nodes'][0]['blockNumber']
            delta=after['nodes'][0]['transactionCount']-before['nodes'][0]['transactionCount']
            expected=d['success']+d.get('setup_transactions',d['users']+(d['workload']=='solidity')+bool(d.get('created_native_table')))
            row['committed_count_delta']=delta;row['expected_count_delta']=expected
            row['count_valid']=delta==expected
            row['final_pending_max']=max(n['pending'] for n in after['nodes'])
            row['failed_count_delta']=after['nodes'][0]['failedTransactionCount']-before['nodes'][0]['failedTransactionCount']
        row['resources']={host:Resource(data,start,end) for host,data in resources.items()}
        # Net accumulation distinguishes a recovered queue burst from continuing growth.
        base=(d['measure_s']>=60 and d['actual_send_tps']>=.95*d['target_tps']
            and (row['common_chain_tps'] or 0)>=.95*d['target_tps'] and d['failures']==0 and d['unresolved']==0
            and d['balances_checked']==d['users'] and d['balance_mismatches']==0 and row['state_valid'] and row['count_valid']
            and row.get('final_pending_max',1)==0 and row.get('failed_count_delta',1)==0)
        row['criterion_version']='net-backlog-v2'
        row['slope_acceptance_passed']=base and (row.get('pending_slope_per_node') or 0)<=max(10,.01*d['target_tps'])
        row['sustainable']=base and (row.get('pending_net_growth_per_node') or 0)<=max(10,.01*d['target_tps'])
        results.append(row)
    return results

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('raw',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
    rows=Analyze(a.raw);a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(rows,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    for r in rows:
        print(r['name'],r.get('common_chain_tps'),r.get('p99_ms'),r.get('state_valid'),r.get('count_valid'),r.get('sustainable'))
