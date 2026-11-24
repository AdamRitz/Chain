"""Package a Git working-tree source snapshot and stage it without overwriting source.

source_commit records the base commit, not a claim that the archive equals that
commit. The manifest and package SHA256 identify the actual working-tree bytes.
Excluded tracked paths are reported by name in the JSON result, never read.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile


MAX_FILES = 100000
MAX_FILE_BYTES = 256 * 1024**2
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_MANIFEST_BYTES = 16 * 1024**2
SHA256 = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')
EXCLUDED_DIRS = {'.git', '.venv', 'venv', 'node_modules', 'build', 'data', 'db',
                 'database', 'databases', 'dist', 'target', '__pycache__',
                 '.aws', '.ssh', '.azure', '.config', 'cmakefiles'}
EXCLUDED_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.keystore', '.wallet',
                     '.db', '.sqlite', '.sqlite3', '.sst', '.blob', '.wal', '.log', '.pyc', '.pyo',
                     '.exe', '.dll', '.so', '.dylib', '.o', '.obj', '.a', '.lib',
                     '.pdb', '.zip', '.tar', '.gz', '.7z'}
CREDENTIAL = re.compile(r'(^|[._-])(credentials?|secrets?|passwords?|passwd|tokens?|private[._-]?key)([._-]|$)')
RESERVED = re.compile(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', re.I)


def Relative(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid source relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(c in value for c in '\\:<>"|?*'):
        raise ValueError(f'Unsafe source path: {value!r}')
    if any(part in ('.', '..') or part.endswith(('.', ' ')) or RESERVED.match(part) for part in path.parts):
        raise ValueError(f'Unsafe source path: {value!r}')
    if value == '.':
        raise ValueError('Source path must name a file or directory')
    return path


def Excluded(value, frontend=False):
    original = Relative(value).parts
    parts = [part.lower() for part in original]
    for index, part in enumerate(parts):
        directory = index < len(parts) - 1
        source_db = directory and index == 0 and original[index] == 'DB'
        frontend_dist = frontend and index == 1 and value.startswith('Classroom/dist/') and part == 'dist'
        if not source_db and not frontend_dist and (
                part in EXCLUDED_DIRS or (directory and part.startswith(('build-', 'cmake-build-')))):
            return True
        if (part.startswith(('.env', 'id_rsa', 'id_ed25519'))
                or part in ('authorized_keys', 'known_hosts') or '.local.' in part
                or part.endswith('.local') or CREDENTIAL.search(part)):
            return True
    # DB/ is the repository's C++ source directory, but RocksDB files remain
    # runtime data even if somebody writes them directly into that directory.
    if parts[-1] in ('current', 'identity', 'lock', 'log') or re.fullmatch(r'(?:manifest|options)-[0-9]+|log\.old(?:\..*)?', parts[-1]):
        return True
    return PurePosixPath(parts[-1]).suffix in EXCLUDED_SUFFIXES


def NoLinks(path):
    """Check every existing component, including Windows junction/reparse points."""
    path = Path(path)
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Symbolic link or reparse point in path: {item}')
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f'Non-directory parent: {item}')


def Absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or path == Path(path.anchor):
        raise ValueError('Expected an absolute path below a filesystem root')
    NoLinks(path)
    return path


def OpenRegular(path):
    path = Path(path)
    NoLinks(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f'Expected regular file: {path}')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError(f'File changed while opening: {path}')
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def HashStream(stream):
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def Digest(path):
    with OpenRegular(path) as source:
        return HashStream(source)[0]


def Git(source, *arguments):
    result = subprocess.run(['git', '-C', str(source), *arguments], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout


def CheckCommit(value):
    if not isinstance(value, str) or not COMMIT.fullmatch(value):
        raise ValueError('source_commit must be 40 lowercase hexadecimal characters')


def AddInfo(archive, name, data, mode):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), int(mode, 8), 0
    archive.addfile(info, io.BytesIO(data))


def Parents(paths):
    return {parent.as_posix() for value in paths for parent in PurePosixPath(value).parents
            if parent != PurePosixPath('.')}


def CheckNames(paths):
    paths = set(paths)
    directories = Parents(paths)
    if paths & directories:
        raise ValueError('Source file/directory path collision')
    seen = {}
    for name in paths | directories:
        folded = name.casefold()
        if folded in seen and seen[folded] != name:
            raise ValueError('Case-colliding source file or directory paths')
        seen[folded] = name


def Pack(source, output, source_commit, include=(), include_frontend=False):
    CheckCommit(source_commit)
    source = Absolute(Path(source).absolute())
    output = Absolute(output)
    if output.exists():
        raise FileExistsError(f'Package already exists: {output}')
    if not output.parent.is_dir():
        raise ValueError('Package parent directory must already exist')
    root = Path(os.fsdecode(Git(source, 'rev-parse', '--show-toplevel')).strip()).resolve()
    if root != source.resolve():
        raise ValueError('--source must be the Git repository root')
    head = Git(source, 'rev-parse', 'HEAD').decode('ascii').strip()
    if head != source_commit:
        raise ValueError('source_commit must identify the current Git HEAD base')
    tracked = {}
    for record in Git(source, 'ls-files', '--stage', '-z').split(b'\0'):
        if not record:
            continue
        metadata, raw_name = record.split(b'\t', 1)
        mode, _, stage = metadata.decode('ascii').split()
        name = os.fsdecode(raw_name)
        Relative(name)
        if stage != '0' or name in tracked or mode not in ('100644', '100755'):
            raise ValueError(f'Unmerged, linked or non-file Git entry: {name}')
        # Missing tracked files are errors even when normally excluded.
        path = source / name
        NoLinks(path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(f'Expected tracked regular file: {name}')
        tracked[name] = '0755' if mode == '100755' else '0644'
    excluded = sorted(name for name in tracked if Excluded(name, include_frontend))
    selected = {name: mode for name, mode in tracked.items() if not Excluded(name, include_frontend)}
    included = list(include)
    if include_frontend:
        frontend = source / 'Classroom/dist'
        NoLinks(frontend / 'index.html')
        if not (frontend / 'index.html').is_file():
            raise ValueError('--include-frontend requires Classroom/dist/index.html; build the frontend first')
        for current, directories, filenames in os.walk(frontend, followlinks=False):
            for name in directories + filenames:
                path = Path(current) / name
                NoLinks(path)
                relative = path.relative_to(source).as_posix()
                if Excluded(relative, True):
                    raise ValueError(f'Unsafe frontend asset path: {relative}')
                info = path.lstat()
                if stat.S_ISREG(info.st_mode):
                    included.append(relative)
                elif not stat.S_ISDIR(info.st_mode):
                    raise ValueError(f'Non-regular frontend asset: {relative}')
    for name in included:
        Relative(name)
        if Excluded(name, include_frontend):
            raise ValueError(f'Explicit include is not a source path: {name}')
        path = source / name
        NoLinks(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'Explicit include is not a regular file: {name}')
        selected.setdefault(name, '0755' if info.st_mode & 0o111 else '0644')
    if not selected or len(selected) > MAX_FILES:
        raise ValueError('Empty, oversized or case-colliding source file set')
    CheckNames(selected)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.vdf-pack-', dir=output.parent)
    temporary = Path(temporary_name)
    try:
        entries, total = [], 0
        with os.fdopen(descriptor, 'wb') as raw, tarfile.open(fileobj=raw, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
            for name, mode in sorted(selected.items()):
                with OpenRegular(source / name) as file:
                    # Read bounded bytes once: manifest and tar record the same snapshot.
                    data = file.read(MAX_FILE_BYTES + 1)
                total += len(data)
                if len(data) > MAX_FILE_BYTES or total > MAX_SOURCE_BYTES:
                    raise ValueError('Source package exceeds size budget')
                entries.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
                                'mode': mode, 'bytes': len(data)})
                AddInfo(archive, 'source/' + name, data, mode)
            manifest = {'schema': 1, 'source_commit': source_commit, 'source_commit_kind': 'base',
                        'dirty_worktree': True, 'frontend_included': include_frontend, 'files': entries}
            encoded = (json.dumps(manifest, indent=2, ensure_ascii=True) + '\n').encode('utf-8')
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise ValueError('Source manifest exceeds size budget')
            AddInfo(archive, 'manifest.json', encoded, '0644')
        NoLinks(output)
        # A hard link publishes the completed file without an overwrite race.
        os.link(temporary, output)
        return {'changed': True, 'package': str(output), 'package_sha256': Digest(output),
                'source_commit': source_commit, 'files': len(entries), 'bytes': total,
                'excluded_tracked': excluded}
    finally:
        temporary.unlink(missing_ok=True)


def ObjectPairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate manifest JSON key: {key}')
        result[key] = value
    return result


def Manifest(data, source_commit):
    manifest = json.loads(data.decode('utf-8'), object_pairs_hook=ObjectPairs)
    if not isinstance(manifest, dict) or type(manifest.get('schema')) is not int or manifest['schema'] != 1:
        raise ValueError('Source manifest requires schema=1')
    if manifest.get('source_commit') != source_commit or manifest.get('source_commit_kind') != 'base' or manifest.get('dirty_worktree') is not True:
        raise ValueError('Source manifest commit or working-tree provenance mismatch')
    frontend = manifest.get('frontend_included', False)
    if type(frontend) is not bool:
        raise ValueError('frontend_included must be a boolean')
    files = manifest.get('files')
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ValueError('Source manifest requires a bounded nonempty files list')
    entries, folded, total = {}, set(), 0
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256', 'mode', 'bytes'}:
            raise ValueError('Invalid source manifest file entry')
        name = str(Relative(entry['path']))
        if Excluded(name, frontend) or name.casefold() in folded:
            raise ValueError('Excluded or duplicate source manifest path')
        if (not isinstance(entry['sha256'], str) or not SHA256.fullmatch(entry['sha256'])
                or entry['mode'] not in ('0644', '0755') or type(entry['bytes']) is not int
                or not 0 <= entry['bytes'] <= MAX_FILE_BYTES):
            raise ValueError('Invalid source manifest hash, mode or bytes')
        total += entry['bytes']
        entries[name] = entry
        folded.add(name.casefold())
    if total > MAX_SOURCE_BYTES:
        raise ValueError('Source manifest exceeds budget or has file/directory collisions')
    CheckNames(entries)
    if frontend and 'Classroom/dist/index.html' not in entries:
        raise ValueError('Frontend source manifest requires Classroom/dist/index.html')
    return manifest, entries


def ValidateArchive(archive, source_commit):
    members, folded, total = {}, set(), 0
    for member in archive:
        name = str(Relative(member.name))
        if (name.casefold() in folded or len(members) >= MAX_FILES * 4 or member.size < 0
                or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
                or member.linkname or member.sparse is not None):
            raise ValueError('Duplicate, linked, special or oversized source archive member')
        if member.isdir():
            if member.size != 0 or member.mode != 0o755:
                raise ValueError('Invalid source directory size or permissions')
        elif member.size > (MAX_MANIFEST_BYTES if name == 'manifest.json' else MAX_FILE_BYTES) or member.mode not in (0o644, 0o755):
            raise ValueError('Invalid source file size or permissions')
        total += member.size
        if total > MAX_SOURCE_BYTES + MAX_MANIFEST_BYTES:
            raise ValueError('Source archive exceeds size budget')
        members[name] = member
        folded.add(name.casefold())
    marker = members.get('manifest.json')
    if marker is None or not marker.isfile() or marker.mode != 0o644:
        raise ValueError('Source archive requires a regular manifest.json')
    with archive.extractfile(marker) as stream:
        encoded = stream.read(MAX_MANIFEST_BYTES + 1)
    manifest, entries = Manifest(encoded, source_commit)
    files = {'source/' + name for name in entries} | {'manifest.json'}
    directories = Parents(files)
    if {name for name, member in members.items() if member.isfile()} != files:
        raise ValueError('Archive file set differs from source manifest')
    if not {name for name, member in members.items() if member.isdir()}.issubset(directories):
        raise ValueError('Unexpected source archive directory')
    for name, entry in entries.items():
        member = members['source/' + name]
        if member.size != entry['bytes'] or member.mode != int(entry['mode'], 8):
            raise ValueError(f'Archive metadata mismatch: {name}')
        with archive.extractfile(member) as stream:
            digest, size = HashStream(stream)
        if digest != entry['sha256'] or size != entry['bytes']:
            raise ValueError(f'Archive checksum mismatch: {name}')
    return manifest, entries, members, encoded


def CheckExisting(destination, entries, manifest_bytes, expected):
    NoLinks(destination)
    if not destination.is_dir():
        raise ValueError('Existing release is not a directory')
    if os.name != 'nt' and stat.S_IMODE(destination.stat().st_mode) != 0o755:
        raise ValueError('Existing release directory must have mode 0755; refusing to change permissions')
    for name, data in [('source-manifest.json', manifest_bytes), ('source-package.sha256', (expected + '\n').encode('ascii'))]:
        with OpenRegular(destination / name) as stream:
            if stream.read(len(data) + 1) != data:
                raise ValueError('Existing source release proof mismatch')
    source = destination / 'source'
    NoLinks(source)
    if not source.is_dir():
        raise ValueError('Existing release has no source directory')
    actual_files, actual_dirs = set(), set()
    for current, directories, filenames in os.walk(source, followlinks=False):
        for name in directories + filenames:
            path = Path(current) / name
            NoLinks(path)
            info = path.lstat()
            relative = path.relative_to(source).as_posix()
            if stat.S_ISDIR(info.st_mode):
                actual_dirs.add(relative)
            elif stat.S_ISREG(info.st_mode):
                actual_files.add(relative)
            else:
                raise ValueError('Special file in existing source')
    if actual_files != set(entries) or actual_dirs != Parents(entries):
        raise ValueError('Existing source file/directory set differs from manifest')
    for name, entry in entries.items():
        with OpenRegular(source / name) as stream:
            info = os.fstat(stream.fileno())
            digest, size = HashStream(stream)
        if (digest != entry['sha256'] or size != entry['bytes']
                or (os.name != 'nt' and stat.S_IMODE(info.st_mode) != int(entry['mode'], 8))):
            raise ValueError(f'Existing source checksum, size or mode mismatch: {name}')


def PublishDirectory(temporary, destination):
    """Atomic rename with no replacement, including a newly created empty target."""
    if os.name == 'nt':
        os.rename(temporary, destination)  # Windows rename never replaces a target.
        return
    if not sys.platform.startswith('linux'):
        raise OSError('Atomic source publication is supported on Linux and Windows')
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    operation = getattr(libc, 'renameat2', None)
    if operation is None:
        raise OSError('renameat2 is required for atomic publication without replacement')
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def Stage(package, expected, release_dir, source_commit):
    CheckCommit(source_commit)
    if not isinstance(expected, str) or not SHA256.fullmatch(expected):
        raise ValueError('Expected package SHA256 must be 64 lowercase hexadecimal characters')
    destination = Absolute(release_dir)
    changed = False
    with OpenRegular(package) as raw:
        if HashStream(raw)[0] != expected:
            raise ValueError('Source package SHA256 mismatch')
        raw.seek(0)
        with tarfile.open(fileobj=raw, mode='r:*') as archive:
            _, entries, members, encoded = ValidateArchive(archive, source_commit)
            # Validate every archive byte and source entry before making directories.
            NoLinks(destination)
            if destination.exists():
                CheckExisting(destination, entries, encoded, expected)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                NoLinks(destination.parent)
                lock = destination.parent / ('.vdf-stage-' + destination.name + '.lock')
                # Prevent concurrent invocations from publishing over each other.
                with lock.open('xb'):
                    pass
                temporary = None
                try:
                    NoLinks(destination)
                    if destination.exists():
                        CheckExisting(destination, entries, encoded, expected)
                    else:
                        temporary = Path(tempfile.mkdtemp(prefix='.vdf-stage-', dir=destination.parent))
                        for name, entry in entries.items():
                            target = temporary / 'source' / name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.extractfile(members['source/' + name]) as stream, target.open('xb') as output:
                                shutil.copyfileobj(stream, output, 1024 * 1024)
                            os.chmod(target, int(entry['mode'], 8))
                        (temporary / 'source-manifest.json').write_bytes(encoded)
                        (temporary / 'source-package.sha256').write_bytes((expected + '\n').encode('ascii'))
                        # The dedicated node user must traverse the published release.
                        # Keep extraction private, then expose only the completed tree.
                        os.chmod(temporary, 0o755)
                        CheckExisting(temporary, entries, encoded, expected)
                        raw.seek(0)
                        if HashStream(raw)[0] != expected:
                            raise ValueError('Source package changed during staging')
                        NoLinks(destination)
                        if destination.exists():
                            raise FileExistsError('Release appeared during staging; nothing was overwritten')
                        PublishDirectory(temporary, destination)
                        temporary = None
                        changed = True
                finally:
                    if temporary is not None:
                        # Only remove the temporary directory created by this call.
                        NoLinks(temporary)
                        if temporary.parent != destination.parent or not temporary.name.startswith('.vdf-stage-'):
                            raise ValueError('Refusing unexpected staging cleanup path')
                        shutil.rmtree(temporary)
                    lock.unlink()
    return {'changed': changed, 'directory': str(destination), 'source_dir': str(destination / 'source'),
            'source_commit': source_commit, 'package_sha256': expected}


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    pack = actions.add_parser('pack')
    pack.add_argument('--source', required=True)
    pack.add_argument('--output', required=True)
    pack.add_argument('--source-commit', required=True)
    pack.add_argument('--include', action='append', default=[])
    pack.add_argument('--include-frontend', action='store_true',
                      help='Include existing Classroom/dist static assets after a frontend build')
    stage = actions.add_parser('stage')
    stage.add_argument('--package', required=True)
    stage.add_argument('--sha256', required=True)
    stage.add_argument('--release-dir', required=True)
    stage.add_argument('--source-commit', required=True)
    args = parser.parse_args()
    try:
        result = (Pack(args.source, args.output, args.source_commit, args.include, args.include_frontend) if args.action == 'pack'
                  else Stage(args.package, args.sha256, args.release_dir, args.source_commit))
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        parser.exit(1, f'vdf_source: {error}\n')
    print(json.dumps(result))


if __name__ == '__main__':
    Main()
