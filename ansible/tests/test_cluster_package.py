"""Runtime dependency packaging checks, independent of Linux tool availability."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cluster_release as package
from vdf_build import Hash, Sources


class RuntimePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.release = self.root / 'release'
        (self.release / 'source').mkdir(parents=True)
        (self.release / 'source/main.cpp').write_bytes(b'// tested source')
        (self.release / 'build').mkdir()
        self.binaries = ['boost', 'sender', 'evm_sender', 'chain_classroom']
        for name in self.binaries:
            (self.release / 'build' / name).write_bytes(b'\x7fELF' + name.encode())
        source = {'source_commit': 'a' * 40, 'files': [
            {'path': 'main.cpp', 'sha256': Hash(self.release / 'source/main.cpp'), 'mode': '0644'}]}
        (self.release / 'source-manifest.json').write_text(json.dumps(source), encoding='utf-8')
        proof = {'ok': True, 'complete': True, 'source_manifest_sha256': Sources(self.release),
                 'binary_sha256': Hash(self.release / 'build/boost'), 'settings': {'classroom': True}}
        (self.release / 'build-proof.json').write_text(json.dumps(proof), encoding='utf-8')
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        for name in ('libexample.so.1', 'libsupport.so.2'):
            (self.runtime / name).write_bytes(b'\x7fELF' + name.encode())
        self.output = self.root / 'package.tar.gz'
        self.header = '  Class: ELF64\n  Machine: Advanced Micro Devices X86-64\n'
        self.versions = 'Name: GLIBC_2.2.5\nName: GLIBC_2.34\n'
        self.sonames = {}
        self.needed = {name: ['libexample.so.1', 'libc.so.6'] for name in self.binaries}
        self.needed.update({'libexample.so.1': ['libsupport.so.2'], 'libsupport.so.2': ['libc.so.6']})
        self.ldd_returncode = 0
        self.ldd_extra = ''
        self.resolved = {name: self.runtime / name for name in ('libexample.so.1', 'libsupport.so.2')}

    def Readelf(self, command, **kwargs):
        self.assertEqual(command[0], 'readelf')
        self.assertEqual(kwargs['env']['LC_ALL'], 'C')
        name = Path(command[-1]).name
        if command[1] == '-h':
            return self.header
        if command[1] == '--version-info':
            return self.versions
        self.assertEqual(command[1], '-d')
        lines = [f' (NEEDED) Shared library: [{dependency}]' for dependency in self.needed[name]]
        if name.startswith('lib'):
            lines.append(f' (SONAME) Library soname: [{self.sonames.get(name, name)}]')
        return '\n'.join(lines)

    def Ldd(self, command, **kwargs):
        self.assertEqual(command[:2], ['ldd', '-r'])
        self.assertEqual(kwargs['env']['LD_LIBRARY_PATH'], str(self.runtime))
        self.assertNotIn('LD_PRELOAD', kwargs['env'])
        self.assertNotIn('LD_AUDIT', kwargs['env'])
        lines = [f' {name} => {path} (0x00001234)' for name, path in self.resolved.items()]
        lines += [' libc.so.6 => /lib64/libc.so.6 (0x00005678)',
                  ' /lib64/ld-linux-x86-64.so.2 (0x00007890)']
        return subprocess.CompletedProcess(command, self.ldd_returncode,
                                           '\n'.join(lines), self.ldd_extra)

    def Pack(self):
        with mock.patch.object(package.subprocess, 'check_output', side_effect=self.Readelf), \
             mock.patch.object(package.subprocess, 'run', side_effect=self.Ldd) as ldd, \
             mock.patch.dict(os.environ, {'LD_PRELOAD': 'inherited.so', 'LD_AUDIT': 'inherited-audit.so'}):
            result = package.Pack(self.release, self.output, runtime_dir=self.runtime)
        return result, ldd

    def test_all_binaries_and_transitive_runtime_dependencies_are_packaged(self):
        result, ldd = self.Pack()
        self.assertEqual([Path(call.args[0][-1]).name for call in ldd.call_args_list], self.binaries)
        self.assertEqual(result['runtime']['libraries'], ['libexample.so.1', 'libsupport.so.2'])
        with tarfile.open(self.output, 'r:gz') as archive:
            manifest = json.load(archive.extractfile('manifest.json'))
            libraries = {row['path']: row for row in manifest['files'] if row['path'].startswith('lib/')}
            self.assertEqual(set(libraries), {'lib/libexample.so.1', 'lib/libsupport.so.2'})
            for relative, row in libraries.items():
                self.assertEqual(row['sha256'], Hash(self.runtime / Path(relative).name))
                self.assertEqual(archive.getmember(relative).mode, 0o644)

    def test_missing_symbol_or_library_prevents_package_creation(self):
        for error in ('libmissing.so.1 => not found', 'undefined symbol: missing_function'):
            with self.subTest(error=error):
                self.ldd_extra = error
                with self.assertRaisesRegex(ValueError, 'Runtime link check failed'):
                    self.Pack()
                self.assertFalse(self.output.exists())

    def test_failed_ldd_prevents_package_creation(self):
        self.ldd_returncode = 1
        with self.assertRaisesRegex(ValueError, 'Runtime link check failed'):
            self.Pack()
        self.assertFalse(self.output.exists())

    def test_dependency_resolved_outside_runtime_directory_is_rejected(self):
        outside = self.root / 'libsupport.so.2'
        outside.write_bytes(b'\x7fELFoutside')
        self.resolved['libsupport.so.2'] = outside
        with self.assertRaisesRegex(ValueError, 'Dependency must resolve from runtime-dir'):
            self.Pack()
        self.assertFalse(self.output.exists())

    def test_missing_ordinary_soname_file_is_rejected(self):
        (self.runtime / 'libsupport.so.2').unlink()
        with self.assertRaisesRegex(ValueError, 'Dependency must resolve from runtime-dir'):
            self.Pack()
        self.assertFalse(self.output.exists())

    def test_symbolic_link_runtime_member_is_rejected(self):
        link = self.runtime / 'libexample.so.1'
        link.unlink()
        try:
            link.symlink_to(self.runtime / 'libsupport.so.2')
        except OSError as error:
            self.skipTest('Symbolic-link creation unavailable: ' + str(error))
        with self.assertRaisesRegex(ValueError, 'Symbolic-link paths'):
            self.Pack()
        self.assertFalse(self.output.exists())

    def test_library_soname_must_match_packaged_file_name(self):
        self.sonames['libsupport.so.2'] = 'libsupport.so.3'
        with self.assertRaisesRegex(ValueError, 'SONAME differs'):
            self.Pack()

    def test_elf_class_and_machine_must_match_nodes(self):
        for header in ('Class: ELF32\nMachine: Advanced Micro Devices X86-64\n',
                       'Class: ELF64\nMachine: AArch64\n'):
            with self.subTest(header=header):
                self.header = header
                with self.assertRaisesRegex(ValueError, 'ELF64 x86-64'):
                    self.Pack()

    def test_glibc_newer_than_234_and_new_abi_tags_are_rejected(self):
        for versions in ('Name: GLIBC_2.35\n', 'Name: GLIBC_ABI_DT_RELR\n'):
            with self.subTest(versions=versions):
                self.versions = versions
                with self.assertRaisesRegex(ValueError, 'GLIBC'):
                    self.Pack()

    def test_transitive_needed_entry_cannot_be_omitted_by_ldd(self):
        self.needed['libsupport.so.2'].append('libomitted.so.1')
        with self.assertRaisesRegex(ValueError, 'Unresolved runtime dependencies'):
            self.Pack()

    def test_absolute_needed_path_is_rejected(self):
        self.needed['boost'].append('/unverified/libabsolute.so.1')
        with self.assertRaisesRegex(ValueError, 'SONAME file names'):
            self.Pack()

    def test_manual_library_mode_keeps_existing_interface_without_linux_tools(self):
        with mock.patch.object(package.subprocess, 'run', side_effect=AssertionError('Unexpected ldd')), \
             mock.patch.object(package.subprocess, 'check_output', side_effect=AssertionError('Unexpected readelf')):
            result = package.Pack(self.release, self.output, [self.runtime / 'libexample.so.1'])
        self.assertNotIn('runtime', result)
        with tarfile.open(self.output, 'r:gz') as archive:
            self.assertIn('lib/libexample.so.1', archive.getnames())

    def test_incomplete_build_is_rejected_before_runtime_commands(self):
        proof = json.loads((self.release / 'build-proof.json').read_text(encoding='utf-8'))
        proof['complete'] = False
        (self.release / 'build-proof.json').write_text(json.dumps(proof), encoding='utf-8')
        with mock.patch.object(package.subprocess, 'run', side_effect=AssertionError('Unexpected ldd')):
            with self.assertRaisesRegex(ValueError, 'successful complete test proof'):
                package.Pack(self.release, self.output, runtime_dir=self.runtime)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
