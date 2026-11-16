"""Low-overhead published-head boundaries; common ancestry is proved offline."""
import concurrent.futures
import inspect
import json
from pathlib import Path
import socket
import struct
import threading
import time

import RetestObserve


def Save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def Install(args):
    import Experiments as E
    import Cluster as C
    from NetworkTest import Frame, ReadAll
    RetestObserve.Install(args)
    CutoffLoad = inspect.getclosurevars(E.LoadCase).nonlocals['CutoffLoad']

    class HeadClient:
        def __init__(self, node):
            self.node, self.socket = node, None

        def Close(self):
            if self.socket:
                self.socket.close()
                self.socket = None

        def Query(self):
            try:
                if self.socket is None:
                    self.socket = socket.create_connection((self.node['host'], self.node['port']), 1)
                    self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.socket.sendall(b'\x02')
                self.socket.settimeout(.4)
                self.socket.sendall(Frame(19, b''))
                kind, size = struct.unpack('<BI', ReadAll(self.socket, 5))
                if kind != 20 or size != 80:
                    raise ValueError('Invalid published-head response')
                data = ReadAll(self.socket, 80)
                height, count = struct.unpack('<QQ', data[64:80])
                return {'genesis': data[:32].hex(), 'hash': data[32:64].hex(),
                        'height': height, 'transactions': count}
            except BaseException:
                self.Close()
                raise

    def LoadCase(cluster, case, plan, dataset, genesis, output):
        load = CutoffLoad(cluster, dataset, case['rate'], 12, 'steady', 'broadcast', 2, 128, False)
        clients = [HeadClient(node) for node in cluster.nodes]
        stop_records, stop_errors, boundaries = {}, {}, []
        pool = concurrent.futures.ThreadPoolExecutor(len(clients))
        done, cutoff_done = threading.Event(), threading.Event()
        stop_lock = threading.Lock()
        stopped = False
        thread, cutoff, guard = None, None, None

        def StopNodes():
            nonlocal stopped
            with stop_lock:
                if stopped:
                    return
                def Stop(index):
                    try:
                        stop_records[cluster.nodes[index]['id']] = cluster.StopOne(index)
                    except Exception as error:
                        stop_errors[cluster.nodes[index]['id']] = str(error)
                cluster.All(Stop)
                stopped = True

        def Point(target):
            def Query(index):
                start = time.monotonic()
                try:
                    head, error = clients[index].Query(), None
                except Exception as exc:
                    head, error = None, str(exc)
                row = {'node': cluster.nodes[index]['id'], 'start_seconds': start - load.started,
                       'end_seconds': time.monotonic() - load.started, 'head': head}
                if error:
                    row['error'] = error
                return row
            return {'target_seconds': target, 'nodes': list(pool.map(Query, range(len(clients))))}

        try:
            # Establish independent query connections while there is no workload.
            list(pool.map(lambda client: client.Query(), clients))
            thread = threading.Thread(target=load.Run)
            thread.start()
            while not load.started and thread.is_alive():
                time.sleep(.001)
            if not load.started:
                raise RuntimeError('Load generator did not start')
            def Cutoff():
                if not cutoff_done.wait(max(0, load.started + 12 - time.monotonic())):
                    load.HardStop()
            def Guard():
                if not done.wait(max(0, load.started + 17 - time.monotonic())):
                    load.HardStop()
                    StopNodes()
            cutoff = threading.Thread(target=Cutoff)
            guard = threading.Thread(target=Guard)
            cutoff.start()
            guard.start()
            for target in (2, 12):
                time.sleep(max(0, load.started + target - time.monotonic()))
                boundaries.append(Point(target))
                Save(output / 'head-boundaries.json', boundaries)
            load.HardStop()
            thread.join(timeout=max(0, load.started + 17 - time.monotonic()))
            if thread.is_alive():
                raise RetestObserve.ProcessStopError('Sender did not stop')
            drain = None
            for target in (13, 14, 15, 16):
                if time.monotonic() >= load.started + 16.5:
                    break
                time.sleep(max(0, load.started + target - time.monotonic()))
                drain = Point(target)
                heads = [r['head'] for r in drain['nodes']]
                if all(heads) and len({h['hash'] for h in heads}) == 1 and heads[0]['transactions'] in (load.sent, load.attempted):
                    break
            def SentAt(seconds):
                return max((b['end'] for b in load.batch_intervals if b['end_seconds'] <= seconds), default=0)
            return {'offered_tps': case['rate'], 'achieved_send_tps': (SentAt(12) - SentAt(2)) / 10,
                    'sent_unique_attempted_upper_bound': load.attempted,
                    'sent_unique_completed_all_ingress': load.sent, 'sent_unique': load.sent,
                    'sent_wire_copies_completed': load.wire_transactions,
                    'completed_per_ingress': load.completed_per_ingress,
                    'load_error': load.error,
                    'load_success': load.error is None and (load.finished - load.started >= 11.995 or load.sent == load.count),
                    'generator_finished_seconds': load.finished - load.started,
                    'partial_batch': load.partial_batch, 'drained': False,
                    'head_drain_observation': drain,
                    'measurement_method': 'independent RPC19 at 2s/12s; ancestor graph reconstructed after stop',
                    'sustainable_evaluated': False}
        finally:
            load.HardStop()
            cutoff_done.set()
            if thread and thread.ident:
                thread.join(timeout=3)
            try:
                StopNodes()
            finally:
                done.set()
                for item in (cutoff, guard):
                    if item and item.ident:
                        item.join(timeout=95)
                for client in clients:
                    client.Close()
                pool.shutdown(wait=True)
                Save(output / 'head-boundaries.json', boundaries)
                Save(output / 'sender-batches.json', load.batch_intervals)
                Save(output / 'sender-state.json', {'completed_all_ingress': load.sent,
                     'attempted_upper_bound': load.attempted, 'partial_batch': load.partial_batch,
                     'error': load.error, 'started_monotonic': load.started,
                     'finished_monotonic': getattr(load, 'finished', None)})
                Save(output / 'stop-records.json', {'nodes': stop_records, 'errors': stop_errors})
            if stop_errors:
                raise RetestObserve.ProcessStopError(str(stop_errors))

    E.LoadCase = LoadCase
