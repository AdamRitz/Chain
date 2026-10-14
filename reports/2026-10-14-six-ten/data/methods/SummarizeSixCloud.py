"""Summarize completed cases and their host observations without changing data."""
import collections
import json
from pathlib import Path
import statistics

WORK=Path('/home/ubuntu/chain/results/six-20261014-c4a8724')
streams={}
for directory in sorted(WORK.glob('host-profile*')):
    if not directory.is_dir():continue
    for file in directory.glob('node*.jsonl'):
        node=file.stem.split('-')[0]
        for line in file.open():
            try:sample=json.loads(line)
            except ValueError:continue
            streams.setdefault(node,[]).append(sample)
for stream in streams.values():stream.sort(key=lambda row:row['utc_seconds'])
cases=[]
for row in json.loads((WORK/'results.json').read_text()):
    info=row['case']; output=WORK/f"{info['version']}-{info['rate']}-r{info['repeat']}"
    metrics=row.get('metrics',{})
    cohort='paired_after_cleanup' if info['repeat']>=4 else ('stable_scan' if info['rate']<=20000 or (info['rate']==30000 and info['repeat']>1) else 'initial_scan')
    result={'case':info,'cohort':cohort,'status':row['status'],'error':row.get('error'),'metrics':metrics}
    if not metrics:
        cases.append(result);continue
    points=[json.loads(line) for line in (output/'timeline.jsonl').open() if line.strip()]
    measured=[point for point in points if 5<=point['seconds']<=35 and point['common']]
    if len(measured)>1:
        first,last=measured[0],measured[-1]; seconds=last['seconds']-first['seconds']
        keys=set(first['nodes'][0]).intersection(last['nodes'][0])
        cumulative=[key for key in keys if key.endswith('_ms') or key in ['signature_checks','signature_cache_hits',
            'signature_cache_evictions','db_writes','db_batch_bytes','network_bytes','network_sent_bytes',
            'fork_attempts','fork_rank_skips','fork_parent_skips','fork_reexecute_transactions','branch_append_transactions',
            'grouped_commit_blocks','missing_transactions','sync_requests','sync_retries','sync_blocks',
            'recent_transaction_hits','reorganizations','local_proposals','compact_reconstructed']]
        rates=[]
        for index,(a,b) in enumerate(zip(first['nodes'],last['nodes'])):
            rates.append({'node':f'node{index+1:02}',**{key:(b[key]-a[key])/seconds for key in cumulative if isinstance(a[key],(int,float))}})
        result['counter_rates_per_node']=rates
        result['counter_rates_mean']={key:statistics.mean(r[key] for r in rates if key in r) for key in cumulative if any(key in r for r in rates)}
        origin=row['controller_clock_origin_utc']+json.loads((output/'latency-samples.json').read_text())[0]['scheduled']
        machines=json.loads((output/'machines.json').read_text()); hosts=[]
        for node,machine in machines.items():
            samples=[p for p in streams.get(node,[]) if origin+first['seconds']<=p['utc_seconds']<=origin+last['seconds']
                and any(n['pid']==machine['pid'] for n in p['nodes'])]
            if not samples:continue
            def Mean(values):
                values=[v for v in values if v is not None]
                return statistics.mean(values) if values else None
            host={'node':node,'samples':len(samples),'cpu_idle_percent':Mean(p.get('cpu_percent',{}).get('idle') for p in samples),
                'cpu_iowait_percent':Mean(p.get('cpu_percent',{}).get('iowait') for p in samples),
                'cpu_steal_percent':Mean(p.get('cpu_percent',{}).get('steal') for p in samples),
                'available_memory_min_bytes':min(p['available_memory_bytes'] for p in samples),
                'swap_used_max_bytes':max(p['swap_used_bytes'] for p in samples)}
            for key in ['util_percent','write_await_ms','write_bytes_per_second']:
                host['disk_'+key]=Mean(p.get('disk_rates',{}).get('vda',{}).get(key) for p in samples)
            for key in ['bytes_sent','bytes_recv','packets_sent','packets_recv','dropin','dropout']:
                host['network_'+key+'_per_second']=Mean(sum(v.get(key,0) for v in p.get('network_rates',{}).values()) for p in samples)
            waits=collections.Counter(); thread_cpu=collections.Counter()
            before={}
            for p in samples:
                process=next(n for n in p['nodes'] if n['pid']==machine['pid'])
                for t in process['threads']:
                    waits[t['wait']]+=1
                    if t['id'] in before:thread_cpu[t['id']]+=max(0,t['cpu_seconds']-before[t['id']])
                    before[t['id']]=t['cpu_seconds']
            host['thread_wait_samples']=dict(waits.most_common())
            host['busiest_threads']=[{'id':tid,'cpu_seconds':cpu,'core_equivalents':cpu/max(1,samples[-1]['utc_seconds']-samples[0]['utc_seconds'])} for tid,cpu in thread_cpu.most_common(5)]
            hosts.append(host)
        result['hosts']=hosts
    cases.append(result)
(WORK/'analysis.json').write_text(json.dumps(cases,indent=2))
groups=collections.defaultdict(list)
for row in cases:groups[(row['case']['version'],row['case']['rate'],row['cohort'])].append(row)
summary=[]
for (version,rate,cohort),rows in sorted(groups.items()):
    complete=[r for r in rows if r['metrics']]
    def Median(key):
        values=[r['metrics'].get(key) for r in complete if r['metrics'].get(key) is not None]
        return statistics.median(values) if values else None
    result={'version':version,'rate':rate,'cohort':cohort,'runs':len(rows),'complete':len(complete),
        'drained':sum(bool(r['metrics'].get('drained')) for r in complete),
        'sustainable':sum(bool(r['metrics'].get('sustainable')) for r in complete),
        **{k:Median(k) for k in ['common_commit_tps','achieved_send_tps','p99_ms','invalid_or_rejected_sum','backlog_growth_per_second']}}
    hosts=[h for r in complete for h in r.get('hosts',[])]
    resources=[a for r in complete for a in r['metrics'].get('resources',[])]
    result['cpu_cores_mean']=statistics.mean(a['cpu_core_equivalents'] for a in resources) if resources else None
    result['node_rss_peak_bytes']=max((a['peak_resident_bytes'] for a in resources),default=None)
    for key in ['cpu_idle_percent','cpu_iowait_percent','cpu_steal_percent','disk_util_percent','disk_write_await_ms',
        'available_memory_min_bytes','network_bytes_sent_per_second','network_bytes_recv_per_second','network_packets_sent_per_second','network_packets_recv_per_second']:
        values=[h.get(key) for h in hosts if h.get(key) is not None]
        result[key]=statistics.mean(values) if values else None
    summary.append(result)
(WORK/'summary.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary,indent=2))
