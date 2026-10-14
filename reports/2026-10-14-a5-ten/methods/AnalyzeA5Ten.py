import json
from pathlib import Path
import statistics

ROOT=Path(__file__).resolve().parents[1]
RAW=ROOT/'work/a5cf8a2-ten-results'
REPO=ROOT/'chain-throughput'
def Load(path):return json.loads(path.read_text(encoding='utf-8-sig'))
before={row['case']['rate']:row for row in Load(REPO/'reports/2026-10-14-six-ten/data/short-results.json')}
old_phases={row['case']:row for row in Load(REPO/'reports/2026-10-14-tps-review/data/timeline-analysis.json')}
rows=Load(RAW/'results.json')
keys=['signature_checks','signature_cache_hits','signature_cache_evictions','signature_shared_checks',
      'local_proposals','fork_walk_ms',
      'reorganizations','fork_attempts','fork_schedules','fork_selections','fork_invalid_candidates',
      'fork_reexecute_transactions','branch_append_transactions','grouped_commit_blocks','grouped_commit_batches',
      'db_writes','db_write_ms','db_prepare_ms','db_batch_bytes','db_write_stalls_ms','pool_refresh_ms',
      'pool_refresh_users','pool_refresh_transactions','tx_queue_wait_ms','block_queue_wait_ms',
      'metadata_queue_wait_ms','metadata_tasks','block_tasks','block_reply_wait_ms','block_reply_read_ms',
      'block_reply_tasks','candidate_execute_ms','commit_execute_ms','candidate_lock_wait_ms',
      'fork_lock_wait_ms','commit_lock_wait_ms','cold_block_signature_ms','duplicate_block_work',
      'sync_requests','sync_blocks','missing_transactions','execution_cache_hits','execution_cache_misses']
profiles={path.stem:[json.loads(line) for line in path.read_text().splitlines()] for path in (RAW/'host-profile').glob('*.jsonl')}
summary=[]
for row in rows:
    rate=row['case']['rate'];metric=row['metrics'];old=before[rate]['metrics']
    points=[json.loads(line) for line in (RAW/f'rate-{rate}/timeline.jsonl').read_text().splitlines()]
    measured=[p for p in points if 2<=p['seconds']<=12 and p['common']]
    first,last=measured[0],measured[-1];elapsed=last['seconds']-first['seconds']
    def Rate(key):return statistics.mean(b.get(key,0)-a.get(key,0) for a,b in zip(first['nodes'],last['nodes']))/elapsed
    phases={key:Rate(key) for key in keys}
    queue_keys=['pool','ready_pool','pending_tx_verify','pending_block_verify','pending_metadata','pending_block_replies']
    queues={key:{'mean_per_node':statistics.mean(node.get(key,0) for p in measured for node in p['nodes']),
                  'peak_per_node':max(node.get(key,0) for p in measured for node in p['nodes'])} for key in queue_keys}
    resources=metric['resources']
    cpu=statistics.mean(node['cpu_core_equivalents'] for node in resources)
    memory=max(node['peak_resident_bytes'] for node in resources)/2**30
    network=statistics.mean(node['received_bytes_per_second']+node['sent_bytes_per_second'] for node in resources)*8/1e9
    hit=phases['execution_cache_hits'];miss=phases['execution_cache_misses']
    latency_samples=Load(RAW/f'rate-{rate}/latency-samples.json')
    assert latency_samples[0]['index']==0
    load_origin=latency_samples[0]['scheduled']
    host_start=row['controller_clock_origin_utc']+load_origin+first['seconds']
    host_end=row['controller_clock_origin_utc']+load_origin+last['seconds']
    host_rows=[]
    for node,profile in profiles.items():
        window=[p for p in profile if host_start<=p['utc_seconds']<=host_end and 'cpu_percent' in p]
        disks=[max(p.get('disk_rates',{}).values(),key=lambda d:d.get('util_percent',0),default={}) for p in window]
        waits=[d['write_await_ms'] for d in disks if d.get('write_await_ms') is not None]
        host_rows.append({'node':node,'samples':len(window),
            'cpu_idle_percent':statistics.mean(p['cpu_percent']['idle'] for p in window) if window else None,
            'cpu_iowait_percent':statistics.mean(p['cpu_percent']['iowait'] for p in window) if window else None,
            'disk_busy_percent':statistics.mean(d.get('util_percent',0) for d in disks) if disks else None,
            'write_await_ms':statistics.mean(waits) if waits else None,
            'minimum_available_gib':min((p['available_memory_bytes']/2**30 for p in window),default=None),
            'maximum_swap_bytes':max((p['swap_used_bytes'] for p in window),default=None)})
    old_phase=old_phases['short-'+str(rate)]
    new={
        'rate':rate,'tps':metric['common_commit_tps'],'previous_tps':old['common_commit_tps'],
        'tps_change_percent':100*(metric['common_commit_tps']/old['common_commit_tps']-1),
        'p99_ms':metric['p99_ms'],'previous_p99_ms':old['p99_ms'],
        'cpu_cores_mean':cpu,'peak_resident_gib':memory,'network_gbps_mean':network,
        'controller_cpu_cores':metric['controller_cpu_core_equivalents'],
        'db_ms_per_call':phases['db_write_ms']/phases['db_writes'] if phases['db_writes'] else None,
        'execution_cache_hit_fraction':hit/(hit+miss) if hit+miss else None,
        'grouped_blocks_per_batch':phases['grouped_commit_blocks']/phases['grouped_commit_batches'] if phases['grouped_commit_batches'] else None,
        'measurement_samples':len(measured),'measurement_seconds':elapsed,
        'previous_measurement_seconds':old_phase['elapsed'],'previous_measurement_samples':old_phase['samples'],
        'phases_per_second_per_node':phases,'previous_phases_per_second_per_node':old_phase['rates_mean_per_node'],
        'queues':queues,'host_rows':host_rows,'host_window_utc':[host_start,host_end],
        'drained':metric['drained'],'sustainable':metric['sustainable'],'tx_sustainable':metric['tx_sustainable'],
        'logical_backlog_growth_per_second':metric['logical_backlog_growth_per_second'],
        'logical_backlog_end':metric['logical_backlog_end'],'invalid_or_rejected_sum':metric['invalid_or_rejected_sum'],
    }
    summary.append(new)
    print(json.dumps({**{key:new[key] for key in ['rate','tps','tps_change_percent','p99_ms','cpu_cores_mean',
                      'network_gbps_mean','peak_resident_gib','db_ms_per_call','execution_cache_hit_fraction',
                      'grouped_blocks_per_batch','measurement_samples','measurement_seconds']},
        'reorg_per_s':phases['reorganizations'],'fork_reexecute_tx_per_s':phases['fork_reexecute_transactions'],
        'candidate_queue_work_ms_per_s':phases['block_queue_wait_ms'],
        'metadata_queue_work_ms_per_s':phases['metadata_queue_wait_ms'],
        'reply_queue_ms_per_s':phases['block_reply_wait_ms'],'signatures_per_s':phases['signature_checks'],
        'duplicates_per_s':phases['duplicate_block_work'],
        'pool_refresh_ms_per_s':phases['pool_refresh_ms'],
        'host_disk_busy_percent':statistics.mean(h['disk_busy_percent'] for h in host_rows if h['disk_busy_percent'] is not None),
        'host_idle_percent':statistics.mean(h['cpu_idle_percent'] for h in host_rows if h['cpu_idle_percent'] is not None)},ensure_ascii=False))
(ROOT/'work/a5-ten-analysis.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
