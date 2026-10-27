"""Check distributed workload accounting without a running chain."""
import json
from datetime import datetime, timezone, timedelta
import io
import gzip
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from Analyze import LogEvents
from Run import Capacity, CompactCompleted


class AccountingTest(unittest.TestCase):
    def test_completed_evidence_compaction_is_lossless(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);out=root/'case';merged=root/'merged';out.mkdir();merged.mkdir()
            content=b'{"index": 123, "value": "abcdef"}\n'*40000
            for path in [out/'rpc.jsonl',merged/'transaction-hashes.jsonl']:path.write_bytes(content)
            CompactCompleted(out,merged)
            rows=json.loads((out/'compressed-records.json').read_text())
            self.assertEqual(len(rows),2)
            for row in rows:
                self.assertFalse(Path(row['path']).exists())
                with gzip.open(row['compressed'],'rb') as stream:self.assertEqual(stream.read(),content)

    def test_timeout_analysis_excludes_setup_and_audit(self):
        lines = [
            'warning|2026-10-26 14:00:01.000000|pbft|After onTimeout',
            'warning|2026-10-26 14:00:02.000000|pbft|verify proposal exceptioned msg=timeout',
            'warning|2026-10-26 14:00:07.000000|pbft|After onTimeout',
            'warning|2026-10-26 14:00:12.000000|pbft|After onTimeout',
        ]
        content=('\n'.join(lines)+'\n').encode()
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w') as tar:
            info=tarfile.TarInfo('node/log/log_2026102614.00.log');info.size=len(content)
            tar.addfile(info,io.BytesIO(content))
        buffer.seek(0)
        start=datetime(2026,10,26,14,0,2,tzinfo=timezone(timedelta(hours=8))).timestamp()
        with tarfile.open(fileobj=buffer,mode='r') as tar:
            result=LogEvents(tar,start,start+10)
        self.assertEqual(result['counts'],dict(view_timeout=1,proposal_verify_timeout=1,proposal_fetch_timeout=0))
        self.assertEqual(len(result['examples']),2)

    def test_shards_reassemble_original_ring_and_outcomes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            c=Capacity.__new__(Capacity)
            c.capacity=root/'remote';c.driver=root/'merged'
            (c.driver/'runs').mkdir(parents=True)
            c.hosts=[str(i) for i in range(10)]
            source=root/'source';out=root/'out';out.mkdir()
            for shard,host in enumerate(c.hosts):
                p=source/host;p.mkdir(parents=True)
                (p/'driver.json').write_text(json.dumps({'requested':3,'shard':shard}))
                (p/'driver.log').write_text('')
                (p/'outcomes.bin').write_bytes(bytes((shard%3,(shard+1)%3,(shard+2)%3)))
                (p/'transaction-hashes.jsonl').write_text(''.join(json.dumps({'index':i*10+shard,'hash':str(i*10+shard)})+'\n' for i in range(3)))
            c.Download=lambda host,remote,local:shutil.copyfile(source/host/Path(remote).name,local)
            result=c.Gather('test',out)
            self.assertEqual(result['requested'],30)
            combined=(c.driver/'runs/test/outcomes.bin').read_bytes()
            for i in range(30):self.assertEqual(combined[i],((i%10)+(i//10))%3)
            indices=[json.loads(line)['index'] for line in (c.driver/'runs/test/transaction-hashes.jsonl').read_text().splitlines()]
            self.assertEqual(sorted(indices),list(range(30)))
            self.assertEqual(sorted(i for shard in range(10) for i in range(shard,1024,10)),list(range(1024)))
            routes=[((i*10+shard)%1024,((i*10+shard)+1)%1024) for i in range(103) for shard in range(10)]
            self.assertEqual(routes,[(i%1024,(i+1)%1024) for i in range(1030)])


if __name__=='__main__':unittest.main()
