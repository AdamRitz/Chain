"""Download the official FISCO v3.7.3 Linux archive using validated HTTP ranges."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import time
import urllib.request

URL = 'https://github.com/FISCO-BCOS/FISCO-BCOS/releases/download/v3.7.3/fisco-bcos-linux-x86_64.tar.gz'
SIZE = 30233449
NODE_SHA256 = '7c29bd2afe615a57af02a440fc4d665c3beec3cf7301ec4007cc07caf5b683f4'


def Digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def Request(url, begin, end):
    return urllib.request.Request(url, headers={
        'Range': 'bytes=' + str(begin) + '-' + str(end),
        'Accept-Encoding': 'identity', 'User-Agent': 'FISCO100-parallel-download',
        'Cache-Control': 'no-cache'})


def CheckResponse(response, begin, end, size=None, etag=None):
    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
    if (response.status != 206 or not match or int(match[1]) != begin or int(match[2]) != end or
            (size is not None and int(match[3]) != size) or
            int(response.headers.get('Content-Length', '-1')) != end - begin + 1 or
            (etag and response.headers.get('ETag') != etag)):
        raise ValueError('The HTTP response does not match the requested range')
    return int(match[3])


def VerifyNode(path, expected=NODE_SHA256):
    with tarfile.open(path, 'r:gz') as archive:
        members = [member for member in archive.getmembers() if Path(member.name).name == 'fisco-bcos']
        if len(members) != 1 or not members[0].isfile() or not 0 < members[0].size <= 512 * 1024**2:
            raise ValueError('The archive must contain one ordinary fisco-bcos binary')
        digest = hashlib.sha256()
        with archive.extractfile(members[0]) as binary:
            header = binary.read(20)
            if (len(header) != 20 or header[:6] != b'\x7fELF\x02\x01' or
                    int.from_bytes(header[18:20], 'little') != 62):
                raise ValueError('Expected the ELF64 x86-64 FISCO node')
            digest.update(header)
            for block in iter(lambda: binary.read(1024 * 1024), b''):
                digest.update(block)
        actual = digest.hexdigest()
        if actual != expected:
            raise ValueError('The node differs from the verified official FISCO v3.7.3 SHA256')
        return actual


def Chunk(url, original_url, directory, index, begin, end, size, etag):
    target = directory / (str(index).zfill(3) + '.chunk')
    metadata = target.with_suffix('.json')
    expected = {'url': original_url, 'begin': begin, 'end': end, 'size': size, 'etag': etag}
    if target.is_file() and metadata.is_file():
        saved = json.loads(metadata.read_text())
        if (all(saved.get(key) == value for key, value in expected.items()) and
                target.stat().st_size == end - begin + 1 and Digest(target) == saved.get('sha256')):
            return target
    pending = target.with_suffix('.part')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(Request(url, begin, end), timeout=30) as response:
                CheckResponse(response, begin, end, size, etag)
                count = 0
                with pending.open('wb') as output:
                    while True:
                        block = response.read(256 * 1024)
                        if not block:
                            break
                        count += len(block)
                        if count > end - begin + 1:
                            raise ValueError('The range response exceeded its declared length')
                        output.write(block)
            if count != end - begin + 1:
                raise ValueError('The range response ended before its declared length')
            pending.replace(target)
            metadata.write_text(json.dumps({**expected, 'sha256': Digest(target)}))
            print(json.dumps({'chunk': index, 'bytes': count}), flush=True)
            return target
        except Exception:
            if attempt == 2:
                raise


def Main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=URL)
    parser.add_argument('--output', type=Path,
                        default=Path('/opt/fisco-fleet/downloads/fisco-bcos-linux-x86_64.tar.gz'))
    parser.add_argument('--threads', type=int, default=32)
    parser.add_argument('--size', type=int, default=SIZE)
    parser.add_argument('--sha256', help='Optional expected SHA256 of the compressed archive')
    parser.add_argument('--node-sha256', default=NODE_SHA256)
    args = parser.parse_args(argv)
    if not 1 <= args.threads <= 128 or args.size <= 0:
        parser.error('Use 1..128 workers and a positive archive size')
    output = args.output.absolute()
    if output == Path(output.anchor):
        parser.error('output must name a file below a filesystem root')
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with urllib.request.urlopen(Request(args.url, 0, 0), timeout=30) as response:
        CheckResponse(response, 0, 0, args.size)
        if len(response.read(2)) != 1:
            raise ValueError('Invalid range probe length')
        resolved, etag = response.geturl(), response.headers.get('ETag')
    cache = output.with_name(output.name + '.chunks')
    cache.mkdir(exist_ok=True)
    width = (args.size + args.threads - 1) // args.threads
    parts = [(index, begin, min(args.size - 1, begin + width - 1))
             for index, begin in enumerate(range(0, args.size, width))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as pool:
        futures = [pool.submit(Chunk, resolved, args.url, cache, index, begin, end, args.size, etag)
                   for index, begin, end in parts]
        chunks = [future.result() for future in futures]
    descriptor, name = tempfile.mkstemp(prefix='.fisco-range-', suffix='.pending', dir=output.parent)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as destination:
            for chunk in chunks:
                with chunk.open('rb') as source:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        if pending.stat().st_size != args.size:
            raise ValueError('Concatenated archive length differs')
        archive_sha256 = Digest(pending)
        if args.sha256 and archive_sha256 != args.sha256:
            raise ValueError('Compressed archive SHA256 differs')
        node_sha256 = VerifyNode(pending, args.node_sha256)
        os.replace(pending, output)
        result = {'status': 'complete', 'url': args.url, 'output': str(output), 'bytes': args.size,
                  'sha256': archive_sha256, 'node_sha256': node_sha256, 'threads': args.threads,
                  'seconds': time.monotonic() - started, 'chunks': len(chunks)}
        output.with_name(output.name + '.verified.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
        return 0
    finally:
        if pending.exists():
            if pending.parent.resolve() != output.parent.resolve() or not pending.name.startswith('.fisco-range-'):
                raise ValueError('Unexpected temporary archive path')
            pending.unlink()


if __name__ == '__main__':
    raise SystemExit(Main())
