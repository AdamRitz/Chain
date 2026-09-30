"""One-second Linux counters for this isolated FISCO runtime."""
import json,os,time
from pathlib import Path

ROOT=Path(os.environ.get('FISCO_BENCH_ROOT','/home/ubuntu/fisco-bench/20260930'))
def Read():
    rows=[]
    for path in Path('/proc').glob('[0-9]*'):
        try:
            exe=os.readlink(path/'exe');cmd=(path/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
            if (exe.startswith(str(ROOT)+'/') and exe.endswith('/fisco-bcos')) or ('java' in exe and ' FiscoBench ' in cmd and str(ROOT)+'/' in cmd):
                stat=(path/'stat').read_text().split(') ',1)[1].split()
                io=dict(line.split(':') for line in (path/'io').read_text().splitlines())
                groups={}
                if 'fisco-bcos' in exe:
                    for task in (path/'task').iterdir():
                        try:
                            name=(task/'comm').read_text().strip()
                            values=(task/'stat').read_text().split(') ',1)[1].split()
                            groups[name]=groups.get(name,0)+(int(values[11])+int(values[12]))/os.sysconf('SC_CLK_TCK')
                        except (OSError,ValueError):pass
                rows.append({'pid':int(path.name),'role':'node' if 'fisco-bcos' in exe else 'driver',
                    'cpu_s':(int(stat[11])+int(stat[12]))/os.sysconf('SC_CLK_TCK'),
                    'rss_bytes':int(stat[21])*os.sysconf('SC_PAGE_SIZE'),
                    'read_bytes':int(io['read_bytes']),'write_bytes':int(io['write_bytes']),
                    'threads':int(stat[17]),'thread_cpu_s':groups})
        except (OSError,ValueError,IndexError):pass
    net={}
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        name,fields=line.split(':');v=fields.split()
        if name.strip()=='eth0':net={'rx_bytes':int(v[0]),'tx_bytes':int(v[8]),'rx_packets':int(v[1]),'tx_packets':int(v[9])}
    cpu=[int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
    disks={}
    for line in Path('/proc/diskstats').read_text().splitlines():
        fields=line.split()
        if fields[2]!='vda':continue
        v=list(map(int,fields[3:]));disks[fields[2]]=dict(read_ios=v[0],read_sectors=v[2],read_ms=v[3],
            write_ios=v[4],write_sectors=v[6],write_ms=v[7],busy_ms=v[9],queue_ms=v[10])
    return {'epoch':time.time(),'processes':rows,'net':net,'cpu_ticks':cpu,'disks':disks}

if __name__=='__main__':
    with (ROOT/'resources.jsonl').open('a',buffering=1) as f:
        while not (ROOT/'sample.stop').exists():
            f.write(json.dumps(Read())+'\n');time.sleep(1)
