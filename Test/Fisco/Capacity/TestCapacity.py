"""Check distributed workload accounting without a running chain."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from Run import Capacity


class AccountingTest(unittest.TestCase):
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
