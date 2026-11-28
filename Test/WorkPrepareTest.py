"""Small offline Work preparation checks; no node processes or private keys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess


def Require(condition, message):
    if not condition:
        raise AssertionError(message)


def Json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def NativePath(path):
    text = str(path.resolve())
    if os.name == 'nt' and not text.startswith('\\\\?\\'):
        text = '\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text
    return Path(text)


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    binary, directory = args.bin.resolve(), args.work.resolve()
    Require(binary.is_file(), 'work_prepare binary missing')
    directory.mkdir(parents=True, exist_ok=False)
    digest = lambda label: hashlib.blake2b(label.encode(), digest_size=32).hexdigest()
    anchors = {'chain_id': digest('WorkPrepare public fixture chain'), 'iterations': 4096,
               'anchors': [{'round': i, 'anchor': digest(f'WorkPrepare public fixture round {i}')} for i in range(3)]}
    identities = [{'id': f'node-{i}', 'public_key': digest(f'WorkPrepare public fixture identity {i}'),
                   'seed_file': 'must-not-be-opened', 'private_key': 'must-not-be-printed'} for i in range(2)]
    manifest, keys, output = directory / 'anchors.json', directory / 'identities.json', directory / 'prepared'
    proof_output = NativePath(output)
    Json(manifest, anchors)
    Json(keys, identities)
    command = [str(binary), '--anchors', str(manifest), '--identities', str(keys), '--out', str(output)]
    results = []

    def Run(arguments=(), success=True):
        result = subprocess.run(command + list(arguments), capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=90)
        Require('must-not-be-' not in result.stdout + result.stderr, 'Unrelated private identity fields leaked')
        Require((result.returncode == 0) == success, f'Unexpected exit {result.returncode}: {result.stderr}')
        return json.loads(result.stdout.strip()) if success else result

    first = Run()
    Require(first['generated'] == 6 and first['reused'] == 0 and first['threads'] == 2, 'Initial generation counts')
    records = []
    for identity in identities:
        previous = bytes(100)
        for entry in anchors['anchors']:
            path = proof_output / 'work-vdf' / anchors['chain_id'] / identity['public_key'] / '4096' / 'proofs' / f"{entry['round']}-{entry['anchor']}.work"
            raw = path.read_bytes()
            Require(len(raw) == 346 and raw[:6] == b'WORK\x01\x00', 'Node-compatible Work length and header')
            Require(struct.unpack_from('<Q', raw, 6)[0] == entry['round'], 'Stored round')
            Require(raw[14:46].hex() == entry['anchor'] and raw[46:146] == previous, 'Historical anchor and sequential output')
            previous = raw[146:246]
            records.append((path, raw))
    Require(not list(proof_output.rglob('*.tmp-*')), 'Temporary files retained after success')
    results.append('six real sequential Work records generated with node-compatible encoding')

    second = Run()
    Require(second['generated'] == 0 and second['reused'] == 6, 'Full resume did not reuse valid records')
    Require(all(path.read_bytes() == raw for path, raw in records), 'Resume changed valid proof bytes')
    results.append('resume verifies and reuses every existing proof')
    missing, saved = records[1]
    missing.unlink()
    resumed = Run()
    Require(resumed['generated'] == 1 and resumed['reused'] == 5 and missing.read_bytes() == saved,
            'Missing middle record was not recreated consistently')
    results.append('missing middle record regenerated and following records reused')

    damaged, original = records[0]
    altered = bytearray(original)
    altered[246] ^= 1
    damaged.write_bytes(altered)
    failed = Run(success=False)
    Require('verification' in failed.stderr and damaged.read_bytes() == altered, 'Invalid existing proof overwritten or accepted')
    damaged.write_bytes(original)
    results.append('invalid existing mathematical proof rejected without replacement')

    changed = dict(anchors, anchors=[dict(anchors['anchors'][0], round=1)])
    Json(manifest, changed)
    Run(success=False)
    Json(manifest, anchors)
    Json(keys, identities + identities[:1])
    Run(success=False)
    Json(keys, identities)
    Run(['--threads', '0'], success=False)
    results.append('nonconsecutive history, duplicate public keys and invalid thread count rejected')

    embedded = dict(anchors, identities=identities)
    Json(manifest, embedded)
    direct = subprocess.run([str(binary), '--anchors', str(manifest), '--out', str(output), '--threads', '1'],
                            capture_output=True, text=True, encoding='utf-8', timeout=90)
    Require(direct.returncode == 0 and json.loads(direct.stdout)['reused'] == 6, 'Embedded public identities unsupported')
    Require('must-not-be-' not in direct.stdout + direct.stderr, 'Private fields leaked from embedded identities')
    results.append('embedded public identities and a single worker supported')
    report = {'status': 'passed', 'checks': results, 'initial': first, 'resume': second}
    Json(directory / 'result.json', report)
    print(json.dumps(report))


if __name__ == '__main__':
    Main()
