import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'vdf_source.py'
spec = importlib.util.spec_from_file_location('vdf_source', SCRIPT)
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.commit = 'a' * 40
        self.counter = 0
        self.files = {'main.cpp': b'int main() { return 0; }\n', 'scripts/run.sh': b'#!/bin/sh\nexit 0\n'}

    def tearDown(self):
        self.temporary.cleanup()

    def Manifest(self):
        return {'schema': 1, 'source_commit': self.commit, 'source_commit_kind': 'base',
                'dirty_worktree': True, 'files': [
                    {'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                     'mode': '0755' if name.endswith('.sh') else '0644'}
                    for name, data in self.files.items()]}

    def Package(self, manifest=None, extra=(), omit=(), contents=None):
        self.counter += 1
        archive = self.root / f'package-{self.counter}.tar.gz'
        manifest = self.Manifest() if manifest is None else manifest
        encoded = json.dumps(manifest).encode() if not isinstance(manifest, bytes) else manifest
        with tarfile.open(archive, 'w:gz') as output:
            source.AddInfo(output, 'manifest.json', encoded, '0644')
            for name, data in (self.files if contents is None else contents).items():
                if name not in omit:
                    source.AddInfo(output, 'source/' + name, data, '0755' if name.endswith('.sh') else '0644')
            for member, data in extra:
                output.addfile(member, io.BytesIO(data) if member.isfile() else None)
        return archive, source.Digest(archive)

    def Stage(self, archive, digest, directory=None):
        return source.Stage(archive, digest, directory or self.root / 'releases/r1', self.commit)

    def Member(self, name, data=b'', kind=tarfile.REGTYPE, linkname='', mode=None):
        info = tarfile.TarInfo(name)
        info.type, info.linkname = kind, linkname
        info.mode = mode if mode is not None else (0o755 if kind == tarfile.DIRTYPE else 0o644)
        info.size = len(data) if kind == tarfile.REGTYPE else 0
        return info, data

    def Git(self, repo, *arguments):
        return subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=' + str(self.root / 'no-hooks'),
                               '-c', 'user.name=Source package test', '-c', 'user.email=test@example.invalid',
                               *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout

    def Repo(self):
        repo = self.root / 'repo'
        repo.mkdir()
        self.Git(repo, 'init')
        for name, data in self.files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.Git(repo, 'add', '--', '.')
        self.Git(repo, 'update-index', '--chmod=+x', 'scripts/run.sh')
        self.Git(repo, 'commit', '-m', 'fixture')
        self.commit = self.Git(repo, 'rev-parse', 'HEAD').decode().strip()
        return repo

    def Symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            self.skipTest(f'Symlink creation unavailable: {error}')

    def test_roundtrip_idempotence_preserves_build_outputs(self):
        archive, digest = self.Package()
        result = self.Stage(archive, digest)
        self.assertTrue(result['changed'])
        release = Path(result['directory'])
        self.assertEqual(result['source_dir'], str(release / 'source'))
        self.assertEqual(result['package_sha256'], digest)
        self.assertEqual(json.loads((release / 'source-manifest.json').read_bytes()), self.Manifest())
        self.assertEqual((release / 'source-package.sha256').read_bytes(), (digest + '\n').encode())
        (release / 'build').mkdir()
        (release / 'build/object.o').write_bytes(b'keep build output')
        self.assertFalse(self.Stage(archive, digest)['changed'])
        self.assertEqual((release / 'build/object.o').read_bytes(), b'keep build output')
        for name, data in self.files.items():
            self.assertEqual((release / 'source' / name).read_bytes(), data)

    def test_package_checksum_and_commit_fail_before_creating_release_parent(self):
        archive, digest = self.Package()
        for sha, commit in [('0' * 64, self.commit), ('BAD', self.commit), (digest, 'b' * 40)]:
            with self.subTest(sha=sha[:4], commit=commit[:4]), self.assertRaises(ValueError):
                source.Stage(archive, sha, self.root / 'new-parent/r1', commit)
            self.assertFalse((self.root / 'new-parent').exists())

    def test_unsafe_member_paths_rejected(self):
        names = ['../escaped', '/absolute', 'source/../escaped', 'source//double', './source/dot',
                 'source/./dot', 'C:/outside', 'source\\escape', 'source/a:stream', 'source/aux.txt',
                 'source/trailing.', 'source/trailing ', 'source/line\nfeed']
        for name in names:
            with self.subTest(name=name):
                archive, digest = self.Package(extra=[self.Member(name, b'x')])
                with self.assertRaises(ValueError):
                    self.Stage(archive, digest)
                self.assertFalse((self.root / 'releases').exists())

    def test_links_special_files_duplicate_and_unexpected_directories_rejected(self):
        extra = [self.Member('source/link', kind=kind, linkname='main.cpp')
                 for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE)]
        extra += [self.Member('source/special', kind=kind) for kind in (tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE)]
        extra += [self.Member('source/main.cpp', self.files['main.cpp']),
                  self.Member('SOURCE/MAIN.CPP', self.files['main.cpp']),
                  self.Member('unlisted', b'x'), self.Member('source/empty', kind=tarfile.DIRTYPE),
                  self.Member('source', kind=tarfile.DIRTYPE, mode=0o777)]
        for member in extra:
            with self.subTest(name=member[0].name, type=member[0].type):
                archive, digest = self.Package(extra=[member])
                with self.assertRaises(ValueError):
                    self.Stage(archive, digest)
                self.assertFalse((self.root / 'releases').exists())

    def test_manifest_hash_size_mode_and_member_set_checked_before_extraction(self):
        for field, value in [('sha256', '0' * 64), ('bytes', 1000), ('bytes', True),
                             ('mode', '0777'), ('path', '../escape'), ('path', 'data/private.cpp')]:
            with self.subTest(field=field, value=value):
                manifest = self.Manifest()
                manifest['files'][-1][field] = value
                archive, digest = self.Package(manifest=manifest)
                with self.assertRaises(ValueError):
                    self.Stage(archive, digest)
                self.assertFalse((self.root / 'releases').exists())
        archive, digest = self.Package(omit=['scripts/run.sh'])
        with self.assertRaises(ValueError):
            self.Stage(archive, digest)
        self.assertFalse((self.root / 'releases').exists())

    def test_source_file_hash_is_checked_even_if_tar_header_is_valid(self):
        archive, digest = self.Package(contents={**self.files, 'scripts/run.sh': b'x' * len(self.files['scripts/run.sh'])})
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.Stage(archive, digest)
        self.assertFalse((self.root / 'releases').exists())

    def test_invalid_manifest_shape_provenance_and_duplicates_rejected(self):
        manifests = [[], {}, {**self.Manifest(), 'schema': True}, {**self.Manifest(), 'dirty_worktree': False},
                     {**self.Manifest(), 'source_commit_kind': 'exact'}, {**self.Manifest(), 'files': []},
                     {**self.Manifest(), 'files': [self.Manifest()['files'][0]] * 2},
                     b'{"schema": 1, "schema": 1}']
        for manifest in manifests:
            with self.subTest(manifest=manifest):
                archive, digest = self.Package(manifest=manifest)
                with self.assertRaises(ValueError):
                    self.Stage(archive, digest)

    def test_archive_budgets_reject_before_extraction(self):
        archive, digest = self.Package()
        for limit, value in [('MAX_FILE_BYTES', 1), ('MAX_SOURCE_BYTES', 1), ('MAX_MANIFEST_BYTES', 1), ('MAX_FILES', 1)]:
            with self.subTest(limit=limit), patch.object(source, limit, value), self.assertRaises(ValueError):
                self.Stage(archive, digest)
        self.assertFalse((self.root / 'releases').exists())

    def test_case_colliding_parent_directories_rejected(self):
        self.files = {'Src/a.cpp': b'a', 'src/b.cpp': b'b'}
        archive, digest = self.Package()
        with self.assertRaisesRegex(ValueError, 'Case-colliding'):
            self.Stage(archive, digest)
        self.assertFalse((self.root / 'releases').exists())

    def test_atomic_publication_does_not_replace_racing_empty_directory(self):
        archive, digest = self.Package()
        destination = self.root / 'releases/r1'
        original = source.PublishDirectory
        def Race(temporary, target):
            target.mkdir()
            original(temporary, target)
        with patch.object(source, 'PublishDirectory', side_effect=Race), self.assertRaises(FileExistsError):
            self.Stage(archive, digest, destination)
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_original_source_modification_never_overwritten(self):
        archive, digest = self.Package()
        release = Path(self.Stage(archive, digest)['directory'])
        modified = release / 'source/main.cpp'
        modified.write_bytes(b'user edited source')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.Stage(archive, digest)
        self.assertEqual(modified.read_bytes(), b'user edited source')

    def test_existing_source_extra_missing_directory_and_proof_tamper_rejected(self):
        archive, digest = self.Package()
        for mutation in ('extra-file', 'missing-file', 'extra-dir', 'manifest', 'sha'):
            with self.subTest(mutation=mutation):
                release = self.root / mutation
                self.Stage(archive, digest, release)
                if mutation == 'extra-file':
                    (release / 'source/user.cpp').write_bytes(b'keep')
                elif mutation == 'missing-file':
                    (release / 'source/main.cpp').unlink()
                elif mutation == 'extra-dir':
                    (release / 'source/user-dir').mkdir()
                elif mutation == 'manifest':
                    (release / 'source-manifest.json').write_bytes(b'{}')
                else:
                    (release / 'source-package.sha256').write_text('0' * 64)
                with self.assertRaises(ValueError):
                    self.Stage(archive, digest, release)

    @unittest.skipIf(os.name == 'nt', 'Windows does not retain POSIX executable permissions')
    def test_existing_source_changed_mode_rejected(self):
        archive, digest = self.Package()
        release = Path(self.Stage(archive, digest)['directory'])
        os.chmod(release / 'source/main.cpp', 0o755)
        with self.assertRaisesRegex(ValueError, 'mode'):
            self.Stage(archive, digest)

    @unittest.skipIf(os.name == 'nt', 'Windows does not retain POSIX directory permissions')
    def test_release_directory_is_traversable_and_existing_permissions_are_not_changed(self):
        archive, digest = self.Package()
        release = Path(self.Stage(archive, digest)['directory'])
        self.assertEqual(release.stat().st_mode & 0o777, 0o755)
        os.chmod(release, 0o700)
        with self.assertRaisesRegex(ValueError, 'directory must have mode 0755'):
            self.Stage(archive, digest)
        self.assertEqual(release.stat().st_mode & 0o777, 0o700)

    def test_existing_unrelated_directory_or_file_preserved(self):
        archive, digest = self.Package()
        for directory in (True, False):
            destination = self.root / ('old-directory' if directory else 'old-file')
            sentinel = destination / 'important' if directory else destination
            if directory:
                destination.mkdir()
            sentinel.write_text('keep old data')
            with self.assertRaises((ValueError, OSError)):
                self.Stage(archive, digest, destination)
            self.assertEqual(sentinel.read_text(), 'keep old data')

    def test_parent_symlink_and_existing_source_symlink_rejected(self):
        archive, digest = self.Package()
        outside = self.root / 'outside'
        outside.mkdir()
        link = self.root / 'linked-parent'
        self.Symlink(link, outside, directory=True)
        with self.assertRaisesRegex(ValueError, 'link'):
            self.Stage(archive, digest, link / 'release')
        self.assertEqual(list(outside.iterdir()), [])
        release = Path(self.Stage(archive, digest)['directory'])
        victim = release / 'source/main.cpp'
        victim.unlink()
        external_file = outside / 'external.cpp'
        external_file.write_text('never edit')
        self.Symlink(victim, external_file)
        with self.assertRaisesRegex(ValueError, 'link'):
            self.Stage(archive, digest)
        self.assertEqual(external_file.read_text(), 'never edit')

    def test_windows_reparse_attribute_rejected(self):
        info = (self.root / '.').lstat()
        class Reparse:
            st_mode = info.st_mode
            st_file_attributes = 0x400
        with patch.object(Path, 'lstat', return_value=Reparse()), self.assertRaisesRegex(ValueError, 'reparse'):
            source.NoLinks(self.root)

    def test_stage_requires_absolute_nonroot_destination(self):
        archive, digest = self.Package()
        for path in ('relative/release', Path(self.root.anchor), self.root / '..' / 'escape'):
            with self.subTest(path=str(path)), self.assertRaises(ValueError):
                self.Stage(archive, digest, path)

    def test_failed_write_cleans_only_own_temporary_directory(self):
        archive, digest = self.Package()
        sibling = self.root / 'releases/other-release'
        sibling.mkdir(parents=True)
        sentinel = sibling / 'keep'
        sentinel.write_text('untouched')
        with patch.object(source.shutil, 'copyfileobj', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.Stage(archive, digest)
        self.assertEqual(sentinel.read_text(), 'untouched')
        self.assertEqual(list((self.root / 'releases').iterdir()), [sibling])

    def test_pack_uses_tracked_worktree_bytes_and_explicit_new_files(self):
        repo = self.Repo()
        (repo / 'main.cpp').write_bytes(b'// modified tracked working tree\n')
        (repo / 'new.cpp').write_bytes(b'// explicit new source\n')
        (repo / 'unlisted.cpp').write_bytes(b'// must not be packaged\n')
        output = self.root / 'snapshot.tar.gz'
        result = source.Pack(repo, output, self.commit, ['new.cpp'])
        self.assertEqual(result['files'], 3)
        release = Path(self.Stage(output, result['package_sha256'])['directory'])
        manifest = json.loads((release / 'source-manifest.json').read_text())
        self.assertEqual({entry['path'] for entry in manifest['files']}, {'main.cpp', 'scripts/run.sh', 'new.cpp'})
        self.assertEqual(manifest['source_commit_kind'], 'base')
        self.assertTrue(manifest['dirty_worktree'])
        self.assertEqual((release / 'source/main.cpp').read_bytes(), b'// modified tracked working tree\n')
        self.assertEqual(next(entry['mode'] for entry in manifest['files'] if entry['path'] == 'scripts/run.sh'), '0755')

    def test_pack_filters_tracked_nonsource_and_rejects_explicit_include(self):
        repo = self.Repo()
        forbidden = ['build/cache.txt', 'data/state.json', 'reports/run/data/result.json', '.venv/config',
                     '.env', 'credentials.json', 'node.secret', 'id_ed25519', 'wallet.key', 'node.exe',
                     'ansible/inventory.local.yml', 'Experiments/cluster.local.json']
        for name in forbidden:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'placeholder')
        self.Git(repo, 'add', '--', *forbidden)
        output = self.root / 'filtered.tar.gz'
        result = source.Pack(repo, output, self.commit)
        self.assertEqual(set(result['excluded_tracked']), set(forbidden))
        self.assertEqual(result['files'], len(self.files))
        for index, name in enumerate(forbidden + ['.git/config', '../escaped', 'main.cpp/../bad', '/absolute']):
            with self.subTest(name=name), self.assertRaises(ValueError):
                source.Pack(repo, self.root / f'refused-{index}.tar.gz', self.commit, [name])

    def test_source_db_headers_and_build_script_are_packaged_but_runtime_data_is_not(self):
        kept = {'DB/DB.h': b'// database implementation\n',
                'DB/State/Cache.hpp': b'// database source header\n',
                'scripts/Build-Linux.sh': b'#!/bin/sh\nexit 0\n'}
        self.files.update(kept)
        repo = self.Repo()
        forbidden = ['DB/CURRENT', 'DB/LOCK', 'DB/IDENTITY', 'DB/LOG', 'DB/LOG.old.123',
                     'DB/MANIFEST-000001', 'DB/OPTIONS-000002', 'DB/000003.sst', 'DB/000004.blob', 'DB/000005.wal',
                     'DB/data/CURRENT', 'runtime/db/CURRENT', 'other-runtime/DB/MANIFEST-000001',
                     'build-debug/cache.txt', 'scripts/build-output/cache.txt', 'runtime/node.log']
        for name in forbidden:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'runtime fixture')
        self.Git(repo, 'add', '--', *forbidden)
        output = self.root / 'source-db.tar.gz'
        result = source.Pack(repo, output, self.commit)
        self.assertEqual(set(result['excluded_tracked']), set(forbidden))
        release = Path(self.Stage(output, result['package_sha256'])['directory'])
        manifest = json.loads((release / 'source-manifest.json').read_text())
        self.assertEqual({row['path'] for row in manifest['files']}, set(self.files))
        for name, content in kept.items():
            self.assertEqual((release / 'source' / name).read_bytes(), content)

    def test_real_repository_database_headers_and_linux_build_script_are_eligible(self):
        repo = Path(__file__).resolve().parents[2]
        headers = [path for path in (repo / 'DB').rglob('*')
                   if path.is_file() and path.suffix.lower() in ('.h', '.hh', '.hpp', '.hxx')]
        self.assertTrue(headers, 'The repository DB source headers must exist')
        for path in [*headers, repo / 'scripts' / 'Build-Linux.sh']:
            with self.subTest(path=path):
                self.assertTrue(path.is_file())
                self.assertFalse(source.Excluded(path.relative_to(repo).as_posix()))

    def test_pack_missing_tracked_file_even_excluded_is_error(self):
        repo = self.Repo()
        (repo / 'data').mkdir()
        path = repo / 'data/state.json'
        path.write_bytes(b'placeholder')
        self.Git(repo, 'add', '--', 'data/state.json')
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            source.Pack(repo, self.root / 'missing.tar.gz', self.commit)
        self.assertFalse((self.root / 'missing.tar.gz').exists())

    def test_pack_refuses_existing_output_bad_commit_and_nonroot_source(self):
        repo = self.Repo()
        output = self.root / 'existing.tar.gz'
        output.write_bytes(b'keep existing package')
        with self.assertRaises(FileExistsError):
            source.Pack(repo, output, self.commit)
        self.assertEqual(output.read_bytes(), b'keep existing package')
        for location, commit in [(repo, 'b' * 40), (repo, 'HEAD'), (repo / 'scripts', self.commit)]:
            with self.subTest(location=str(location), commit=commit), self.assertRaises(ValueError):
                source.Pack(location, self.root / 'bad.tar.gz', commit)
        with self.assertRaises(ValueError):
            source.Pack(repo, 'relative.tar.gz', self.commit)

    def test_pack_rejects_symlinks_and_git_symlink_mode(self):
        repo = self.Repo()
        # Reject a Git symlink even on Windows with core.symlinks=false.
        blob = self.Git(repo, 'rev-parse', 'HEAD:main.cpp').decode().strip()
        self.Git(repo, 'update-index', '--cacheinfo', '120000,' + blob + ',main.cpp')
        with self.assertRaisesRegex(ValueError, 'linked'):
            source.Pack(repo, self.root / 'bad.tar.gz', self.commit)

    def test_pack_rejects_explicit_source_symlink(self):
        repo = self.Repo()
        self.Symlink(repo / 'new.cpp', self.root / 'outside')
        with self.assertRaisesRegex(ValueError, 'link'):
            source.Pack(repo, self.root / 'bad.tar.gz', self.commit, ['new.cpp'])

    def test_frontend_assets_require_explicit_flag_and_are_verified(self):
        repo = self.Repo()
        frontend = repo / 'Classroom/dist'
        (frontend / 'assets').mkdir(parents=True)
        (frontend / 'index.html').write_bytes(b'<html>built frontend</html>')
        (frontend / 'assets/index-123.js').write_bytes(b'console.log("app");')
        (repo / 'new.cpp').write_bytes(b'// explicit new source\n')
        result = source.Pack(repo, self.root / 'default.tar.gz', self.commit)
        self.assertEqual(result['files'], len(self.files))
        with self.assertRaisesRegex(ValueError, 'source path'):
            source.Pack(repo, self.root / 'refused.tar.gz', self.commit, ['Classroom/dist/index.html'])
        output = self.root / 'frontend.tar.gz'
        result = source.Pack(repo, output, self.commit, ['new.cpp', 'new.cpp'], include_frontend=True)
        release = Path(self.Stage(output, result['package_sha256'])['directory'])
        manifest = json.loads((release / 'source-manifest.json').read_bytes())
        self.assertTrue(manifest['frontend_included'])
        self.assertEqual(result['files'], len(self.files) + 3)
        self.assertEqual((release / 'source/Classroom/dist/index.html').read_bytes(), b'<html>built frontend</html>')
        self.assertFalse(self.Stage(output, result['package_sha256'])['changed'])
        (release / 'source/Classroom/dist/assets/index-123.js').write_bytes(b'edited app')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.Stage(output, result['package_sha256'])

    def test_frontend_missing_index_unsafe_asset_and_other_dist_rejected(self):
        repo = self.Repo()
        with self.assertRaisesRegex(ValueError, 'index.html'):
            source.Pack(repo, self.root / 'missing-frontend.tar.gz', self.commit, include_frontend=True)
        frontend = repo / 'Classroom/dist'
        frontend.mkdir(parents=True)
        (frontend / 'index.html').write_bytes(b'<html/>')
        (frontend / 'credentials.json').write_bytes(b'placeholder')
        with self.assertRaisesRegex(ValueError, 'Unsafe frontend'):
            source.Pack(repo, self.root / 'unsafe-frontend.tar.gz', self.commit, include_frontend=True)
        (frontend / 'credentials.json').unlink()
        (repo / 'dist').mkdir()
        (repo / 'dist/index.html').write_bytes(b'other build output')
        with self.assertRaisesRegex(ValueError, 'source path'):
            source.Pack(repo, self.root / 'other-dist.tar.gz', self.commit, ['dist/index.html'], include_frontend=True)

    def test_frontend_link_is_rejected(self):
        repo = self.Repo()
        frontend = repo / 'Classroom/dist'
        frontend.mkdir(parents=True)
        (frontend / 'index.html').write_bytes(b'<html/>')
        self.Symlink(frontend / 'linked.js', self.root / 'external.js')
        with self.assertRaisesRegex(ValueError, 'link'):
            source.Pack(repo, self.root / 'linked-frontend.tar.gz', self.commit, include_frontend=True)

    def test_frontend_manifest_requires_flag_and_index(self):
        self.files['Classroom/dist/index.html'] = b'<html/>'
        archive, digest = self.Package()
        with self.assertRaisesRegex(ValueError, 'Excluded'):
            self.Stage(archive, digest)
        manifest = {**self.Manifest(), 'frontend_included': True}
        archive, digest = self.Package(manifest=manifest)
        self.assertTrue(self.Stage(archive, digest)['changed'])
        manifest['files'] = [entry for entry in manifest['files'] if entry['path'] != 'Classroom/dist/index.html']
        archive, digest = self.Package(manifest=manifest, omit=['Classroom/dist/index.html'])
        with self.assertRaisesRegex(ValueError, 'index.html'):
            self.Stage(archive, digest, self.root / 'other-release')

    def test_cli_pack_and_stage_emit_json_and_report_failures(self):
        repo = self.Repo()
        output = self.root / 'cli.tar.gz'
        common = [sys.executable, '-B', str(SCRIPT)]
        packed = subprocess.run(common + ['pack', '--source', str(repo), '--output', str(output),
                                          '--source-commit', self.commit], capture_output=True, text=True, check=True)
        digest = json.loads(packed.stdout)['package_sha256']
        args = common + ['stage', '--package', str(output), '--sha256', digest,
                         '--release-dir', str(self.root / 'cli-release'), '--source-commit', self.commit]
        self.assertTrue(json.loads(subprocess.run(args, capture_output=True, text=True, check=True).stdout)['changed'])
        self.assertFalse(json.loads(subprocess.run(args, capture_output=True, text=True, check=True).stdout)['changed'])
        invalid = subprocess.run(args[:-1] + ['0' * 40], capture_output=True, text=True)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(invalid.stdout, '')
        self.assertIn('vdf_source:', invalid.stderr)


if __name__ == '__main__':
    unittest.main()
