"""Validate an immutable Linux release before atomically making it available."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile


def Digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def Relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or path == PurePosixPath('.'):
        raise ValueError(f'Unsafe release member: {value!r}')
    return path


def Validate(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 1 or not re.fullmatch(r'[0-9a-f]{40}', manifest.get('source_commit', '')):
        raise ValueError('Manifest requires schema=1 and the full source_commit')
    declared = set()
    for entry in manifest['files']:
        relative = str(Relative(entry['path']))
        if relative in declared or relative in ('manifest.json', '.chain-release.json'):
            raise ValueError('Duplicate or reserved manifest member')
        declared.add(relative)
        path = directory / relative
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
            raise ValueError(f'Invalid manifest file: {relative}')
        if Digest(path) != entry['sha256']:
            raise ValueError(f'File checksum mismatch: {relative}')
        mode = str(entry.get('mode', '0644'))
        if mode not in ('0644', '0755'):
            raise ValueError(f'Unsafe release permissions: {relative}')
        os.chmod(path, int(mode, 8))
    required = {'bin/boost', 'bin/sender', 'source/Test/ClusterWorker.py', 'source/Test/Netem.py'}
    if not required.issubset(declared):
        raise ValueError('Missing executable or existing ClusterWorker/Netem source')
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob('*')
              if path.is_file()} - {'manifest.json', '.chain-release.json'}
    if actual != declared or any(path.is_symlink() for path in directory.rglob('*')):
        raise ValueError('Undeclared files or symbolic links in release')
    for binary in ('boost', 'sender'):
        path = directory / 'bin' / binary
        with path.open('rb') as source:
            if source.read(4) != b'\x7fELF':
                raise ValueError('Linux releases must contain ELF binaries')
        if not os.access(path, os.X_OK):
            raise ValueError('Release executables must have mode 0755')
    return manifest


def Stage(archive, release_root, release_id, expected):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', release_id):
        raise ValueError('Unsafe release identifier')
    if not re.fullmatch(r'[0-9a-f]{64}', expected) or Digest(archive) != expected:
        raise ValueError('Package SHA256 mismatch')
    root = Path(release_root).resolve()
    if not Path(release_root).is_absolute() or root == Path(root.anchor):
        raise ValueError('Release root must be an absolute managed directory')
    root.mkdir(parents=True, exist_ok=True)
    destination = root / release_id
    if destination.is_symlink() or destination.resolve().parent != root:
        raise ValueError('Release destination escapes its managed root')
    if destination.exists():
        marker = json.loads((destination / '.chain-release.json').read_text())
        if marker.get('package_sha256') != expected:
            raise ValueError('Release ID already identifies another package')
        manifest = Validate(destination)
        return {'changed': False, 'directory': str(destination), 'source_commit': manifest['source_commit'],
                'binary_sha256': Digest(destination / 'bin' / 'boost'), 'package_sha256': expected}
    temporary = Path(tempfile.mkdtemp(prefix=f'.stage-{release_id}-', dir=root))
    try:
        with tarfile.open(archive, 'r:gz') as package:
            members = package.getmembers()
            if len(members) > 100000 or any(member.size < 0 for member in members) or sum(member.size for member in members) > 4 * 1024**3:
                raise ValueError('Release package exceeds extraction budget')
            seen = set()
            for member in members:
                if member.name in ('.', './') and member.isdir():
                    continue
                name = str(Relative(member.name))
                if name in seen or name == '.chain-release.json' or not (member.isfile() or member.isdir()):
                    raise ValueError('Duplicate, linked or special archive member')
                seen.add(name)
                target = temporary / name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                os.chmod(target, 0o644)
        manifest = Validate(temporary)
        (temporary / '.chain-release.json').write_text(json.dumps({
            'schema': 1, 'release_id': release_id, 'package_sha256': expected,
            'source_commit': manifest['source_commit']}, indent=2) + '\n')
        temporary.rename(destination)
        return {'changed': True, 'directory': str(destination), 'source_commit': manifest['source_commit'],
                'binary_sha256': Digest(destination / 'bin' / 'boost'), 'package_sha256': expected}
    finally:
        if temporary.exists():
            if temporary.parent != root or not temporary.name.startswith('.stage-'):
                raise ValueError('Refusing to clean an unrecognized staging directory')
            shutil.rmtree(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True)
    parser.add_argument('--release-root', required=True)
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(Stage(args.archive, args.release_root, args.release_id, args.sha256)))
