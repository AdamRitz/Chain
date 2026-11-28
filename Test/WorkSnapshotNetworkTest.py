"""Restore five nodes from a completed Work snapshot and check real cache reuse.

  python Test/WorkSnapshotNetworkTest.py --bin build --snapshot /path/snapshot.json \
      --work /path/new-restore-test

Uses the original genesis, private identities and all historical Work proofs.
No transactions are sent. The test retains its databases and result.json.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import sys
import time

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ansible' / 'scripts'))
import work_snapshot
from work_history_prepare import FreePorts, Query, Save
from ClusterWorker import FindProcess, Handle


class SnapshotCase:
    def __init__(self, binary, snapshot, output, timeout):
        binary = Path(binary).resolve(strict=True)
        self.binary = binary / ('boost.exe' if os.name == 'nt' else 'boost') if binary.is_dir() else binary
        if not self.binary.is_file():
            raise ValueError('Missing complete node executable')
        self.source = Path(snapshot).resolve(strict=True)
        record = json.loads(self.source.read_text())
        public = json.loads((Path(record['genesis']).parent / 'identities.json').read_text())
        original = json.loads(Path(record['genesis']).read_text())
        self.output = Path(output).resolve()
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        package = self.output / 'package'
        package.mkdir()
        self.genesis, self.identities, self.snapshot = work_snapshot.Prepare(self.source,
            {'nodes': [{'id': row['id']} for row in public]},
            {key: original[key] for key in ('accounts', 'base_fee')}, package)
        self.selected = [row['id'] for row in public[:5]]
        self.ports, self.owned = FreePorts(5), {}
        self.deadline = time.monotonic() + timeout
        self.timeout = timeout
        self.minimum_election = (self.snapshot['height'] - 2) // 160 + 1
        self.report = {'status': 'running', 'starts': [], 'stops': [], 'snapshot_height': self.snapshot['height'],
                       'snapshot_head': self.snapshot['head'], 'rounds': self.snapshot['rounds'],
                       'minimum_election': self.minimum_election, 'nodes': self.selected}

    def Budget(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Snapshot restore regression exceeded its budget')
        for node, value in self.owned.items():
            if FindProcess(value['process']) is None:
                raise RuntimeError('Restored node exited: ' + node)

    def Start(self):
        root = self.output / 'retained'
        run = 'snapshot_restore'
        staging = root / '.work-snapshots' / run
        staging.mkdir(parents=True, mode=0o700)
        shutil.copyfile(self.snapshot['ledger_archive'], staging / 'ledger.tar.gz')
        for index, node in enumerate(self.selected):
            self.Budget()
            work = self.snapshot['work_archives'][node]
            shutil.copyfile(work['archive'], staging / (node + '.tar.gz'))
            database = work_snapshot.Restore(root, run, node,
                {'chain_id': self.snapshot['chain_id'], 'public_key': work['public_key'],
                 'rounds': self.snapshot['rounds'], 'iterations': self.snapshot['iterations'],
                 'ledger_sha256': self.snapshot['ledger_sha256'], 'work_sha256': work['sha256']})
            request = {'action': 'start', 'root': str(self.output / 'cases'), 'run': run, 'node': node,
                       'binary': str(self.binary), 'genesis': self.genesis, 'prepared_database': str(database)}
            config = self.output / (node + '-config.json')
            Save(config, {'node': {'peers': [f'127.0.0.1:{port}' for other, port in enumerate(self.ports) if other != index]}})
            stop = self.output / (node + '-stop')
            options = {'config': config, 'vdf-key': self.identities[node], 'bind': '127.0.0.1',
                       'port': self.ports[index], 'produce': 1, 'sync': 1, 'block-ms': 200,
                       'max-block-txs': 6000, 'max-peers': 16, 'peer-discovery': 0,
                       'clock-sync': 0, 'io-threads': 1, 'verify-threads': 1, 'candidate-threads': 1,
                       'block-verify-threads': 1, 'query-threads': 1, 'experiment-index': 1,
                       'stop-file': stop, 'run-seconds': int(self.timeout) + 30}
            request['arguments'] = [str(part) for key, value in options.items() for part in ('--' + key, value)]
            process = Handle(request)
            self.owned[node] = {'request': request, 'process': process, 'stop': stop}
            self.report['starts'].append({'node': node, 'pid': process['pid'], 'directory': process['directory']})

    def Stats(self):
        with ThreadPoolExecutor(max_workers=5) as executor:
            rows = list(executor.map(lambda port: Query(port, 13), self.ports))
        if any(row.get('failed') or row.get('chain_transactions') != 0 for row in rows):
            raise ValueError('Snapshot node failed or changed the transaction count')
        return rows

    def Wait(self, check):
        while True:
            self.Budget()
            try:
                rows = self.Stats()
                result = check(rows)
                if result:
                    return result
            except (OSError, ConnectionError, TimeoutError):
                pass
            time.sleep(.2)

    def Loaded(self, rows):
        count = self.snapshot['rounds']
        # The first complete load must finish before a new round needs Work.
        if any(row['vdf']['work_computed'] != 0 for row in rows):
            raise ValueError('Work was recomputed before the restored history finished loading')
        if not all(row['vdf']['work_completed'] >= count and row['vdf']['work_disk_hits'] >= count for row in rows):
            return None
        for index, row in enumerate(rows):
            head = Query(self.ports[index], 19, raw=True)
            anchor = Query(self.ports[index], 29, struct.pack('<Q', self.snapshot['height']))
            if (head[:32].hex() != self.snapshot['chain_id'] or anchor['hash'] != self.snapshot['head'] or
                    anchor['transactions'] != 0 or row['vdf']['work_verify_threads'] != 2):
                raise ValueError('Restored chain or Work verifier configuration differs')
        self.report['loaded'] = [{'node': node, 'height': row['height'],
            **{key: row['vdf'][key] for key in ('work_completed', 'work_computed', 'work_disk_hits', 'proof_checks')}}
            for node, row in zip(self.selected, rows)]
        return rows

    def Election(self):
        selected = None

        def Ready(rows):
            nonlocal selected
            states = [row['vdf'] for row in rows]
            if not all(row['work_sample_count'] == 32 and row['work_mature_rounds'] >= 32 for row in states):
                return None
            if (selected and all(row['epoch'] == selected['epoch'] + 1 and row['producers'] == selected['committee'] for row in states)):
                minimum = min(row['height'] for row in rows)
                heads = [Query(port, 29, struct.pack('<Q', minimum)) for port in self.ports]
                if minimum > self.snapshot['height'] and heads[0] and all(head == heads[0] for head in heads):
                    self.report.update(election=selected, common=heads[0])
                    return rows
            first = states[0]
            committee = first.get('next_producers', [])
            if (first['epoch'] >= self.minimum_election and len(committee) == 5 and len(set(committee)) == 5 and
                    all(row['epoch'] == first['epoch'] and row['election_frozen'] and len(row['candidates']) >= 5 and
                        row['next_producers'] == committee for row in states)):
                selected = {'epoch': first['epoch'], 'committee': committee}
            return None

        return self.Wait(Ready)

    def Accounts(self):
        accounts = self.genesis['accounts']
        for begin in range(0, len(accounts), 128):
            batch = accounts[begin:begin + 128]
            payload = b''.join(bytes.fromhex(row['public_key']) for row in batch)
            expected = [{'balance': row['balance'], 'nonce': 0} for row in batch]
            for port in self.ports:
                self.Budget()
                if Query(port, 37, payload) != expected:
                    raise ValueError('Restored genesis balance or nonce changed')
        self.report['accounts_checked_per_node'] = len(accounts)

    def Stop(self):
        owned, errors = list(self.owned.items()), []
        for node, value in owned:
            try:
                value['stop'].touch()
            except OSError as error:
                errors.append(node + ' stop file: ' + str(error))
        end = time.monotonic() + 30
        for node, value in owned:
            try:
                process = FindProcess(value['process'])
                exit_code = None
                if process:
                    try:
                        exit_code = process.wait(timeout=max(.1, end - time.monotonic()))
                    except psutil.TimeoutExpired:
                        errors.append(node + ' required forced stop')
                        Handle({**value['request'], 'action': 'stop'})
                metrics = json.loads((Path(value['process']['directory']) / 'final.json').read_text())
                work = metrics['vdf']
                pending = {key: work[key] for key in ('pending_proofs', 'pending_beacons', 'pending_bytes', 'work_priority_pending')}
                self.report['stops'].append({'node': node, 'exit_code': exit_code, 'failed': metrics['failed'], 'pending': pending,
                    'height': metrics['height'], 'head': metrics['head'], 'transactions': metrics['chain_transactions']})
                if exit_code not in (None, 0) or metrics['failed'] or any(pending.values()) or metrics['chain_transactions'] != 0:
                    errors.append(node + ' did not stop with empty Work queues')
            except BaseException as error:
                errors.append(node + ': ' + str(error))
                try:
                    Handle({**value['request'], 'action': 'stop'})
                except BaseException as cleanup:
                    errors.append(node + ' cleanup: ' + str(cleanup))
            finally:
                self.owned.pop(node, None)
        if errors:
            raise RuntimeError('; '.join(errors))

    def Run(self):
        started = time.monotonic()
        try:
            self.Start()
            self.Wait(self.Loaded)
            self.Election()
            self.Accounts()
            self.Stop()
            self.report['status'] = 'passed'
            return self.report
        except BaseException as error:
            self.report.update(status='failed', error=str(error), error_type=type(error).__name__)
            raise
        finally:
            try:
                self.Stop()
            except BaseException as cleanup:
                self.report.update(status='failed', cleanup_error=str(cleanup))
                raise
            finally:
                self.report['elapsed_seconds'] = time.monotonic() - started
                Save(self.output / 'result.json', self.report)


def Main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bin', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args(argv)
    if not 90 <= args.timeout <= 600:
        parser.error('--timeout must be 90..600 seconds')
    def Interrupted(number, _):
        raise KeyboardInterrupt(f'Interrupted by signal {number}')
    previous = {number: signal.signal(number, Interrupted) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        case = SnapshotCase(args.bin, args.snapshot, args.work, args.timeout)
        result = case.Run()
        print(json.dumps({'status': result['status'], 'result': str(case.output / 'result.json'),
                          'elapsed_seconds': result['elapsed_seconds']}), flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError, KeyboardInterrupt) as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}), flush=True)
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    raise SystemExit(Main())
