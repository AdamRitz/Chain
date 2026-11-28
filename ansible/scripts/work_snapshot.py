"""Validate and restore a stopped Work test ledger and per-identity proofs."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import tarfile
import tempfile


def Native(path):
    value = str(path)
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = '\\\\?\\' + str(Path(path).absolute())
    return Path(value)


def Digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()


def Hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Snapshot requires a 256-bit hexadecimal hash')
    return value


def File(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('Snapshot input must be an absolute ordinary file')
    return path.resolve(strict=True)


def CheckProof(data, round_number, anchor, previous):
    if (len(data) != 346 or data[:6] != b'WORK\x01\x00' or
            struct.unpack_from('<Q', data, 6)[0] != round_number or
            data[14:46] != bytes.fromhex(anchor) or data[46:146] != previous):
        raise ValueError('Work file has incorrect encoding, round, anchor or previous output')
    return data[146:246]


def Prepare(path, plan, accounts, output):
    """Reuse the original genesis/keys and package only each node's own public proofs."""
    from nacl.signing import SigningKey
    source = File(path)
    record = json.loads(source.read_text(encoding='utf-8'))
    if record.get('schema') != 1 or record.get('transactions') != 0:
        raise ValueError('Use a schema 1 snapshot with zero account transactions')
    chain, head = Hash(record.get('chain_id')), Hash(record.get('head'))
    height = record.get('height')
    genesis_path, ledger = File(record['genesis']), File(record['ledger_archive'])
    if Digest(genesis_path) != Hash(record['genesis_sha256']) or Digest(ledger) != Hash(record['ledger_sha256']):
        raise ValueError('Snapshot genesis or ledger SHA256 mismatch')
    genesis = json.loads(genesis_path.read_text(encoding='utf-8'))
    config = genesis.get('vdf', {})
    sample_count = config.get('work_groups', 4)*config.get('work_group_size', 8)
    if (not config.get('work_election') or config.get('epoch_blocks') != 160 or
            type(height) is not int or height < 1+(sample_count+1)*160):
        raise ValueError('Snapshot lacks a complete mature Work history')
    if genesis.get('accounts') != accounts.get('accounts') or genesis.get('base_fee') != accounts.get('base_fee'):
        raise ValueError('Snapshot funded accounts differ from the signed dataset')
    rounds = (height-1)//160
    anchors_path = File(record['anchors_file'])
    if Digest(anchors_path) != Hash(record['anchors_sha256']):
        raise ValueError('Snapshot historical anchor SHA256 mismatch')
    anchors_record = json.loads(anchors_path.read_text(encoding='utf-8'))
    anchors = anchors_record.get('anchors', [])
    if (anchors_record.get('chain_id') != chain or anchors_record.get('height') != height or
            anchors_record.get('head') != head or anchors_record.get('iterations') != config['work_iterations'] or
            len(anchors) != rounds or any(row.get('round') != number for number, row in enumerate(anchors))):
        raise ValueError('Snapshot historical anchors do not match the stopped ledger')
    expected_anchors = [Hash(row['anchor']) for row in anchors]
    ids = [node['id'] for node in plan['nodes']]
    identities = record.get('identity_files', {})
    if set(identities) != set(ids):
        raise ValueError('Snapshot identity set differs from the selected nodes')
    root = Path(record['work_directory'])
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('Work proof directory is missing')
    archive_dir = output/'snapshot-work'
    archive_dir.mkdir()
    work_archives, public = {}, []
    for node in ids:
        seed_file = File(identities[node])
        if seed_file.stat().st_size > 128:
            raise ValueError('Invalid Work identity seed file')
        key = bytes(SigningKey(bytes.fromhex(seed_file.read_text().strip())).verify_key).hex()
        public.append(key)
        prefix = PurePosixPath('work-vdf')/chain/key/str(config['work_iterations'])/'proofs'
        directory = root/Path(str(prefix))
        if directory.resolve() != directory or not Native(directory).is_dir():
            raise ValueError('Missing Work proofs for node '+node)
        proof_files = {}
        for proof in Native(directory).iterdir():
            match = re.fullmatch(r'([0-9]+)-([0-9a-f]{64})\.work', proof.name)
            if not match or proof.is_symlink() or not proof.is_file():
                raise ValueError('Unexpected Work proof directory entry')
            number = int(match[1])
            if number in proof_files or number >= rounds or match[2] != expected_anchors[number]:
                raise ValueError('Work proof rounds differ from the snapshot')
            proof_files[number] = (proof, match[2])
        if set(proof_files) != set(range(rounds)):
            raise ValueError('Every identity must have all completed Work rounds')
        previous = bytes(100)
        archive = archive_dir/(node+'.tar.gz')
        with tarfile.open(archive, 'w:gz') as target:
            for number in range(rounds):
                proof, anchor = proof_files[number]
                previous = CheckProof(proof.read_bytes(), number, anchor, previous)
                target.add(proof, arcname=str(prefix/proof.name), recursive=False)
        work_archives[node] = {'archive': str(archive), 'sha256': Digest(archive), 'public_key': key}
    if len(set(public)) != len(public) or public[:5] != config.get('bootstrap_producers'):
        raise ValueError('Snapshot identities do not match its original bootstrap committee')
    snapshot = {'schema': 1, 'chain_id': chain, 'head': head, 'height': height, 'rounds': rounds,
                'transactions': 0, 'iterations': config['work_iterations'],
                'ledger_archive': str(ledger), 'ledger_sha256': record['ledger_sha256'],
                'work_archives': work_archives}
    return genesis, {node: str(File(identities[node])) for node in ids}, snapshot


