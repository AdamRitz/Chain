"""Five short ten-node cases; remove every stopped case database."""
import concurrent.futures,getpass,json,shlex,sys,time
from pathlib import Path
import CloudSixC4 as bench

WORK=bench.ROOT/'results/quick-20261014-c4a8724'
SCRATCH=bench.ROOT/'scratch-quick-c4a8724'

def CleanDatabases(root,run=None):
    code="""
import json,psutil,shutil,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve();selected=sys.argv[2]
active=[]
for process in psutil.process_iter(['cmdline']):
    args=process.info['cmdline'] or []
    if '--data' in args:active.append(Path(args[args.index('--data')+1]).resolve())
removed=[]
if root.exists():
    for case in root.iterdir():
        if selected and case.name!=selected:continue
        if not case.is_dir() or case.is_symlink():continue
        for node in case.glob('node[0-9][0-9]'):
            database=node/'db'
            if not database.is_dir():continue
            assert not database.is_symlink() and database.resolve()==database and database.resolve().is_relative_to(root)
            assert database.resolve() not in active,'Database is still in use'
            record=json.loads((node/'process.json').read_text());args=record['command']
            assert '--data' in args and Path(args[args.index('--data')+1]).resolve()==database
            shutil.rmtree(database);removed.append(str(database))
print(json.dumps({'removed':len(removed),'paths':removed}))
"""
    def Clean(host):
        return {'host':host,**json.loads(bench.Remote(host,shlex.join([str(bench.PYTHON),'-c',code,str(root),run or ''])))}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:return list(pool.map(Clean,bench.HOSTS))

def Main():
    password=getpass.getpass('CLUSTER_PASSWORD: ')
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(lambda host:bench.Connect(host,password),bench.HOSTS))
        password=None
        WORK.mkdir(exist_ok=False)
        for host in bench.HOSTS:
            if bench.Remote(host,"ss -H -lnt 'sport = :8089'").strip():raise RuntimeError(host+': test port in use')
            digest=bench.Remote(host,shlex.join(['sha256sum',str(bench.NEW/'build/boost')])).split()[0]
            assert digest==bench.NEW_HASH,'Node binary checksum mismatch'
        (WORK/'prior-database-cleanup.json').write_text(json.dumps(CleanDatabases(bench.SCRATCH),indent=2))
        bench.WORK=WORK;bench.SCRATCH=SCRATCH
        sys.path.insert(0,str(bench.NEW/'Test'));import Cluster
        Cluster.Worker=bench.Worker
        plan=json.loads((bench.NEW/'Experiments/plan.json').read_text())
        plan.update({'nodes':10,'repeats':1,'warmup':2,'duration':10,'drain_seconds':5,'poll_seconds':.05,
            'sample_hz':20,'max_backlog_growth':10,'batch':128,'ingress':'broadcast','clock_sync':True,
            'round_ms':50,'candidate_ms':12,'throughput_rates':[20000,30000,40000,50000,60000]})
        (WORK/'plan.json').write_text(json.dumps(plan,indent=2))
        dataset=bench.ROOT/'results/six-20261014-c4a8724/transactions.bin'
        genesis=json.loads(Path(str(dataset)+'.genesis.json').read_text());rows=[]
        for rate in plan['throughput_rates']:
            name=f'new-{rate}-r1';run='six-c4a8724-'+name
            try:bench.RunCase('new',rate,1,plan,dataset,genesis,rows)
            finally:
                (WORK/name/'database-cleanup.json').write_text(json.dumps(CleanDatabases(SCRATCH,run),indent=2))
        print('QUICK_TEST_FINISHED',len(rows),flush=True)
    finally:
        password=None
        for client in bench.CLIENTS.values():client.close()

if __name__=='__main__':Main()
