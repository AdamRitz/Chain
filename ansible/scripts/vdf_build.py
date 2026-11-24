"""Build and test a staged source release without opening an existing node database."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET


def Hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def SafePath(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or path == Path(path.anchor):
        raise ValueError('An absolute non-root path without traversal is required')
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('Symbolic-link paths are not accepted: ' + str(item))
    return path


def ExecutablePath(value):
    path = Path(value)
    # Python venvs and toolsets intentionally link executable files. Their
    # containing directories must still be ordinary managed directories.
    SafePath(path.parent)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('An absolute executable path is required')
    if path.exists() and not path.is_file():
        raise ValueError('Executable path is not a regular file')
    return path


def Save(path, value):
    temporary = path.with_name(path.name + '.tmp')
    if temporary.exists() or temporary.is_symlink() or path.is_symlink():
        raise ValueError('Unexpected build proof path')
    with temporary.open('x', encoding='utf-8') as output:
        json.dump(value, output, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def Sources(release):
    manifest = json.loads(SafePath(release / 'source-manifest.json').read_text(encoding='utf-8'))
    expected = set()
    for row in manifest['files']:
        relative = row['path']
        # The source package records repository-relative paths.
        path = SafePath(release / 'source' / relative)
        if not path.is_relative_to(release / 'source') or not path.is_file():
            raise ValueError('Missing source member: ' + relative)
        if Hash(path) != row['sha256']:
            raise ValueError('Source checksum changed: ' + relative)
        expected.add(relative)
    actual = set()
    for path in (release / 'source').rglob('*'):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise ValueError('Non-regular source member')
        if path.is_file():
            actual.add(path.relative_to(release / 'source').as_posix())
    if actual != expected:
        raise ValueError('Source members differ from their manifest')
    return Hash(release / 'source-manifest.json')


def Run(command, log, environment):
    with log.open('x', encoding='utf-8') as output:
        output.write(json.dumps([str(x) for x in command]) + '\n')
        output.flush()
        completed = subprocess.run([str(x) for x in command], stdout=output,
                                   stderr=subprocess.STDOUT, env=environment, check=False)
    if completed.returncode:
        raise RuntimeError(f'Command failed ({completed.returncode}); see {log}')


def RocksDB(release, supplied):
    if supplied:
        directory = SafePath(supplied)
        configuration = SafePath(directory / 'RocksDBConfig.cmake')
        return directory, Hash(configuration)
    directory = SafePath(release / 'cmake' / 'RocksDB')
    configuration = SafePath(directory / 'RocksDBConfig.cmake')
    text = ('# EL9 shared RocksDB package adapter; does not build or replace RocksDB.\n'
            'find_path(RocksDB_INCLUDE_DIR rocksdb/db.h REQUIRED)\n'
            'find_library(RocksDB_LIBRARY NAMES rocksdb REQUIRED)\n'
            'if(NOT TARGET RocksDB::rocksdb)\n'
            '  add_library(RocksDB::rocksdb SHARED IMPORTED)\n'
            '  set_target_properties(RocksDB::rocksdb PROPERTIES\n'
            '    IMPORTED_LOCATION "${RocksDB_LIBRARY}"\n'
            '    INTERFACE_INCLUDE_DIRECTORIES "${RocksDB_INCLUDE_DIR}")\n'
            'endif()\nset(RocksDB_FOUND TRUE)\n')
    directory.mkdir(parents=True, exist_ok=True)
    if configuration.exists():
        if configuration.read_text(encoding='utf-8') != text:
            raise ValueError('Existing RocksDB adapter differs')
    else:
        with configuration.open('x', encoding='utf-8') as output:
            output.write(text)
    return directory, Hash(configuration)


def Build(args):
    release = SafePath(args.release_dir)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', args.run_id):
        raise ValueError('Invalid build run ID')
    manifest_hash = Sources(release)
    build = SafePath(release / 'build')
    proof_path = SafePath(release / 'build-proof.json')
    rocksdb_dir, rocksdb_hash = RocksDB(release, args.rocksdb_dir)
    if args.target and not args.test_regex:
        raise ValueError('Building selected targets requires an explicit partial test filter')
    if args.classroom and not (release / 'source' / 'Classroom' / 'dist' / 'index.html').is_file():
        raise ValueError('Classroom tests require built frontend assets; pack with --include-frontend')
    boost_dir = SafePath(args.boost_dir) if args.boost_dir else Path('/usr/include')
    boost_version = SafePath(boost_dir / 'boost' / 'version.hpp')
    settings = {'build_type': 'RelWithDebInfo', 'classroom': args.classroom,
                'python': str(ExecutablePath(args.python)), 'cc': str(ExecutablePath(args.cc)),
                'cxx': str(ExecutablePath(args.cxx)), 'required_tests': sorted(set(args.require_test)),
                'rocksdb_dir': str(rocksdb_dir), 'rocksdb_config_sha256': rocksdb_hash,
                'boost_dir': str(boost_dir), 'boost_version_sha256': Hash(boost_version),
                'library_path': args.library_path or ''}
    if proof_path.exists():
        if args.test_regex or args.target:
            raise ValueError('A validated release is immutable; iterate using a new release ID')
        proof = json.loads(proof_path.read_text(encoding='utf-8'))
        if (proof.get('ok') is not True or proof.get('settings') != settings or
                proof.get('source_manifest_sha256') != manifest_hash or
                proof.get('binary_sha256') != Hash(build / 'boost')):
            raise ValueError('Existing completed release differs; use a new release ID')
        return dict(proof, changed=False)
    attempts = SafePath(release / 'build-attempts')
    attempts.mkdir(exist_ok=True)
    attempt = SafePath(attempts / args.run_id)
    attempt.mkdir()  # A failed attempt is evidence; use another run ID to resume.
    tests = SafePath(attempt / 'test-data')
    tests.mkdir()
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if args.library_path:
        environment['LD_LIBRARY_PATH'] = args.library_path
    else:
        environment.pop('LD_LIBRARY_PATH', None)
    result = {'ok': False, 'complete': not bool(args.test_regex), 'run_id': args.run_id, 'settings': settings,
              'source_manifest_sha256': manifest_hash, 'started_unix': time.time(),
              'test_data_root': str(tests), 'evidence': str(attempt),
              'test_regex': args.test_regex, 'targets': args.target}
    try:
        command = ['cmake', '-S', release / 'source', '-B', build, '-G', 'Ninja',
                   '-DCMAKE_BUILD_TYPE=RelWithDebInfo', '-DCHAIN_BUILD_CLASSROOM=' + ('ON' if args.classroom else 'OFF'),
                   '-DCMAKE_C_COMPILER=' + args.cc, '-DCMAKE_CXX_COMPILER=' + args.cxx,
                   '-DPython3_EXECUTABLE=' + args.python, '-DCHAIN_TEST_DATA_ROOT=' + str(tests),
                   '-DCHAIN_BOOST_DIR=' + str(boost_dir), '-DCHAIN_BOOST_INCLUDE_DIR=' + str(boost_dir)]
        command.append('-DRocksDB_DIR=' + str(rocksdb_dir))
        Run([args.cxx, '--version'], attempt / 'compiler-version.log', environment)
        Run(['cmake', '--version'], attempt / 'cmake-version.log', environment)
        Run(command, attempt / 'configure.log', environment)
        build_command = ['cmake', '--build', build, '--parallel', str(args.jobs)]
        if args.target:
            build_command.extend(['--target', *args.target])
        Run(build_command, attempt / 'build.log', environment)
        test_command = ['ctest', '--test-dir', str(build)]
        if args.test_regex:
            test_command.extend(['-R', args.test_regex])
        discovered = subprocess.run([*test_command, '--show-only=json-v1'],
                                    check=True, capture_output=True, text=True, env=environment)
        plan = json.loads(discovered.stdout)
        Save(attempt / 'ctest-plan.json', plan)
        names = {row['name'] for row in plan['tests']}
        missing = set(args.require_test) - names
        if missing:
            raise ValueError('Required tests were not registered: ' + ', '.join(sorted(missing)))
        if not names:
            raise ValueError('No tests match the requested CTest filter')
        if not args.test_regex and len(names) < (24 if args.classroom else 15):
            raise ValueError('Full baseline and classroom test suite was not registered')
        Run([*test_command, '--output-on-failure', '--parallel', '1',
             '--output-junit', attempt / 'ctest.xml'], attempt / 'ctest.log', environment)
        suite = ET.parse(attempt / 'ctest.xml').getroot()
        cases = suite.findall('.//testcase')
        if len(cases) != len(names) or any(any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped'))
                                         for case in cases):
            raise ValueError('CTest did not run every registered test successfully')
        runtime_targets = [] if args.test_regex else ['boost', 'sender', 'evm_sender']
        if not args.test_regex and args.classroom:
            runtime_targets.append('chain_classroom')
        for name in runtime_targets:
            Run(['ldd', build / name], attempt / (name + '-ldd.log'), environment)
            if 'not found' in (attempt / (name + '-ldd.log')).read_text():
                raise RuntimeError('Unresolved runtime library: ' + name)
        if not args.test_regex:
            Run(['readelf', '-S', build / 'boost'], attempt / 'debug-sections.log', environment)
            debug_sections = (attempt / 'debug-sections.log').read_text()
            if '.debug_info' not in debug_sections or '.debug_line' not in debug_sections:
                raise RuntimeError('The node binary is missing source debug information')
        if Sources(release) != manifest_hash:
            raise ValueError('Source manifest changed during build')
        result.update(ok=True, changed=True, binary_sha256=Hash(build / 'boost') if (build / 'boost').is_file() else None,
                      test_count=len(names), tests=sorted(names), finished_unix=time.time())
        if not args.test_regex:
            Save(proof_path, result)
        return result
    except Exception as error:
        result.update(error=str(error), finished_unix=time.time())
        raise
    finally:
        Save(attempt / 'result.json', result)


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--cc', default='/opt/rh/gcc-toolset-13/root/usr/bin/gcc')
    parser.add_argument('--cxx', default='/opt/rh/gcc-toolset-13/root/usr/bin/g++')
    parser.add_argument('--jobs', type=int, choices=range(1, 65), default=1)
    parser.add_argument('--require-test', action='append', default=[])
    parser.add_argument('--rocksdb-dir')
    parser.add_argument('--boost-dir')
    parser.add_argument('--library-path')
    parser.add_argument('--classroom', action='store_true')
    parser.add_argument('--test-regex', default='')
    parser.add_argument('--target', action='append', default=[])
    args = parser.parse_args()
    try:
        import fcntl
        release = SafePath(args.release_dir)
        lock = SafePath(release / '.build.lock')
        with lock.open('a') as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(json.dumps(Build(args)))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        raise SystemExit(1)


if __name__ == '__main__':
    Main()
