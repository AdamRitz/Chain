"""Verify non-SM FISCO TLS certificates and repair an existing CA with its key."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile


CA_EXTENSIONS = ('[v3_ca]\nbasicConstraints=critical,CA:TRUE\n'
                 'keyUsage=critical,keyCertSign,cRLSign\n'
                 'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n')


def run(openssl, *arguments):
    return subprocess.run([str(openssl), *map(str, arguments)], check=True,
                          text=True, capture_output=True).stdout


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def paths(root, hosts):
    generated = Path(root) / 'generated'
    ca = generated / 'ca/ca.crt'
    copies, leaves = [ca], []
    for host in hosts:
        for directory, name in [(generated / host / 'node0/conf', 'ssl'),
                                (generated / host / 'sdk', 'sdk')]:
            copies.append(directory / 'ca.crt')
            leaves.append(directory / (name + '.crt'))
            for filename in ('ca.crt', name + '.crt', name + '.key'):
                path = directory / filename
                if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                    raise ValueError('Missing TLS material: ' + str(path))
    if ca.is_symlink() or not ca.is_file():
        raise ValueError('Missing root CA: ' + str(ca))
    return ca, copies, leaves


def verify_generated(root, hosts, openssl, ca_override=None, check_copies=True):
    ca, copies, leaves = paths(root, hosts)
    selected = Path(ca_override) if ca_override else ca
    extensions = run(openssl, 'x509', '-in', selected, '-noout', '-ext', 'basicConstraints,keyUsage')
    if 'CA:TRUE' not in extensions or 'Certificate Sign' not in extensions or 'CRL Sign' not in extensions:
        raise ValueError('Root CA requires CA:TRUE, keyCertSign and cRLSign; use prepare --repair-ca')
    if check_copies and any(sha(path) != sha(selected) for path in copies):
        raise ValueError('TLS CA copies differ')
    run(openssl, 'verify', '-check_ss_sig', '-CAfile', selected, selected, *leaves)
    return {'ok': True, 'ca_sha256': sha(selected), 'leaf_certificates': len(leaves),
            'hosts': len(hosts), 'tls_verification': True}


def repair_generated(root, hosts, openssl):
    """Preserve the CA key, subject, serial and subject-key identifier."""
    root = Path(root)
    ca, copies, leaves = paths(root, hosts)
    key = ca.with_name('ca.key')
    if key.is_symlink() or not key.is_file():
        raise ValueError('CA repair requires the original ca.key')
    old_sha = sha(ca)
    if any(sha(path) != old_sha for path in copies):
        raise ValueError('CA copies differ before repair')
    try:
        return {**verify_generated(root, hosts, openssl), 'repaired': False}
    except (ValueError, subprocess.CalledProcessError):
        pass
    cert_public = run(openssl, 'x509', '-in', ca, '-noout', '-pubkey').strip()
    key_public = run(openssl, 'pkey', '-in', key, '-pubout').strip()
    if cert_public != key_public:
        raise ValueError('CA certificate and key do not match')
    serial = run(openssl, 'x509', '-in', ca, '-noout', '-serial').strip().split('=', 1)[1]
    ski = run(openssl, 'x509', '-in', ca, '-noout', '-ext', 'subjectKeyIdentifier')
    identifiers = re.findall(r'(?m)^\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2})+)\s*$', ski)
    extensions = CA_EXTENSIONS
    if identifiers:
        extensions = extensions.replace('subjectKeyIdentifier=hash', 'subjectKeyIdentifier=' + identifiers[0])
    with tempfile.TemporaryDirectory(prefix='.repair-ca-', dir=ca.parent) as temporary:
        temporary = Path(temporary)
        config, request, repaired = temporary / 'ca.cnf', temporary / 'ca.csr', temporary / 'ca.crt'
        config.write_bytes(extensions.encode())
        run(openssl, 'x509', '-x509toreq', '-in', ca, '-signkey', key, '-out', request)
        run(openssl, 'x509', '-req', '-in', request, '-signkey', key, '-set_serial', '0x' + serial,
            '-days', '36500', '-sha256', '-extfile', config, '-extensions', 'v3_ca', '-out', repaired)
        verify_generated(root, hosts, openssl, ca_override=repaired, check_copies=False)
        original = ca.with_name('ca.before-repair.crt')
        if original.exists() and original.read_bytes() != ca.read_bytes():
            raise ValueError('A different CA backup already exists')
        if not original.exists():
            original.write_bytes(ca.read_bytes())
        content = repaired.read_bytes()
        for path in copies:
            pending = path.with_name(path.name + '.repairing')
            pending.write_bytes(content)
            pending.replace(path)
    result = {**verify_generated(root, hosts, openssl), 'repaired': True, 'previous_ca_sha256': old_sha,
              'ca_key_preserved': True, 'leaf_certificates_preserved': True}
    (root / 'ca-repair.json').write_bytes((json.dumps(result, indent=2) + '\n').encode())
    return result
