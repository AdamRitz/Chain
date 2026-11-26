import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('recover_generated', Path(__file__).with_name('recover_generated.py'))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def test_reuse_identities_and_remove_only_verified_copies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hosts = ['192.168.0.'+str(index) for index in range(1, 101)]
            (root/'hosts.json').write_text(json.dumps(hosts))
            inventory = root/'inventory.json'
            inventory.write_text(json.dumps({'all':{'children':{'chain':{'hosts':{
                'node%03d'%i:{'ansible_host':ip} for i,ip in enumerate(hosts,1)}}}}}))
            (root/'downloads').mkdir()
            (root/'tools').mkdir()
            (root/'bin').mkdir()
            (root/'downloads/build_chain.sh').write_text('official original')
            (root/'tools/build_chain-openssl.sh').write_text('auth_mode=false\nmain "$@"\n')
            (root/'bin/fisco-bcos').write_bytes(b'official binary')
            for index, host in enumerate(hosts,1):
                conf = root/'generated'/host/'node0/conf'
                conf.mkdir(parents=True)
                for name in ('ca.crt','ssl.crt','ssl.key','node.pem'):
                    (conf/name).write_text('existing identity '+str(index))
                (conf/'node.nodeid').write_text('%0128x'%index)
                (conf.parent.parent/'fisco-bcos').write_bytes(b'official binary')
            def execute(argv, **kwargs):
                if argv[0] == 'bash':
                    script = Path(argv[1]).read_text()
                    self.assertEqual(script.count('generate_config false '),100)
                    self.assertNotIn('generate_node_cert',script)
                    self.assertNotIn('generate_node_account',script)
                    for host in hosts:
                        node = root/'generated'/host/'node0'
                        (node/'config.ini').write_text('[storage]\ndata_path=data\n')
                        (node/'config.genesis').write_text('same official genesis')
                        (node/'nodes.json').write_text(json.dumps({'nodes':[ip+':30300' for ip in hosts]}))
                return type('Result',(),{'stdout':'OpenSSL 3.5.9\n'})()
            args = argparse.Namespace(root=str(root),inventory=str(inventory),openssl='/usr/bin/openssl',remove_binary_copies=True)
            unexpected = root/'generated'/hosts[-1]/'fisco-bcos'
            unexpected.write_bytes(b'other binary')
            with patch.object(recovery.subprocess,'run',side_effect=execute), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError): recovery.recover(args)
                self.assertTrue(all((root/'generated'/host/'fisco-bcos').exists() for host in hosts))
                unexpected.write_bytes(b'official binary')
                recovery.recover(args)
            self.assertTrue(all(not (root/'generated'/host/'fisco-bcos').exists() for host in hosts))
            self.assertEqual((root/'bin/fisco-bcos').read_bytes(),b'official binary')
            self.assertTrue(all((root/'generated'/host/'node0/conf/node.pem').exists() for host in hosts))
            saved = json.loads((root/'recovery.json').read_text())
            self.assertEqual(len(saved['binary_copies_removed']),100)
            self.assertTrue(saved['identities_unchanged'])


if __name__ == '__main__': unittest.main()
