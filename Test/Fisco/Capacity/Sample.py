"""Bounded resource sampling for one named, isolated node and sender."""
import json
import os
from pathlib import Path
import sys
import time

name, output = sys.argv[1:]
hz = os.sysconf('SC_CLK_TCK')
with Path(output).open('w', buffering=1) as stream:
    for _ in range(1200):
        rows = []
        for p in Path('/proc').glob('[0-9]*'):
            try:
                command = (p/'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
                cwd = os.readlink(p/'cwd')
                node = cwd == '/var/tmp/fisco-experiments/' + name and 'fisco-bcos' in command
                driver = 'NativeAlignedBench' in command and '/'+name+'/' in command
                if not (node or driver):
                    continue
                stat = (p/'stat').read_text().split(') ', 1)[1].split()
                io = dict(line.split(':') for line in (p/'io').read_text().splitlines())
                threads = {}
                if node:
                    for task in (p/'task').iterdir():
                        try:
                            label = (task/'comm').read_text().strip()
                            v = (task/'stat').read_text().split(') ', 1)[1].split()
                            threads[label] = threads.get(label, 0) + (int(v[11])+int(v[12]))/hz
                        except (OSError, ValueError):
                            pass
                rows.append(dict(pid=int(p.name), role='node' if node else 'driver',
                    cpu_s=(int(stat[11])+int(stat[12]))/hz,
                    rss_bytes=int(stat[21])*os.sysconf('SC_PAGE_SIZE'),
                    read_bytes=int(io['read_bytes']), write_bytes=int(io['write_bytes']),
                    threads=int(stat[17]), thread_cpu_s=threads))
            except (OSError, ValueError, IndexError):
                pass
        stream.write(json.dumps(dict(epoch=time.time(), processes=rows,
            cpu=Path('/proc/stat').read_text().splitlines()[0],
            memory=Path('/proc/meminfo').read_text(),
            net=Path('/proc/net/dev').read_text(), disks=Path('/proc/diskstats').read_text()))+'\n')
        time.sleep(1)
