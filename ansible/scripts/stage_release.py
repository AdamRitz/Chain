"""Validate an immutable Linux release before atomically making it available."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile


def Digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def ManifestDigest(archive):
    with tarfile.open(archive, 'r:gz') as package:
        member = package.getmember('manifest.json')
        if not member.isfile() or not 0 < member.size <= 16 * 1024**2:
            raise ValueError('Invalid release manifest')
        return hashlib.sha256(package.extractfile(member).read()).hexdigest()


def Relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or path == PurePosixPath('.'):
        raise ValueError(f'Unsafe release member: {value!r}')
    return path


def Validate(directory, repair_permissions=True):
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
        if repair_permissions:
            os.chmod(path, int(mode, 8))
        elif os.name == 'posix' and stat.S_IMODE(path.stat().st_mode) != int(mode, 8):
            raise ValueError(f'Release file permissions changed: {relative}')
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


def VerifyExisting(release_root, release_id, expected, expected_manifest):
    """Check an already staged release without transferring or extracting it."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', release_id):
        raise ValueError('Unsafe release identifier')
    if not all(re.fullmatch(r'[0-9a-f]{64}', value) for value in (expected, expected_manifest)):
        raise ValueError('Invalid package or manifest SHA256')
    requested = Path(release_root)
    root = requested.resolve(strict=True)
    if not requested.is_absolute() or root == Path(root.anchor):
        raise ValueError('Release root must be an absolute managed directory')
    destination = root / release_id
    if destination.is_symlink() or destination.resolve(strict=True).parent != root:
        raise ValueError('Release destination escapes its managed root')
    marker_file = destination / '.chain-release.json'
    if marker_file.is_symlink():
        raise ValueError('Release marker must be an ordinary file')
    marker = json.loads(marker_file.read_text(encoding='utf-8'))
    if (marker.get('schema') != 1 or marker.get('release_id') != release_id or
            marker.get('package_sha256') != expected):
        raise ValueError('Staged release does not match the requested package; run stage first')
    if Digest(destination / 'manifest.json') != expected_manifest:
        raise ValueError('Staged release manifest differs from the requested package')
    manifest = Validate(destination, repair_permissions=False)
    if marker.get('source_commit') != manifest['source_commit']:
        raise ValueError('Staged release source differs from its marker')
    return {'changed': False, 'directory': str(destination), 'source_commit': manifest['source_commit'],
            'binary_sha256': Digest(destination / 'bin' / 'boost'), 'package_sha256': expected}


def DirectoryPermissions(directory):
    """Allow service users to traverse a verified release, preserving its parent."""
    directory = Path(directory)
    # Set the staging root last so extraction stays private until validation and
    # all child directories are ready. mkdtemp creates this root with mode 0700.
    directories = [path for path in directory.rglob('*') if path.is_dir()] + [directory]
    changed = False
    for path in directories:
        before = stat.S_IMODE(path.stat().st_mode)
        if before != 0o755:
            os.chmod(path, 0o755)
            changed |= stat.S_IMODE(path.stat().st_mode) != before
    return changed


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
        changed = DirectoryPermissions(destination)
        return {'changed': changed, 'directory': str(destination), 'source_commit': manifest['source_commit'],
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
        DirectoryPermissions(temporary)
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
    parser.add_argument('--archive')
    parser.add_argument('--verify-existing', action='store_true')
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--release-root', required=True)
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    if args.verify_existing:
        if args.archive:
            parser.error('--verify-existing does not use --archive')
        if not args.manifest_sha256:
            parser.error('--verify-existing requires --manifest-sha256')
        result = VerifyExisting(args.release_root, args.release_id, args.sha256, args.manifest_sha256)
    else:
        if not args.archive:
            parser.error('--archive is required when staging a release')
        result = Stage(args.archive, args.release_root, args.release_id, args.sha256)
    print(json.dumps(result))