def Extract(archive, expected, destination, *, work_prefix=None, rounds=0):
    """Read only regular archive files, with explicit paths and byte limits."""
    archive = File(archive)
    if Digest(archive) != Hash(expected):
        raise ValueError('Snapshot archive SHA256 mismatch')
    total, names, proofs = 0, set(), {}
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or '\\' in member.name or member.name in names:
                raise ValueError('Unsafe or repeated snapshot archive path')
            names.add(member.name)
            if len(names) > 100000:
                raise ValueError('Snapshot archive has too many entries')
            if member.isdir():
                if path != PurePosixPath('db') and not work_prefix:
                    raise ValueError('Unexpected ledger directory')
                continue
            if not member.isfile() or member.size < 0:
                raise ValueError('Snapshot archives may contain only ordinary files')
            total += member.size
            if total > 512*1024*1024:
                raise ValueError('Empty-ledger snapshot exceeds size limits')
            if work_prefix:
                if path.parent != PurePosixPath(work_prefix):
                    raise ValueError('Work archive belongs to a different identity')
                match = re.fullmatch(r'([0-9]+)-([0-9a-f]{64})\.work', path.name)
                if not match or member.size != 346 or int(match[1]) in proofs:
                    raise ValueError('Invalid or duplicate Work archive record')
                proofs[int(match[1])] = (path, match[2])
                target = destination/Path(str(path))
            else:
                if (len(path.parts) != 2 or path.parts[0] != 'db' or not re.fullmatch(
                        r'CURRENT|MANIFEST-\d+|OPTIONS-\d+|\d+\.(?:sst|ldb|log|blob)', path.name)):
                    raise ValueError('Ledger archive must contain only db/RocksDB files')
                if path.name.lower().endswith(('.seed', '.work')) or path.name in ('process.json', 'genesis.json'):
                    raise ValueError('Ledger archive contains identity or process data')
                target = destination/path.name
            target = Native(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as incoming, target.open('xb') as output:
                shutil.copyfileobj(incoming, output)
    if work_prefix:
        if set(proofs) != set(range(rounds)):
            raise ValueError('Restored Work history is incomplete')
        previous = bytes(100)
        for number in range(rounds):
            path, anchor = proofs[number]
            previous = CheckProof(Native(destination/Path(str(path))).read_bytes(), number, anchor, previous)
    elif not (destination/'CURRENT').is_file() or not any(destination.glob('MANIFEST-*')):
        raise ValueError('Ledger archive lacks RocksDB metadata')


def Restore(root, run, node, metadata):
    """Build a retained source DB outside the disposable experiment case."""
    root = Path(root).resolve()
    directory = root/'.work-snapshots'/run
    if directory.resolve() != directory or directory.is_symlink():
        raise ValueError('Snapshot staging directory contains a link')
    chain, key = Hash(metadata['chain_id']), Hash(metadata['public_key'])
    rounds, iterations = metadata['rounds'], metadata['iterations']
    if type(rounds) is not int or not 1 <= rounds <= 100000 or type(iterations) is not int or not 4096 <= iterations <= 100000000:
        raise ValueError('Invalid restored Work parameters')
    destination = directory/(node+'-db')
    if destination.exists():
        raise FileExistsError('Restored database source already exists')
    directory.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.restore-', dir=directory))
    try:
        Extract(directory/'ledger.tar.gz', metadata['ledger_sha256'], temporary)
        prefix = PurePosixPath('work-vdf')/chain/key/str(iterations)/'proofs'
        Extract(directory/(node+'.tar.gz'), metadata['work_sha256'], temporary,
                work_prefix=str(prefix), rounds=rounds)
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(Native(temporary))
    return destination
