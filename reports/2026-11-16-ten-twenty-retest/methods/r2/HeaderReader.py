#!/usr/bin/env python3
"""Export headers from one stopped, owned experiment DB through RocksDB's C API."""
import argparse
import ctypes as C
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import struct
import sys

COMMIT = '9d34b55f1d5bf8ebab1f3fe44a86df31e18b1899'
SOURCE_TEST = Path('/opt/chain/releases') / COMMIT / 'source/Test'
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def checked_path(path):
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    real = path.resolve(strict=True)
    forbidden = Path('/var/lib')
    if path == forbidden or forbidden in path.parents or real == forbidden or forbidden in real.parents:
        raise ValueError('Refusing /var/lib paths')
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('Refusing symbolic links in experiment paths')
    return real


def validate_record(worker, record, database):
    command = record.get('command')
    if not isinstance(command, list) or command.count('--data') != 1:
        raise ValueError('Missing unique recorded --data argument')
    index = command.index('--data')
    if index + 1 >= len(command) or checked_path(command[index + 1]) != database:
        raise ValueError('Recorded --data does not match the isolated DB')
    if worker.FindProcess(record) is not None:
        raise RuntimeError('Experiment process is still running')


class RocksReadOnly:
    def __init__(self, library, database):
        self.api = C.CDLL(str(library))
        p, size = C.c_void_p, C.c_size_t
        signatures = {
            'rocksdb_options_create': (p, []),
            'rocksdb_options_destroy': (None, [p]),
            'rocksdb_options_set_create_if_missing': (None, [p, C.c_ubyte]),
            'rocksdb_open_for_read_only': (p, [p, C.c_char_p, C.c_ubyte, C.POINTER(p)]),
            'rocksdb_close': (None, [p]),
            'rocksdb_readoptions_create': (p, []),
            'rocksdb_readoptions_destroy': (None, [p]),
            'rocksdb_create_iterator': (p, [p, p]),
            'rocksdb_iter_destroy': (None, [p]),
            'rocksdb_iter_seek': (None, [p, C.c_char_p, size]),
            'rocksdb_iter_valid': (C.c_ubyte, [p]),
            'rocksdb_iter_next': (None, [p]),
            'rocksdb_iter_key': (p, [p, C.POINTER(size)]),
            'rocksdb_iter_value': (p, [p, C.POINTER(size)]),
            'rocksdb_iter_get_error': (None, [p, C.POINTER(p)]),
            'rocksdb_get': (p, [p, p, C.c_char_p, size, C.POINTER(size), C.POINTER(p)]),
            'rocksdb_free': (None, [p]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(self.api, name)
            function.restype, function.argtypes = restype, argtypes
        self.options = self.readoptions = self.db = self.iterator = None
        try:
            self.options = self.api.rocksdb_options_create()
            self.api.rocksdb_options_set_create_if_missing(self.options, 0)
            error = C.c_void_p()
            # A stopped clean DB may retain WAL files; opening remains read-only.
            self.db = self.api.rocksdb_open_for_read_only(self.options, os.fsencode(database), 0, C.byref(error))
            self.check_error(error)
            if not self.db:
                raise RuntimeError('RocksDB read-only open returned null')
            self.readoptions = self.api.rocksdb_readoptions_create()
            self.iterator = self.api.rocksdb_create_iterator(self.db, self.readoptions)
            if not self.iterator:
                raise RuntimeError('RocksDB iterator creation failed')
        except BaseException:
            self.close()
            raise

    def check_error(self, error):
        if error.value:
            try:
                message = C.string_at(error.value).decode('utf-8', errors='replace')
            finally:
                self.api.rocksdb_free(error.value)
            raise RuntimeError('RocksDB: ' + message)

    def get(self, key):
        length, error = C.c_size_t(), C.c_void_p()
        value = self.api.rocksdb_get(self.db, self.readoptions, key, len(key), C.byref(length), C.byref(error))
        try:
            self.check_error(error)
            return C.string_at(value, length.value) if value else None
        finally:
            if value:
                self.api.rocksdb_free(value)

    def headers(self):
        prefix = b'header/'
        self.api.rocksdb_iter_seek(self.iterator, prefix, len(prefix))
        while self.api.rocksdb_iter_valid(self.iterator):
            length = C.c_size_t()
            pointer = self.api.rocksdb_iter_key(self.iterator, C.byref(length))
            key = C.string_at(pointer, length.value)
            if not key.startswith(prefix):
                break
            pointer = self.api.rocksdb_iter_value(self.iterator, C.byref(length))
            value = C.string_at(pointer, length.value)
            yield key, value
            self.api.rocksdb_iter_next(self.iterator)
        error = C.c_void_p()
        self.api.rocksdb_iter_get_error(self.iterator, C.byref(error))
        self.check_error(error)

    def close(self):
        for attribute, destroy in [('iterator', 'rocksdb_iter_destroy'), ('readoptions', 'rocksdb_readoptions_destroy'),
                                   ('db', 'rocksdb_close'), ('options', 'rocksdb_options_destroy')]:
            value = getattr(self, attribute, None)
            if value:
                getattr(self.api, destroy)(value)
                setattr(self, attribute, None)


def decimal(value):
    if value is None or not re.fullmatch(rb'[0-9]{1,20}', value):
        raise ValueError('Missing or malformed decimal score/metadata')
    result = int(value)
    if result > (1 << 64) - 1:
        raise ValueError('Decimal score/metadata exceeds uint64')
    return result


def decode_header(key, value, score):
    if len(key) != 39 or not key.startswith(b'header/') or len(value) != 112:
        raise ValueError('Invalid header key/value length')
    own_hash = value[80:112]
    if key[7:] != own_hash or hashlib.blake2b(value[:80], digest_size=32).digest() != own_hash:
        raise ValueError('Header key/hash/content mismatch')
    height, txnum = struct.unpack_from('<QQ', value, 64)
    if height < 1:
        raise ValueError('Header height is zero')
    return {'hash': own_hash.hex(), 'parent': value[:32].hex(), 'height': height,
            'txnum': txnum, 'score': decimal(score)}


def Export(request, worker=None):
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Linux experiment hosts only')
    if worker is None:
        sys.path.insert(0, str(SOURCE_TEST))
        worker = importlib.import_module('ClusterWorker')
    root = checked_path(request['root'])
    if root == Path('/'):
        raise ValueError('An isolated experiment root is required')
    directory = checked_path(worker.CaseDirectory(request))
    if directory != root / request['run'] / request['node']:
        raise ValueError('Experiment path does not match root/run/node')
    database = checked_path(directory / 'db')
    if database.parent != directory or not database.is_dir():
        raise ValueError('Isolated experiment DB must exist')
    record_path = checked_path(directory / 'process.json')
    record_bytes = record_path.read_bytes()
    record = json.loads(record_bytes)
    validate_record(worker, record, database)
    library = checked_path(request['library'])
    if not library.is_file():
        raise ValueError('RocksDB library must be a file')
    db = RocksReadOnly(library, database)
    try:
        headers = {}
        for key, value in db.headers():
            header = decode_header(key, value, db.get(b'score/' + key[7:]))
            headers[header['hash']] = header
            if len(headers) > 12000:
                raise ValueError('Header export exceeds bounded output size')
        current = db.get(b'CurrentBlock')
        if current is None or len(current) != 32 or current.hex() not in headers:
            raise ValueError('Missing current head header')
        height, transactions = decimal(db.get(b'BlockHeight')), decimal(db.get(b'ChainTx'))
        head = headers[current.hex()]
        if head['height'] != height or head['score'] != transactions:
            raise ValueError('Current head height/score metadata mismatch')
    finally:
        db.close()
    if record_path.read_bytes() != record_bytes:
        raise RuntimeError('Experiment process record changed during export')
    validate_record(worker, record, database)
    result = {'ok': True, 'read_only': True, 'run': request['run'], 'node': request['node'],
              'database': str(database), 'header_count': len(headers), 'headers': headers,
              'current': {'hash': current.hex(), 'height': height, 'transactions': transactions},
              'process_record_sha256': hashlib.sha256(record_bytes).hexdigest()}
    if len(json.dumps(result, separators=(',', ':')).encode('utf-8')) > MAX_OUTPUT_BYTES:
        raise ValueError('Header export exceeds 4 MiB output limit')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'run', 'node', 'library'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        result = Export(vars(args))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, separators=(',', ':')))
        return 1
    print(json.dumps(result, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
