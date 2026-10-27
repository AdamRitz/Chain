"""Recompute endpoint, resource, latency and cleanup evidence offline."""
import argparse
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import re
import statistics
import tarfile


def LogEvents(tar, start, end):
    """Count only measurement-window events, excluding setup and no-quorum audits."""
    counts = {'view_timeout': 0, 'proposal_verify_timeout': 0, 'proposal_fetch_timeout': 0}
    examples = []
    for member in tar.getmembers():
        if not member.isfile() or not member.name.startswith('node/log/'):
            continue
        for raw in tar.extractfile(member):
            line = raw.decode('utf-8', errors='replace').strip()
            kind = ('view_timeout' if 'After onTimeout' in line else
                    'proposal_verify_timeout' if 'verify proposal exceptioned' in line and 'timeout' in line else
                    'proposal_fetch_timeout' if 'onRecvCommittedProposalResponse failed' in line and 'timeout' in line else None)
            if kind is None:
                continue
            match = re.search(r'\|(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\|', line)
            if not match:
                continue
            epoch = datetime.fromisoformat(match[1]).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
            if start <= epoch < end:
                counts[kind] += 1
                if len(examples) < 5:
                    examples.append(line)
    return dict(counts=counts, examples=examples)


def ReadLines(path):
    if path.exists():
        with path.open(encoding='utf-8') as stream:yield from stream
    else:
        with gzip.open(path.with_suffix(path.suffix+'.gz'),'rt',encoding='utf-8') as stream:yield from stream


