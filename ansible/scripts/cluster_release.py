"""Package one fully tested Linux build for distribution without rebuilding each node."""
import argparse
import io
import json
from pathlib import Path
import sys
import tarfile

sys.dont_write_bytecode = True
from vdf_build import Hash, SafePath, Sources


def Pack(release, output, libraries=()):
    release, output = SafePath(release), SafePath(output)
    proof = json.loads(SafePath(release / 'build-proof.json').read_text(encoding='utf-8'))
    source_manifest = SafePath(release / 'source-manifest.json')
    source = json.loads(source_manifest.read_text(encoding='utf-8'))
    if (proof.get('ok') is not True or proof.get('complete') is not True or
            proof.get('source_manifest_sha256') != Sources(release) or
            proof.get('binary_sha256') != Hash(SafePath(release / 'build/boost'))):
        raise ValueError('Build must match its successful complete test proof')
    files = {'build-proof.json': (release / 'build-proof.json', '0644'),
             'source-manifest.json': (source_manifest, '0644')}
    binaries = ['boost', 'sender']
    if SafePath(release / 'build/evm_sender').exists():
        binaries.append('evm_sender')
    if proof.get('settings', {}).get('classroom'):
        binaries.append('chain_classroom')
    for name in binaries:
        path = SafePath(release / 'build' / name)
        if path.read_bytes()[:4] != b'\x7fELF':
            raise ValueError('Only Linux ELF binaries can be distributed')
        files['bin/' + name] = (path, '0755')
    for row in source['files']:
        files['source/' + row['path']] = (SafePath(release / 'source' / row['path']), row.get('mode', '0644'))
    for library in libraries:
        path = SafePath(library)
        name = 'lib/' + path.name
        if not path.is_file() or not path.name.startswith('lib') or name in files:
            raise ValueError('Libraries must be distinct ordinary files named by their runtime SONAME')
        files[name] = (path, '0644')
    manifest = {'schema': 1, 'source_commit': source['source_commit'],
                'files': [{'path': name, 'sha256': Hash(path), 'mode': mode}
                          for name, (path, mode) in sorted(files.items())]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as destination, tarfile.open(fileobj=destination, mode='w:gz') as archive:
        for name, (path, mode) in sorted(files.items()):
            info = archive.gettarinfo(str(path), name)
            info.mode, info.uid, info.gid, info.uname, info.gname = int(mode, 8), 0, 0, '', ''
            with path.open('rb') as stream:
                archive.addfile(info, stream)
        payload = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
        info = tarfile.TarInfo('manifest.json')
        info.size, info.mode = len(payload), 0o644
        archive.addfile(info, io.BytesIO(payload))
    return {'package': str(output), 'sha256': Hash(output), 'source_commit': source['source_commit'],
            'binary_sha256': proof['binary_sha256'], 'files': len(files)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--library', action='append', default=[])
    args = parser.parse_args()
    print(json.dumps(Pack(args.release_dir, args.output, args.library)))
