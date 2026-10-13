"""Same dataset, affinity, sender and synchronous writes for old/new node builds."""
import argparse,json,sys,statistics,hashlib
from pathlib import Path
parser=argparse.ArgumentParser(); parser.add_argument('--source',type=Path,required=True)
parser.add_argument('--new',type=Path,required=True); parser.add_argument('--old',type=Path,required=True)
parser.add_argument('--work',type=Path,required=True); parser.add_argument('--repeats',type=int,default=3)
parser.add_argument('--cores',default='1,4,8'); args=parser.parse_args()
sys.path.insert(0,str(args.source/'Test')); import BenchCores as b
work=args.work.resolve(); work.mkdir(parents=True,exist_ok=False)
new=args.new.resolve(); old=args.old.resolve(); cores=b.PhysicalCores()
reserved=cores[0]['logical_cpus']; available=[row['logical_cpus'][0] for row in cores[1:]]
points=[int(n) for n in args.cores.split(',')]; assert max(points)<=len(available)
native=work/'native.bin'; count=300000
b.Run([new/b.ExeName('sender'),'--prepare',native,'--count',count,'--wallets',1024])
owner=work/'owner.wallet.json'; public=b.Run([new/b.ExeName('sender'),'--create-wallet',owner])['public_key']
base=work/'evm-base.json'; base.write_text(json.dumps({'base_fee':0,'accounts':[{'public_key':public,'balance':1000000000}]}))
address=b.Run([new/b.ExeName('evm_sender'),'--wallet',owner,'--address'])['create_address']
compiled=json.loads((args.source/'Contracts/Counter.compiled.json').read_text())['contracts']['Counter']['evm']
evm=work/'evm.bin'; evmCount=10000
b.Run([new/b.ExeName('evm_sender'),'--prepare',evm,'--genesis',base,'--call',address,
       '--input',compiled['methodIdentifiers']['Increment()'],'--count',evmCount,'--wallets',64,'--gas',100000])
deploy={'wallet':owner,'public_key':public,'address':address,'code':compiled['bytecode']['object'],'runtime':compiled['deployedBytecode']['object']}
config={'cores':cores,'points':points,'sender_cpus':reserved,'sync':1,'repeats':args.repeats,
        'native_count':count,'evm_count':evmCount,'sha256':{name:hashlib.sha256((binary/b.ExeName('boost')).read_bytes()).hexdigest() for name,binary in [('old',old),('new',new)]}}
(work/'config.json').write_text(json.dumps(config,indent=2)); rows=[]
for name in ['old','new']:(work/name).mkdir()
for repeat in range(1,args.repeats+1):
    for n in points if repeat%2 else points[::-1]:
        for mode,dataset,total in [('native',native,count),('evm',evm,evmCount)]:
            for name,binary in [('old',old),('new',new)] if repeat%2 else [('new',new),('old',old)]:
                result=b.RunCase(binary,new,work/name,dataset,Path(str(dataset)+'.genesis.json'),total,
                                 {'count':n,'cpus':available[:n]},repeat,mode,reserved,deploy if mode=='evm' else None)
                result['version']=name; rows.append(result)
                (work/'results.json').write_text(json.dumps(rows,indent=2))
                print(json.dumps({key:result[key] for key in ['version','workload','cores','repeat','tps','allocated_cpu_percent','db_write_ms']}),flush=True)
summary=[]
for version in ['old','new']:
    for mode in ['native','evm']:
        for n in points:
            group=[row for row in rows if row['version']==version and row['workload']==mode and row['cores']==n]
            summary.append({'version':version,'workload':mode,'cores':n,'median_tps':statistics.median(row['tps'] for row in group),
                            'min_tps':min(row['tps'] for row in group),'max_tps':max(row['tps'] for row in group)})
(work/'summary.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary),flush=True)
