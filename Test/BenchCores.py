"""Physical-core affinity benchmark: native transfers and compiled Solidity storage calls.

Install psutil and matplotlib. Every run uses a fresh database; one physical core
is reserved for the load generator. Node affinity uses one logical CPU per core.
"""
import argparse
import ctypes
import csv
import statistics
import winreg
import psutil
from EvmNetworkTest import Contract, WaitReceipt
from NetworkTest import *

def PhysicalCores():
    function=ctypes.windll.kernel32.GetLogicalProcessorInformationEx
    function.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
    length=ctypes.c_ulong()
    function(0,None,ctypes.byref(length))
    buffer=ctypes.create_string_buffer(length.value)
    if not function(0,buffer,ctypes.byref(length)): raise ctypes.WinError()
    data=buffer.raw; offset=0; cores=[]
    while offset<len(data):
        relationship,size=struct.unpack_from('<II',data,offset)
        if not size: raise RuntimeError('Invalid processor topology')
        if relationship==0:
            efficiency=data[offset+9]
            groups=struct.unpack_from('<H',data,offset+30)[0]
            if groups!=1: raise RuntimeError('This benchmark currently uses one Windows processor group')
            mask,group=struct.unpack_from('<QH',data,offset+32)
            if group: raise RuntimeError('Unsupported processor group')
            cores.append({'logical_cpus':[i for i in range(64) if mask>>i&1],'efficiency_class':efficiency})
        offset+=size
    return sorted(cores,key=lambda c:(-c['efficiency_class'],c['logical_cpus'][0]))

def VerifyEvmAccounts(port,dataset,genesis,deployerKey,count):
    config=json.loads(Path(genesis).read_text())
    expected={item['public_key']:0 for item in config['accounts']}; expected[deployerKey]=1
    with Path(dataset).open('rb') as source:
        for _ in range(count):
            length=struct.unpack('<I',source.read(4))[0]; tx=source.read(length)
            assert len(tx)==length and length>=221
            key=tx[:32].hex(); nonce=struct.unpack_from('<Q',tx,72)[0]
            assert nonce==expected[key]+1
            expected[key]=nonce
    total=0
    for key,nonce in expected.items():
        account=GetAccount(port,key); assert account['exists'] and account['nonce']==nonce
        total+=account['balance']
    stats=GetStats(port); supply=sum(item['balance'] for item in config['accounts'])
    assert total+stats['user_burned']==supply and total==stats['user_supply']
    return {'verified_users':len(expected),'native_balance_total':total,'burned':stats['user_burned'],'genesis_supply':supply}

def RunCase(binary,baseline,work,dataset,genesis,count,cores,repeat,workload,loadCpus,deploy=None):
    label=f'{workload}-{cores["count"]}c-r{repeat}'
    node=StartNode(binary,work,label,**{'genesis':genesis,'io-threads':min(4,cores['count']),
        'verify-threads':cores['count'],'max-block-txs':256 if workload=='evm' else 6000,
        'block-ms':10 if workload=='evm' else 50,'run-seconds':300,'sync':1})
    process=psutil.Process(node['process'].pid); process.cpu_affinity(cores['cpus'])
    sender=None
    try:
        if deploy:
            command=[baseline/'evm_sender.exe','--wallet',deploy['wallet'],'--genesis',genesis,
                '--deploy',deploy['code'],'--gas',1000000,'--nonce',1,'--port',node['port']]
            sent=Run(command); receipt=WaitReceipt(node['port'],sent['transaction_hash'])
            assert receipt['status']==0 and receipt['contract']==deploy['address']
        before=GetStats(node['port']); startCpu=sum(process.cpu_times()[:2]); startIo=process.io_counters()
        peakRss=process.memory_info().rss
        command=[str(baseline/('evm_sender.exe' if workload=='evm' else 'sender.exe')),'--file',str(dataset),
            '--count',str(count),'--batch','64' if workload=='evm' else '128','--connections','8','--port',str(node['port'])]
        started=time.perf_counter()
        sender=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=FLAGS)
        psutil.Process(sender.pid).cpu_affinity(loadCpus)
        while time.perf_counter()-started<240:
            stats=GetStats(node['port']); peakRss=max(peakRss,process.memory_info().rss)
            if stats['failed'] or stats['invalid'] or stats['rejected']: raise RuntimeError(f'Rejected benchmark transactions: {stats}')
            if stats['committed']-before['committed']==count and stats['pool']==0 and stats['pending']==0: break
            time.sleep(.02)
        else: raise TimeoutError(f'Benchmark timeout: {stats}')
        elapsed=time.perf_counter()-started
        cpu=sum(process.cpu_times()[:2])-startCpu; endIo=process.io_counters()
        stdout,stderr=sender.communicate(timeout=10)
        if sender.returncode: raise RuntimeError(stdout+stderr)
        checks={}
        if workload=='evm':
            actual=Contract(node['port'],deploy['address'])
            assert int(actual['storage']['00'*32],16)==count
            checks={'counter':count,'runtime_code':actual['code']==deploy['runtime']}
            assert checks['runtime_code']
            checks.update(VerifyEvmAccounts(node['port'],dataset,genesis,deploy['public_key'],count))
        else:
            checks=VerifyAccounts(node['port'],dataset,count,genesis)
        result={'workload':workload,'cores':cores['count'],'logical_cpu_ids':cores['cpus'],'repeat':repeat,'count':count,
            'seconds':elapsed,'tps':count/elapsed,'cpu_seconds':cpu,'average_busy_cores':cpu/elapsed,
            'allocated_cpu_percent':100*cpu/elapsed/cores['count'],'peak_rss_bytes':peakRss,
            'process_io_write_bytes':endIo.write_bytes-startIo.write_bytes,
            'db_write_ms':stats['db_write_ms']-before['db_write_ms'],
            'tx_commit_wait_ms':stats['tx_commit_wait_ms']-before['tx_commit_wait_ms'],
            'stats':stats,'checks':checks,'sender':json.loads(stdout.strip().splitlines()[-1])}
        (node['directory']/'measured.json').write_text(json.dumps(result,indent=2))
        return result
    finally:
        if sender is not None and sender.poll() is None: sender.terminate(); sender.communicate(timeout=5)
        StopNode(node)