def Analyze(root):
    cases=[]
    for path in sorted(root.glob('cap-*/*/summary.json')):
        row=json.loads(path.read_text())
        if 'plan' not in row:
            continue
        if row.get('status')!='complete':
            row.update(batch=path.parent.parent.name,eligible=False,stage='failed')
            cases.append(row)
            continue
        plan=row['plan'];timing=json.loads(path.with_name('timing.json').read_text())
        a_time=timing['gate_epoch']+timing['warm'];b_time=a_time+timing['seconds']
        points=[json.loads(line) for line in path.with_name('chain.jsonl').read_text().splitlines()]
        pair=[next((p for p in points if p['target_seconds']==t),None) for t in [timing['warm'],timing['warm']+timing['seconds']]]
        row['endpoint_spans_seconds']=[p['bracket_end_monotonic']-p['bracket_begin_monotonic'] if p else None for p in pair]
        row['eligible']=bool(row.get('status')=='complete' and row.get('state_audit_passed')
            and row.get('boundary_proof_passed') and row.get('cleanup_ok')
            and all(v is not None and 0<=v<=.5 for v in row['endpoint_spans_seconds']))
        hosts=[]
        for archive in sorted(path.parent.glob('*-evidence.tar.gz')):
            with tarfile.open(archive) as tar:
                samples=[json.loads(line) for line in tar.extractfile('node/resources.jsonl')]
                config={name:tar.extractfile('node/'+name).read().decode() for name in ['config.ini','config.genesis']}
                log_events=LogEvents(tar,a_time,b_time)
            if not samples:continue
            a=min(samples,key=lambda s:abs(s['epoch']-a_time));b=min(samples,key=lambda s:abs(s['epoch']-b_time));dt=b['epoch']-a['epoch']
            if dt<=0:continue
            data={'host':archive.name.removesuffix('-evidence.tar.gz'),'interval_seconds':dt,'roles':{},'measurement_log_events':log_events}
            for role in ['node','driver']:
                x=next((p for p in a['processes'] if p['role']==role),None)
                y=next((p for p in b['processes'] if p['role']==role),None)
                if not(x and y) or x['pid']!=y['pid']:continue
                measured=[p['rss_bytes'] for s in samples if a_time<=s['epoch']<=b_time
                          for p in s['processes'] if p['role']==role and p['pid']==x['pid']]
                data['roles'][role]={'cpu_cores':(y['cpu_s']-x['cpu_s'])/dt,
                    'max_rss_bytes':max(measured) if measured else None,
                    'max_rss_bytes_entire_case':max(p['rss_bytes'] for s in samples for p in s['processes'] if p['role']==role),
                    'write_bytes_per_s':(y['write_bytes']-x['write_bytes'])/dt,
                    'thread_cpu_cores':{k:(v-x['thread_cpu_s'].get(k,0))/dt for k,v in y['thread_cpu_s'].items()}}
            ac=list(map(int,a['cpu'].split()[1:]));bc=list(map(int,b['cpu'].split()[1:]));ticks=sum(bc[:8])-sum(ac[:8])
            if ticks:
                data['host_idle_fraction']=(bc[3]-ac[3])/ticks
                data['host_iowait_fraction']=(bc[4]-ac[4])/ticks
                data['host_steal_fraction']=(bc[7]-ac[7])/ticks
            def Network(s):
                total=0
                for line in s['net'].splitlines()[2:]:
                    label,v=line.split(':');v=v.split()
                    if label.strip()!='lo':total+=int(v[0])+int(v[8])
                return total
            data['network_rx_plus_tx_gbps']=(Network(b)-Network(a))*8/dt/1e9
            hosts.append(data)
            if 'configuration' not in row:row['configuration']=config
        row['resources_by_node']=hosts
        row['measurement_log_events']={kind:sum(h['measurement_log_events']['counts'][kind] for h in hosts)
                                       for kind in ['view_timeout','proposal_verify_timeout','proposal_fetch_timeout']}
        if hosts:
            for role,label in [('node','node'),('driver','sender')]:
                values=[h['roles'][role]['cpu_cores'] for h in hosts if role in h['roles']]
                row[f'cpu_{label}_mean']=statistics.mean(values) if values else None
            row['host_idle_mean']=statistics.mean(h['host_idle_fraction'] for h in hosts)
            row['network_mean_gbps']=statistics.mean(h['network_rx_plus_tx_gbps'] for h in hosts)
        row['batch']=path.parent.parent.name
        row['stage']='formal' if row['batch'].startswith('cap-formal') else 'smoke' if row['batch'].startswith('cap-smoke') else 'tuning'
        row['callback_failure_count']=sum(row.get('callback_errors',{}).values())
        row['success_callback_fraction']=row.get('callback_success',0)/row['submitted'] if row.get('submitted') else None
        audit=json.loads(path.with_name('state-audit.json').read_text())
        row['terminal_heights']=[n['height'] for n in audit['nodes']]
        row['terminal_counts']=[n['count'] for n in audit['nodes']]
        row['balance_audit']=json.loads(path.with_name('balance-audit.json').read_text())
        for key in ['expected','observed']:
            row['balance_audit'].pop(key,None)
        row['database_cleanups']=len(json.loads(path.with_name('cleanup.json').read_text()))
        row['endpoints']=[dict(target_seconds=p['target_seconds'],count_min=p['count_min'],
                              height_min=min(n['height'] for n in p['nodes']),
                              height_max=max(n['height'] for n in p['nodes'])) for p in pair if p]
        row['common_chain_timeline']=[{k:p[k] for k in ['target_seconds','count_min','pending_mean']} for p in points]
        if len(pair)==2 and all(pair):
            first,last=[min(n['height'] for n in p['nodes']) for p in pair]
            sizes=[]
            ledger_path=path.with_name('canonical-blocks.jsonl')
            if not ledger_path.exists() and not ledger_path.with_suffix('.jsonl.gz').exists():
                ledger_path=path.with_name('canonical-index.jsonl')
                row['local_ledger_evidence']='block index; full transaction lists retained on controller'
            for line in ReadLines(ledger_path):
                block=json.loads(line)
                if first<block['height']<=last:
                    sizes.append(block['transaction_count'] if 'transaction_count' in block else len(block['hashes']))
            row['measurement_blocks']=dict(count=len(sizes),mean_transactions=statistics.mean(sizes) if sizes else 0,
                                            max_transactions=max(sizes,default=0))
        drivers=json.loads(path.with_name('drivers.json').read_text())
        row['sender_backlog_start']=sum(d['backlog_start'] for d in drivers)
        row['sender_backlog_end']=sum(d['backlog_end'] for d in drivers)
        scheduled={}
        for driver in drivers:
            for k,v in driver['scheduled_latency_histogram_ms'].items():
                scheduled[int(k)]=scheduled.get(int(k),0)+v
        for percentile in [50,95,99]:
            total=sum(scheduled.values());seen=0
            for k,v in sorted(scheduled.items()):
                seen+=v
                if seen>=percentile/100*total:
                    row[f'scheduled_p{percentile}_ms']=k
                    break
        row.pop('receipt_histogram_ms',None)
        # Do not equate a bounded inflight plateau with successful open-loop target admission.
        row['target_admitted_without_backpressure']=bool(row.get('unsubmitted')==0 and row['callback_failure_count']==0)
        cases.append(row)
    valid=[r for r in cases if r['eligible'] and r['stage']=='formal']
    return dict(cases=cases,formal_cases=len(valid),best_formal=max(valid,key=lambda r:r['tps']) if valid else None,
                note='Capacity is specific to these ten 4-vCPU servers and 1024-account native transfers. Short saturation throughput is not a sustainable-rate guarantee.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--raw',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();value=Analyze(args.raw);args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for row in value['cases']:
        print(row['stage'],row['name'],row['eligible'],round(row.get('tps',0)),row.get('receipt_p95_ms'),round(row.get('cpu_node_mean') or 0,2))
