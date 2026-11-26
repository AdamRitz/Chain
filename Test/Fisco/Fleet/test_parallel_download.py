import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import re
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('fleet_ranges', Path(__file__).with_name('parallel_download.py'))
ranges = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ranges)


class Response(io.BytesIO):
    def __init__(self, data, begin, end, size, valid=True):
        super().__init__(data)
        self.status = 206
        self.headers = {'Content-Range': 'bytes %d-%d/%d' % (begin, end, size),
                        'Content-Length': str(len(data)), 'ETag': 'fixture-v1'}
        if not valid:
            self.headers['Content-Range'] = 'bytes 0-0/1'

    def geturl(self):
        return 'https://example.invalid/resolved-release'


class RangeTests(unittest.TestCase):
    def fixture(self):
        node = bytearray(b'\x7fELF\x02\x01' + b'\0' * 200)
        node[18:20] = (62).to_bytes(2, 'little')
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            member = tarfile.TarInfo('fisco-bcos')
            member.size = len(node)
            archive.addfile(member, io.BytesIO(node))
        return output.getvalue(), hashlib.sha256(node).hexdigest()

    def test_ranges_cache_and_corrupt_chunk_repair(self):
        data, node_sha = self.fixture()
        requested = []
        def fetch(request, **kwargs):
            begin, end = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', request.get_header('Range')).groups())
            requested.append((begin, end))
            return Response(data[begin:end+1], begin, end, len(data))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'node.tar.gz'
            argv = ['--output', str(output), '--size', str(len(data)), '--threads', '32', '--node-sha256', node_sha]
            with patch.object(ranges.urllib.request, 'urlopen', side_effect=fetch), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ranges.Main(argv), 0)
                self.assertEqual(output.read_bytes(), data)
                requested.clear()
                ranges.Main(argv)
                self.assertEqual(requested, [(0, 0)])
                next(output.with_name(output.name+'.chunks').glob('*.chunk')).write_bytes(b'corrupt')
                requested.clear()
                ranges.Main(argv)
                self.assertEqual(len(requested), 2)
                self.assertEqual(output.read_bytes(), data)

    def test_bad_http_range_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'node.tar.gz'
            output.write_bytes(b'previous valid package')
            with patch.object(ranges.urllib.request, 'urlopen', return_value=Response(b'x', 0, 0, 20, False)):
                with self.assertRaises(ValueError):
                    ranges.Main(['--output', str(output), '--size', '20'])
            self.assertEqual(output.read_bytes(), b'previous valid package')

    def test_incorrect_node_hash_preserves_existing_file(self):
        data, _ = self.fixture()
        def fetch(request, **kwargs):
            begin, end = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', request.get_header('Range')).groups())
            return Response(data[begin:end+1], begin, end, len(data))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'node.tar.gz'
            output.write_bytes(b'previous valid package')
            with patch.object(ranges.urllib.request, 'urlopen', side_effect=fetch), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    ranges.Main(['--output', str(output), '--size', str(len(data)), '--node-sha256', '0'*64])
            self.assertEqual(output.read_bytes(), b'previous valid package')
            self.assertFalse(list(output.parent.glob('.fisco-range-*.pending')))


if __name__ == '__main__':
    unittest.main()
