import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('fleet_certificates', Path(__file__).with_name('certificates.py'))
certificates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(certificates)
OPENSSL = shutil.which('openssl')


@unittest.skipUnless(OPENSSL, 'OpenSSL executable is required for certificate integration tests')
class CertificateTests(unittest.TestCase):
    def fixture(self, root, valid=False):
        ca = root/'generated/ca'
        ca.mkdir(parents=True)
        config = ca/'bad-ca.cnf'
        config.write_text('[req]\ndistinguished_name=dn\n[dn]\n[bad_ca]\n'
                          'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n' +
                          certificates.CA_EXTENSIONS)
        certificates.run(OPENSSL, 'req', '-new', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=fleet-test-ca/O=fleet', '-keyout', ca/'ca.key', '-out', ca/'ca.crt',
            '-config', config, '-extensions', 'v3_ca' if valid else 'bad_ca')
        certificates.run(OPENSSL, 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=fleet-test-leaf',
            '-keyout', ca/'leaf.key', '-out', ca/'leaf.csr', '-config', config)
        leaf_config = ca/'leaf.cnf'
        leaf_config.write_text('[leaf]\nbasicConstraints=CA:FALSE\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n')
        certificates.run(OPENSSL, 'x509', '-req', '-in', ca/'leaf.csr', '-CA', ca/'ca.crt', '-CAkey', ca/'ca.key',
            '-set_serial', '12', '-days', '1', '-extfile', leaf_config, '-extensions', 'leaf', '-out', ca/'leaf.crt')
        hosts = ['192.0.2.1', '192.0.2.2']
        for host in hosts:
            for directory, prefix in [(root/'generated'/host/'node0/conf', 'ssl'), (root/'generated'/host/'sdk', 'sdk')]:
                directory.mkdir(parents=True)
                for original, name in [('ca.crt','ca.crt'),('leaf.crt',prefix+'.crt'),('leaf.key',prefix+'.key')]:
                    shutil.copyfile(ca/original, directory/name)
        return hosts, ca

    def test_real_openssl_new_ca_extensions_verify_all_leaf_certificates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hosts, _ = self.fixture(root, valid=True)
            self.assertEqual(certificates.verify_generated(root, hosts, OPENSSL)['leaf_certificates'], 4)

    def test_real_openssl_repairs_ca_and_preserves_existing_leaf_certificates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hosts, ca = self.fixture(root)
            leaf = (ca/'leaf.crt').read_bytes()
            key = (ca/'ca.key').read_bytes()
            serial = certificates.run(OPENSSL, 'x509', '-in', ca/'ca.crt', '-noout', '-serial')
            ski = certificates.run(OPENSSL, 'x509', '-in', ca/'ca.crt', '-noout', '-ext', 'subjectKeyIdentifier')
            with self.assertRaises(subprocess.CalledProcessError):
                certificates.run(OPENSSL, 'verify', '-CAfile', ca/'ca.crt', ca/'leaf.crt')
            with self.assertRaisesRegex(ValueError, 'CA:TRUE'):
                certificates.verify_generated(root, hosts, OPENSSL)
            result = certificates.repair_generated(root, hosts, OPENSSL)
            self.assertTrue(result['ok'])
            self.assertTrue(result['repaired'])
            self.assertEqual(result['leaf_certificates'], 4)
            self.assertEqual((ca/'leaf.crt').read_bytes(), leaf)
            self.assertEqual((ca/'ca.key').read_bytes(), key)
            self.assertEqual(certificates.run(OPENSSL, 'x509', '-in', ca/'ca.crt', '-noout', '-serial'), serial)
            self.assertEqual(certificates.run(OPENSSL, 'x509', '-in', ca/'ca.crt', '-noout', '-ext', 'subjectKeyIdentifier'), ski)
            self.assertFalse(certificates.repair_generated(root, hosts, OPENSSL)['repaired'])

    def test_mismatched_ca_copy_is_rejected_before_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hosts, ca = self.fixture(root)
            original = (ca/'ca.crt').read_bytes()
            (root/'generated'/hosts[1]/'sdk/ca.crt').write_bytes(b'foreign CA')
            with self.assertRaisesRegex(ValueError, 'copies differ'):
                certificates.repair_generated(root, hosts, OPENSSL)
            self.assertEqual((ca/'ca.crt').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
