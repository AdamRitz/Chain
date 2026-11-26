"""Package one fully tested Linux build for distribution without rebuilding each node."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

sys.dont_write_bytecode = True
from vdf_build import Hash, SafePath, Sources


SYSTEM_LIBRARIES = {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2',
                    'librt.so.1', 'libresolv.so.2', 'libutil.so.1', 'libanl.so.1',
                    'libthread_db.so.1'}


def SystemLibrary(name):
    return name in SYSTEM_LIBRARIES or name.startswith(('ld-linux', 'libnss_'))


def CheckElf(path, soname=None):
    environment = dict(os.environ, LC_ALL='C')
    def Read(option):
        return subprocess.check_output(['readelf', option, str(path)], text=True,
                                       env=environment, timeout=60)
    header = Read('-h')
    if not re.search(r'Class:\s+ELF64\b', header) or not re.search(
            r'Machine:\s+Advanced Micro Devices X86-64\b', header):
        raise ValueError('Expected an ELF64 x86-64 runtime file: ' + str(path))
    dynamic = Read('-d')
    names = re.findall(r'\(SONAME\).*?\[([^\]]+)\]', dynamic)
    if soname is not None and names != [soname]:
        raise ValueError('Runtime SONAME differs from file name: ' + str(path))
    needed = re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', dynamic)
    if any('/' in name or '\\' in name or not name for name in needed):
        raise ValueError('Runtime dependencies must use SONAME file names: ' + str(path))
    versions = Read('--version-info')
    tags = set(re.findall(r'\bGLIBC_([A-Za-z0-9_.]+)\b', versions))
    if any(not re.fullmatch(r'\d+(?:\.\d+)+', tag) for tag in tags):
        raise ValueError('Unsupported GLIBC version tag for EL9: ' + str(path))
    glibc = sorted({tuple(map(int, value.split('.'))) for value in tags})
    if glibc and glibc[-1] > (2, 34):
        raise ValueError('Runtime requires GLIBC newer than EL9 2.34: ' + str(path))
    return {'needed': needed, 'soname': names[0] if names else None,
            'glibc_versions': ['.'.join(map(str, value)) for value in glibc]}


def ResolveRuntime(binaries, directory):
    directory = SafePath(directory)
    if not directory.is_dir():
        raise ValueError('runtime-dir must be an existing ordinary directory')
    environment = dict(os.environ, LD_LIBRARY_PATH=str(directory), LC_ALL='C')
    environment.pop('LD_PRELOAD', None)
    environment.pop('LD_AUDIT', None)
    selected, checks = {}, {}
    for binary in binaries:
        checks['bin/' + binary.name] = CheckElf(binary)
        completed = subprocess.run(['ldd', '-r', str(binary)], text=True, capture_output=True,
                                   env=environment, timeout=60)
        text = completed.stdout + completed.stderr
        if completed.returncode or re.search(r'not found|undefined symbol', text, re.I):
            raise ValueError('Runtime link check failed for ' + str(binary) + ': ' + text.strip())
        for name, resolved in re.findall(r'^\s*(\S+)\s+=>\s+(.+?)\s+\(0x[0-9a-f]+\)\s*$', text, re.M | re.I):
            if SystemLibrary(name):
                continue
            if not name.startswith('lib') or '/' in name or '\\' in name:
                raise ValueError('Invalid runtime SONAME: ' + name)
            path = SafePath(directory / name)
            linked = SafePath(Path(resolved))
            if not path.is_file() or not linked.is_file() or linked.resolve() != path.resolve():
                raise ValueError('Dependency must resolve from runtime-dir by SONAME: ' + name)
            selected[name] = path
    for name, path in sorted(selected.items()):
        checks['lib/' + name] = CheckElf(path, name)
    for name, info in checks.items():
        missing = [dependency for dependency in info['needed']
                   if not SystemLibrary(dependency) and dependency not in selected]
        if missing:
            raise ValueError('Unresolved runtime dependencies in ' + name + ': ' + ', '.join(missing))
    return selected, checks


def Pack(release, output, libraries=(), runtime_dir=None):
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
    runtime = None
    if runtime_dir is not None:
        selected, checks = ResolveRuntime([SafePath(release / 'build' / name) for name in binaries], runtime_dir)
        libraries = [*libraries, *selected.values()]
        runtime = {'directory': str(runtime_dir), 'libraries': sorted(selected), 'checks': checks}
    for name in binaries:
        path = SafePath(release / 'build' / name)
        with path.open('rb') as binary:
            elf = binary.read(4)
        if elf != b'\x7fELF':
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
    result = {'package': str(output), 'sha256': Hash(output), 'source_commit': source['source_commit'],
              'binary_sha256': proof['binary_sha256'], 'files': len(files)}
    if runtime is not None:
        result['runtime'] = runtime
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--library', action='append', default=[])
    parser.add_argument('--runtime-dir', type=Path, help='Verified ordinary SONAME files; checks ldd -r and EL9 compatibility')
    args = parser.parse_args()
    print(json.dumps(Pack(args.release_dir, args.output, args.library, args.runtime_dir)))
