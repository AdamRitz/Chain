"""Prepare a Work-election genesis offline from funded accounts and public identities.

Example (the caller chooses the common start timestamp before deployment):
  python work_genesis.py --accounts-genesis funded.json --identities identities.json \
      --start-time-ms 1806382800000 --out work-genesis.json

identities.json is [{"id": "node-0001", "public_key": "64 hex characters", ...}].
The first five identities bootstrap the chain. Optional seed_file fields are
ignored: this tool never reads, generates or copies a node's private key.
"""

import argparse
import json
from pathlib import Path
import re
import sys


UINT64_MAX = (1 << 64) - 1
MIN_ITERATIONS = 4096
MAX_ITERATIONS = 100_000_000
MAX_GROUPS = 32
MAX_GROUP_SIZE = 64
MAX_WORKS = 256


def Integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer in {minimum}..{maximum}')
    return value


def PublicKey(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value):
        raise ValueError('public_key must contain exactly 64 hexadecimal characters')
    return value.lower()


def UniqueObject(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('JSON object contains a repeated key')
        result[key] = value
    return result


def ReadJson(path, maximum_bytes):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > maximum_bytes:
        raise ValueError('Input JSON is missing, not a regular file, or too large')
    with path.open('r', encoding='utf-8-sig') as source:
        return json.load(source, object_pairs_hook=UniqueObject)


def MakeGenesis(accounts_genesis, identities, start_time_ms, *, work_iterations=65536,
                work_groups=4, work_group_size=8):
    """Return public genesis fields with the same account keys, balances and fee."""
    Integer(start_time_ms, 'start_time_ms', 1, UINT64_MAX)
    if start_time_ms % 200:
        raise ValueError('start_time_ms must be a multiple of the 200 ms slot length')
    Integer(work_iterations, 'work_iterations', MIN_ITERATIONS, MAX_ITERATIONS)
    Integer(work_groups, 'work_groups', 1, MAX_GROUPS)
    Integer(work_group_size, 'work_group_size', 1, MAX_GROUP_SIZE)
    if work_groups * work_group_size > MAX_WORKS:
        raise ValueError('work_groups * work_group_size must not exceed 256')
    if not isinstance(accounts_genesis, dict) or 'vdf' in accounts_genesis:
        raise ValueError('Use a funded accounts genesis without a vdf section')
    accounts = accounts_genesis.get('accounts')
    if not isinstance(accounts, list) or len(accounts) > 1_000_000:
        raise ValueError('accounts must be an array with at most 1,000,000 entries')
    Integer(accounts_genesis.get('base_fee', 0), 'base_fee', 0, UINT64_MAX)
    seen_accounts = set()
    public_accounts = []
    supply = 0
    for row in accounts:
        if not isinstance(row, dict):
            raise ValueError('Each account must be an object')
        public_key = PublicKey(row.get('public_key'))
        if public_key in seen_accounts:
            raise ValueError('Genesis contains a duplicate account')
        seen_accounts.add(public_key)
        supply += Integer(row.get('balance'), 'balance', 0, UINT64_MAX)
        public_accounts.append({'public_key': public_key, 'balance': row['balance']})
        if supply > UINT64_MAX:
            raise ValueError('Total genesis supply exceeds UINT64_MAX')
    if not isinstance(identities, list) or not 1 <= len(identities) <= 100_000:
        raise ValueError('identities must contain 1..100,000 node records')
    seen_ids, seen_keys, bootstrap = set(), set(), []
    for row in identities:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'].strip():
            raise ValueError('Each node identity requires a nonempty string id')
        if len(row['id']) > 256 or row['id'] in seen_ids:
            raise ValueError('Node ids must be unique and at most 256 characters')
        seen_ids.add(row['id'])
        public_key = PublicKey(row.get('public_key'))
        if public_key == '0' * 64 or public_key in seen_keys:
            raise ValueError('Node public keys must be nonzero and unique')
        seen_keys.add(public_key)
        if len(bootstrap) < 5:
            bootstrap.append(public_key)

    # Copy only public genesis fields. Unrelated input fields, including any
    # accidentally supplied key material, never enter the output file.
    genesis = {'base_fee': accounts_genesis.get('base_fee', 0), 'accounts': public_accounts}
    genesis['vdf'] = {
        'enabled': True,
        'producer_rotation': True,
        'epoch_blocks': 160,
        'consecutive_blocks': 32,
        'producer_timeout_slots': 2,
        'iterations': 65536,
        'slot_ms': 200,
        'start_time_ms': start_time_ms,
        'fallback_slots': 20,
        'future_slots': 1,
        'producers': 5,
        'work_election': True,
        'work_iterations': work_iterations,
        'work_groups': work_groups,
        'work_group_size': work_group_size,
        'commit_start_slot': 80,
        'commit_end_slot': 90,
        'reveal_end_slot': 100,
        'ticket_end_slot': 120,
        'proof_end_slot': 140,
        'bootstrap_producers': bootstrap,
    }
    return genesis


def Main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--accounts-genesis', type=Path, required=True)
    parser.add_argument('--identities', type=Path, required=True)
    parser.add_argument('--start-time-ms', type=int, required=True,
                        help='Common Unix timestamp in milliseconds; must be divisible by 200')
    parser.add_argument('--work-iterations', type=int, default=65536,
                        help='VDF iteration count; default is temporary and has not been calibrated')
    parser.add_argument('--work-groups', type=int, default=4)
    parser.add_argument('--work-group-size', type=int, default=8)
    parser.add_argument('--out', type=Path, required=True, help='New genesis file; an existing path is never replaced')
    args = parser.parse_args(argv)
    try:
        accounts = ReadJson(args.accounts_genesis, 128 * 1024 * 1024)
        identities = ReadJson(args.identities, 32 * 1024 * 1024)
        genesis = MakeGenesis(accounts, identities, args.start_time_ms,
                              work_iterations=args.work_iterations,
                              work_groups=args.work_groups,
                              work_group_size=args.work_group_size)
        # Exclusive creation also handles two controllers choosing the same output.
        content = json.dumps(genesis, ensure_ascii=False, indent=2) + '\n'
        with args.out.open('x', encoding='utf-8', newline='\n') as output:
            output.write(content)
        print(f'Created Work genesis with {len(genesis["vdf"]["bootstrap_producers"])} bootstrap producers: {args.out}')
        return 0
    except (OSError, ValueError, TypeError) as error:
        print(f'Work genesis preparation failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(Main())
