import contextlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('fleet_prepare', HERE / 'prepare.py')
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class PrepareTests(unittest.TestCase):
    def test_driver_reuses_repository_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            driver = Path(temporary) / 'driver'
            prepare.CopyDriver(driver)
            self.assertEqual((driver / 'pom.xml').read_bytes(), (HERE.parent / 'pom.xml').read_bytes())
            for name in ('DagTransfer.java', 'ParallelOk.java'):
                self.assertEqual((driver / 'src/org/fisco/bcos/sdk/demo/contract' / name).read_bytes(),
                                 (HERE.parent / 'vendor' / name).read_bytes())
            source = (driver / 'NativeAlignedBench.java').read_text()
            self.assertIn('FISCO_BENCH_SAMPLE_HZ', source)
            self.assertEqual(source.count('Math.max(1,rate/SAMPLE_HZ)'), 3)

    def test_driver_only_download_does_not_touch_node_or_builder(self):
        requested = []
        def download(url, destination):
            requested.append(destination.name)
            destination.write_bytes(b'cached download')
            return {'path': str(destination)}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(prepare, 'Download', side_effect=download), \
                patch.object(prepare, 'Hash', return_value=prepare.MAVEN_SHA512), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(prepare.Main(['--root', temporary, '--driver-only', '--download-only']), 0)
        self.assertCountEqual(requested, ['jdk17-linux-x64.tar.gz', 'apache-maven-3.9.9.tar.gz'])

    def test_download_reuses_complete_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'cached.tar.gz'
            path.write_bytes(b'already downloaded')
            with patch.object(prepare.urllib.request, 'urlopen') as open_url:
                result = prepare.Download('https://example.invalid/tool', path)
            open_url.assert_not_called()
            self.assertEqual(result['sha256'], prepare.Hash(path))

    def test_extract_rejects_parent_directory_and_escaping_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, link in [('../escape', None), ('tool/bin/java', '/outside')]:
                archive = root / 'tool.tar.gz'
                with tarfile.open(archive, 'w:gz') as output:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = link
                    else:
                        member.size = 1
                    output.addfile(member, None if link else io.BytesIO(b'x'))
                with self.assertRaises(ValueError):
                    prepare.Extract(archive, root / 'extracted')
            self.assertFalse((root.parent / 'escape').exists())


if __name__ == '__main__':
    unittest.main()