def Main(args):
    binary=args.bin.resolve(); work=args.work.resolve(); work.mkdir(parents=True,exist_ok=False)
    cores=PhysicalCores(); reserved=cores[0]['logical_cpus']; available=[c['logical_cpus'][0] for c in cores[1:]]
    points=sorted(set([n for n in [1,2,4,8,12,len(available)] if n<=len(available)]))
    if args.cores: points=[int(n) for n in args.cores.split(',')]
    if any(n<1 or n>len(available) for n in points): raise ValueError('Requested more physical cores than available')
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key: cpuName=winreg.QueryValueEx(key,'ProcessorNameString')[0].strip()
    config={'processor':cpuName,'physical_cores':cores,'node_cpus':available,'load_cpus':reserved,'points':points,'repeats':args.repeats,'sync':1,'timing':'sender launch through all transactions persisted locally','native_count':args.native_count,'evm_count':args.evm_count}
    config['node_binary_sha256']=hashlib.sha256((binary/'boost.exe').read_bytes()).hexdigest()
    if args.baseline: config['baseline_binary_sha256']=hashlib.sha256((args.baseline.resolve()/'boost.exe').read_bytes()).hexdigest()
    print(json.dumps(config),flush=True)
    (work/'config.json').write_text(json.dumps(config,indent=2))
    native=work/'transactions.bin'
    Run([binary/'sender.exe','--prepare',native,'--count',args.native_count,'--wallets',1024])
    owner=work/'deployer.wallet.json'
    public=Run([binary/'sender.exe','--create-wallet',owner])['public_key']
    base=work/'evm-base.json'; base.write_text(json.dumps({'base_fee':0,'accounts':[{'public_key':public,'balance':1000000000}]}))
    address=Run([binary/'evm_sender.exe','--wallet',owner,'--address'])['create_address']
    compiled=json.loads((Path(__file__).resolve().parents[1]/'Contracts/Counter.compiled.json').read_text())['contracts']['Counter']['evm']
    evm=work/'evm-transactions.bin'
    Run([binary/'evm_sender.exe','--prepare',evm,'--genesis',base,'--call',address,'--input',compiled['methodIdentifiers']['Increment()'],
        '--count',args.evm_count,'--wallets',64,'--gas',100000])
    deploy={'wallet':owner,'public_key':public,'address':address,'code':compiled['bytecode']['object'],'runtime':compiled['deployedBytecode']['object']}
    results=[]
    for repeat in range(1,args.repeats+1):
        for n in points if repeat%2 else list(reversed(points)):
            selected={'count':n,'cpus':available[:n]}
            modes=['native','evm']
            if args.baseline: modes.insert(0,'previous')
            for workload in modes if repeat%2 else list(reversed(modes)):
                isEvm=workload=='evm'
                result=RunCase(args.baseline.resolve() if workload=='previous' else binary,binary,work,evm if isEvm else native,
                    Path(str(evm if isEvm else native)+'.genesis.json'),args.evm_count if isEvm else args.native_count,
                    selected,repeat,workload,reserved,deploy if isEvm else None)
                results.append(result)
                (work/'results.json').write_text(json.dumps({'config':config,'runs':results},indent=2))
                print(json.dumps({k:result[k] for k in ['workload','cores','repeat','tps','allocated_cpu_percent','db_write_ms']}),flush=True)
    with (work/'runs.csv').open('w',newline='',encoding='utf-8-sig') as output:
        fields=['workload','cores','repeat','count','seconds','tps','cpu_seconds','average_busy_cores','allocated_cpu_percent','db_write_ms','tx_commit_wait_ms','peak_rss_bytes']
        writer=csv.DictWriter(output,fieldnames=fields,extrasaction='ignore'); writer.writeheader(); writer.writerows(results)
    summary=[]
    for mode in ['previous','native','evm']:
        for n in points:
            group=[r for r in results if r['workload']==mode and r['cores']==n]
            if group: summary.append({'workload':mode,'cores':n,'median_tps':statistics.median(r['tps'] for r in group),'min_tps':min(r['tps'] for r in group),'max_tps':max(r['tps'] for r in group),'median_cpu_percent':statistics.median(r['allocated_cpu_percent'] for r in group)})
    (work/'summary.json').write_text(json.dumps(summary,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--bin',type=Path,required=True); parser.add_argument('--baseline',type=Path)
    parser.add_argument('--work',type=Path,required=True); parser.add_argument('--cores'); parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--native-count',type=int,default=300000); parser.add_argument('--evm-count',type=int,default=20000)
    Main(parser.parse_args())
