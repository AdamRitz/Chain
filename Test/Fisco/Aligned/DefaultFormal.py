"""Controller-only five short FISCO runs, unchanged official node parameters.

Owns only unique transient units and /var/tmp/fisco-experiments/<case>/data.
Default fisco-bcos services are restored in finally. No Chain service is touched.
"""
import argparse
import concurrent.futures
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import threading
import time

from DriverStage import Controller, Load, Parallel, Run, Save


def Number(value):
    return int(value, 0) if isinstance(value, str) and value.startswith("0x") else int(value)


class Formal(Controller):
    def __init__(self, args):
        super().__init__(args)
        if len(self.hosts) != 10:
            raise ValueError("Exactly ten hosts required")
        self.rpc_log = None
        self.rpc_lock = threading.Lock()
        self.block_cache = {}

    def Remote(self, host, script):
        process = subprocess.run(["ssh", *self.ssh_options, "root@" + host, "python3 -"], input=script,
                                 timeout=40, text=True, capture_output=True)
        if process.returncode:
            raise RuntimeError(host + ': ' + process.stderr + process.stdout)
        return process.stdout

    def Download(self, host, source, destination):
        return Run(["scp", *self.ssh_options, "root@" + host + ":" + source, str(destination)], timeout=60)

    def Ready(self, seconds=30):
        deadline = time.monotonic() + seconds
        last = None
        while True:
            try:
                value = self.Check()
                if value['valid'] and all(Number(row['pending']) == 0 for row in value['nodes']):
                    return value
                last = value
            except (OSError, RuntimeError, TimeoutError, ValueError) as error:
                last = repr(error)
            if time.monotonic() >= deadline:
                raise RuntimeError('Fresh-node readiness timed out: ' + str(last))
            time.sleep(.5)

    def Rpc(self, host, method, *params, deadline=None):
        begin, mono = time.time(), time.monotonic()
        log_sink = self.rpc_log
        timeout = min(1.0, deadline - mono) if deadline is not None else 3.0
        if timeout <= 0:
            raise TimeoutError("Observation deadline reached")
        connection = http.client.HTTPSConnection(host, 20200, context=self.context, timeout=timeout)
        row = {"host": host, "method": method, "params": params,
               "begin_epoch": begin, "begin_monotonic": mono}
        try:
            body = json.dumps({"jsonrpc": "2.0", "method": method, "params": ["group0", "", *params], "id": 1})
            connection.request("POST", "/", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            value = json.loads(response.read())
            if "error" in value:
                raise RuntimeError(value["error"])
            row["result"] = value["result"]
            return row
        except Exception as error:
            row["error"] = repr(error)
            raise
        finally:
            row.update(end_epoch=time.time(), end_monotonic=time.monotonic())
            connection.close()
            if log_sink is not None:
                # Log small RPC results in full. Block transaction lists are reduced to hash/count;
                # receipt/block evidence relevant to sampled positions is retained separately.
                record = dict(row)
                if method == "getBlockByNumber" and isinstance(record.get("result"), dict):
                    block = record["result"]
                    record["result"] = {key: block.get(key) for key in ["hash", "number", "stateRoot", "timestamp"]}
                    record["transaction_entries"] = len(block.get("transactions", block.get("transactionHashes", [])))
                with self.rpc_lock:
                    if not log_sink.closed:
                        log_sink.write(json.dumps(record) + "\n")
                        log_sink.flush()

    def SnapshotTimed(self, deadline):
        def One(host):
            count = self.Rpc(host, "getTotalTransactionCount", deadline=deadline)
            pending = self.Rpc(host, "getPendingTxSize", deadline=deadline)
            value = count["result"]
            header = self.Rpc(host, "getBlockByNumber", Number(value["blockNumber"]), True, True, deadline=deadline)
            return {"host": host, "count": Number(value["transactionCount"]),
                    "failed_count": Number(value["failedTransactionCount"]), "height": Number(value["blockNumber"]),
                    "count_begin_epoch": count["begin_epoch"], "count_end_epoch": count["end_epoch"],
                    "count_begin_monotonic": count["begin_monotonic"], "count_end_monotonic": count["end_monotonic"],
                    "pending": Number(pending["result"]), "pending_begin_epoch": pending["begin_epoch"],
                    "pending_end_epoch": pending["end_epoch"], "head_hash": header["result"].get("hash"),
                    "state_root": header["result"].get("stateRoot"),
                    "head_begin_epoch": header["begin_epoch"], "head_end_epoch": header["end_epoch"]}
        rows = Parallel(One, self.hosts)
        return {"nodes": rows, "count_min": min(row["count"] for row in rows),
                "bracket_begin_epoch": min(row["count_begin_epoch"] for row in rows),
                "bracket_end_epoch": max(row["count_end_epoch"] for row in rows),
                "bracket_begin_monotonic": min(row["count_begin_monotonic"] for row in rows),
                "bracket_end_monotonic": max(row["count_end_monotonic"] for row in rows),
                "pending_mean": sum(row["pending"] for row in rows) / len(rows)}

    def Observe(self, sample, deadline):
        txhash = sample["hash"].lower()
        def One(host):
            receipt = self.Rpc(host, "getTransactionReceipt", sample["hash"], False, deadline=deadline)
            value = receipt["result"]
            answer = {"host": host, "receipt_begin_epoch": receipt["begin_epoch"],
                      "receipt_end_epoch": receipt["end_epoch"], "receipt": value}
            if value is None or Number(value.get("status", -1)) != 0 or int(value.get("output", "-1"),16) != 0:
                return answer
            if str(value.get("transactionHash", "")).lower() != txhash:
                raise ValueError("Receipt hash mismatch")
            height = Number(value["blockNumber"])
            key = (host, height)
            block = self.block_cache.get(key)
            if block is None:
                block = self.Rpc(host, "getBlockByNumber", height, False, True, deadline=deadline)
                self.block_cache[key] = block
            body = block["result"]
            entries = body.get("transactions", body.get("transactionHashes", []))
            hashes = [str(item.get("hash", item.get("transactionHash", ""))) if isinstance(item, dict) else str(item)
                      for item in entries]
            normalized = [value.lower() for value in hashes]
            if txhash not in normalized:
                raise ValueError("Receipt transaction absent from its block")
            answer.update(position={"height": height, "block_hash": body["hash"], "tx_index": normalized.index(txhash)},
                          block_begin_epoch=block["begin_epoch"], block_end_epoch=block["end_epoch"],
                          observed_end_epoch=max(receipt["end_epoch"], block["end_epoch"]),
                          observed_end_monotonic=max(receipt["end_monotonic"], block["end_monotonic"]))
            return answer
        rows = Parallel(One, self.hosts)
        positions = [row.get("position") for row in rows]
        same = bool(positions[0] and all(position == positions[0] for position in positions))
        end = max((row.get("observed_end_monotonic", 0) for row in rows), default=0)
        return {"hash": sample["hash"], "sender": sample["sender"], "scheduled_offset_ms": sample["scheduled_offset_ms"],
                "nodes": rows, "all_nodes_same_position": same, "within_observation_budget": end <= deadline,
                "observed_epoch": max((row.get("observed_end_epoch", 0) for row in rows), default=0),
                "position": positions[0] if same else None}

    def SetDefaults(self, action):
        def One(host):
            return {"host": host, "output": self.Remote(host, f"import subprocess\nr=subprocess.run(['systemctl',{action!r},'fisco-bcos'],capture_output=True,text=True,timeout=25)\nprint(r.stdout+r.stderr)\nassert r.returncode==0,r.returncode\n")}
        return Parallel(One, self.hosts)

    def PrepareNode(self, host, name, node_unit):
        script = f'''import pathlib,shutil,os,pwd,grp,hashlib,json,subprocess,configparser
base=pathlib.Path('/var/tmp/fisco-experiments')
assert not base.is_symlink()
base.mkdir(mode=0o755,exist_ok=True)
base=base.resolve()
target=base/{name!r}
assert target.parent==base and not target.exists()
source=pathlib.Path('/opt/fisco/nodes/node0')
config=configparser.ConfigParser();config.read(source/'config.ini')
assert config.get('storage','data_path',fallback='data')=='data','Default data path must remain relative data'
target.mkdir(mode=0o750)
for filename in ['config.ini','config.genesis','nodes.json']:shutil.copy2(source/filename,target/filename)
shutil.copytree(source/'conf',target/'conf')
uid=pwd.getpwnam('fisco').pw_uid;gid=grp.getgrnam('fisco').gr_gid
for p in [target,*target.rglob('*')]:
 os.chown(p,uid,gid);os.chmod(p,0o750 if p.is_dir() else 0o640)
manifest={{str(p.relative_to(target)):hashlib.sha256(p.read_bytes()).hexdigest() for p in target.rglob('*') if p.is_file()}}
for f,h in manifest.items():assert hashlib.sha256((source/f).read_bytes()).hexdigest()==h
(target/'input-manifest.json').write_text(json.dumps(manifest,indent=2))
args=['systemd-run','--quiet','--unit',{node_unit!r},'--uid=fisco','--gid=fisco','--working-directory='+str(target),
 '--property=LimitNOFILE=1024','--property=TimeoutStopSec=5','--property=KillMode=control-group',
 '--property=Restart=no','--property=RuntimeMaxSec=900','--setenv=RUST_LOG=bcos_wasm=error',
 '--property=StandardOutput=append:'+str(target/'stdout.log'),'--property=StandardError=append:'+str(target/'stderr.log'),
 '/opt/fisco/bin/fisco-bcos','-c','config.ini','-g','config.genesis']
r=subprocess.run(args,capture_output=True,text=True,timeout=15)
assert r.returncode==0,(r.stdout,r.stderr)
print(json.dumps({{'host':{host!r},'unit':{node_unit!r},'directory':str(target),'files':manifest,'command':args}}))
'''
        return json.loads(self.Remote(host, script))

    def LaunchDriver(self, host, name, rate, users, unit):
        directory = self.driver / "runs" / name
        command = [str(self.root / "toolchain/jdk/bin/java"), "-Xms64m", "-Xmx1g", "-XX:ActiveProcessorCount=2",
                   "-cp", "classes:lib/*", "NativeAlignedBench", "config.toml", "solidity", str(rate // 10), "2", "10",
                   str(directory / "driver.json"), str(users), str(directory / "go"), str(self.args.inflight), "short-budget"]
        script = f'''import pathlib,subprocess,os,json
os.umask(0o077)
root=pathlib.Path({str(directory)!r});root.mkdir(parents=True,exist_ok=False)
args=['systemd-run','--quiet','--unit',{unit!r},'--uid=root','--gid=root','--working-directory='+{str(self.driver)!r},
 '--property=TimeoutStopSec=1','--property=KillMode=control-group','--property=Restart=no','--property=RuntimeMaxSec=210',
 '--property=StandardOutput=append:'+str(root/'driver.log'),'--property=StandardError=append:'+str(root/'driver.log'),*{command!r}]
r=subprocess.run(args,capture_output=True,text=True,timeout=15);assert r.returncode==0,(r.stdout,r.stderr)
(root/'launch.json').write_text(json.dumps({{'unit':{unit!r},'command':args}},indent=2))
print(json.dumps({{'host':{host!r},'unit':{unit!r},'directory':str(root),'users':{users},'command':args}}))
'''
        return json.loads(self.Remote(host, script))

    def DriverState(self, host, name, unit):
        directory = self.driver / "runs" / name
        return json.loads(self.Remote(host, f'''import pathlib,subprocess,json
p=pathlib.Path({str(directory)!r})
r=subprocess.run(['systemctl','show',{unit!r},'--property=ActiveState','--property=SubState','--property=ExecMainStatus'],capture_output=True,text=True,timeout=5)
print(json.dumps({{'ready':(p/'go.ready').exists(),'samples':(p/'go.samples.json').exists(),'result':(p/'driver.json').exists(),'status':r.stdout}}))
'''))

    def ReadRemote(self, host, filename):
        return self.Remote(host, f"import pathlib\nprint(pathlib.Path({str(filename)!r}).read_text())\n")

    def StopOwned(self, host, name, node_unit, driver_unit):
        run = "/var/tmp/fisco-experiments/" + name
        driver_run = str(self.driver / "runs" / name)
        script = f'''import pathlib,subprocess,json,os,signal,time
units={ [driver_unit, node_unit]!r}
records=[]
for unit in units:
 before=subprocess.run(['systemctl','show',unit,'--property=MainPID','--property=ControlGroup','--property=ActiveState','--property=ExecMainStatus','--property=Result'],capture_output=True,text=True,timeout=5)
 stop=subprocess.run(['systemctl','stop',unit],capture_output=True,text=True,timeout=12)
 after=subprocess.run(['systemctl','show',unit,'--property=MainPID','--property=ActiveState','--property=Result'],capture_output=True,text=True,timeout=5)
 records.append({{'unit':unit,'before':before.stdout,'stop_code':stop.returncode,'stop_error':stop.stderr,'after':after.stdout}})
 if 'ActiveState=active' in after.stdout or 'ActiveState=deactivating' in after.stdout:
  subprocess.run(['systemctl','kill','--kill-who=all','--signal=SIGKILL',unit],capture_output=True,text=True,timeout=5)
alive=[]
for p in pathlib.Path('/proc').glob('[0-9]*'):
 try:
  args=(p/'cmdline').read_bytes().split(b'\\0');cwd=os.readlink(p/'cwd')
  node=(cwd=={run!r} or cwd.startswith({run!r}+'/')) and args and args[0].endswith(b'/fisco-bcos')
  driver=b'NativeAlignedBench' in args and any(a.startswith({driver_run.encode()!r}+b'/') for a in args)
  if node or driver:alive.append(int(p.name))
 except (OSError,ValueError):pass
proof={{'units':records,'associated_processes':alive,'verified_stopped':not alive}}
print(json.dumps(proof))
assert not alive,alive
'''
        return json.loads(self.Remote(host, script))

    def WatchdogStopUnit(self, host, unit):
        begin_epoch, begin_monotonic = time.time(), time.monotonic()
        try:
            remote = json.loads(self.Remote(host, f'''import subprocess,time,json
begin=time.time()
r=subprocess.run(['systemctl','stop',{unit!r}],capture_output=True,text=True,timeout=12)
after=subprocess.run(['systemctl','show',{unit!r},'--property=ActiveState','--property=MainPID'],capture_output=True,text=True,timeout=5)
print(json.dumps({{'remote_stop_begin_epoch':begin,'remote_stop_end_epoch':time.time(),'returncode':r.returncode,'stderr':r.stderr,'after':after.stdout}}))
'''))
            return {"host": host, "unit": unit, "request_begin_epoch": begin_epoch,
                    "request_begin_monotonic": begin_monotonic, "request_end_epoch": time.time(), **remote}
        except Exception as error:
            return {"host": host, "unit": unit, "request_begin_epoch": begin_epoch,
                    "request_begin_monotonic": begin_monotonic, "request_end_epoch": time.time(), "error": repr(error)}

    def Evidence(self, host, name, node_unit, driver_unit, out):
        run = "/var/tmp/fisco-experiments/" + name
        driver_run = str(self.driver / "runs" / name)
        script = f'''import pathlib,tarfile,subprocess,json,hashlib
root=pathlib.Path({run!r});driver=pathlib.Path({driver_run!r})
assert root.resolve().parent==pathlib.Path('/var/tmp/fisco-experiments').resolve()
if not root.exists():print(json.dumps({{'missing_run':True}}));raise SystemExit(0)
for unit in {[node_unit,driver_unit]!r}:
 r=subprocess.run(['journalctl','-u',unit,'--no-pager','--output=short-iso','-n','5000'],capture_output=True,text=True,timeout=10)
 (root/(unit+'.journal.txt')).write_text(r.stdout+r.stderr)
metadata={{'data_exists':(root/'data').exists(),'data_bytes':sum(p.stat().st_size for p in (root/'data').rglob('*') if p.is_file()) if (root/'data').exists() else 0}}
(root/'database-summary.json').write_text(json.dumps(metadata))
archive=root/'evidence.tar.gz'
with tarfile.open(archive,'w:gz') as tar:
 for p in root.iterdir():
  if p.name not in ['data','conf','evidence.tar.gz']:tar.add(p,arcname='node/'+p.name)
 if driver.exists():
  large={{}}
  for p in driver.iterdir():
   if p.name in ['signed-transactions.bin','transaction-hashes.jsonl']:
    digest=hashlib.sha256()
    with p.open('rb') as stream:
     for block in iter(lambda:stream.read(1048576),b''):digest.update(block)
    large[p.name]={{'bytes':p.stat().st_size,'sha256':digest.hexdigest(),'retained_path':str(p)}}
   else:tar.add(p,arcname='driver/'+p.name)
  (root/'retained-datasets.json').write_text(json.dumps(large,indent=2))
  tar.add(root/'retained-datasets.json',arcname='retained-datasets.json')
print(json.dumps({{'archive':str(archive),'bytes':archive.stat().st_size,**metadata}}))
'''
        meta = json.loads(self.Remote(host, script))
        if "archive" in meta:
            self.Download(host, meta["archive"], out / (host + "-evidence.tar.gz"))
        return {"host": host, **meta}

    def CleanupData(self, host, name, node_unit, driver_unit):
        run = "/var/tmp/fisco-experiments/" + name
        script = f'''import pathlib,subprocess,os,shutil,json
base=pathlib.Path('/var/tmp/fisco-experiments');root=base/{name!r};data=root/'data'
assert not base.is_symlink() and not root.is_symlink() and not data.is_symlink()
assert root.resolve().parent==base.resolve() and data.resolve().parent==root.resolve()
for unit in {[node_unit,driver_unit]!r}:
 r=subprocess.run(['systemctl','show',unit,'--property=ActiveState'],capture_output=True,text=True,timeout=5)
 assert not any('ActiveState='+state in r.stdout for state in ['active','activating','deactivating','reloading']),r.stdout
for p in pathlib.Path('/proc').glob('[0-9]*'):
 try:
  cwd=os.readlink(p/'cwd');args=(p/'cmdline').read_bytes().split(b'\\0')
 except OSError:continue
 assert not (cwd==str(root) or cwd.startswith(str(root)+'/')),'Process still using run directory'
 assert not (b'NativeAlignedBench' in args and any({name.encode()!r} in a for a in args)),'Driver still alive'
existed=data.exists()
if existed:shutil.rmtree(data)
print(json.dumps({{'path':str(data.resolve()),'existed':existed,'deleted':not data.exists()}}))
'''
        return json.loads(self.Remote(host, script))


def Quantile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def Summarize(points, observations, drivers, target_epoch):
    starts = [driver["start_epoch_ms"] / 1000 for driver in drivers if driver and "start_epoch_ms" in driver]
    window_start = max(starts) + 2 if len(starts) == 10 else target_epoch + 2
    window_end = min(starts) + 12 if len(starts) == 10 else target_epoch + 12
    measured = [point for point in points if point["bracket_begin_epoch"] >= window_start
                and point["bracket_end_epoch"] <= window_end]
    result = {"measurement_window_epoch": [window_start, window_end], "complete_sender_start_times": len(starts) == 10,
              "start_skew_ms": (max(starts) - min(starts)) * 1000 if len(starts) == 10 else None,
              "measurement_samples": len(measured), "balance_verification": "not_performed_in_short_budget",
              "tps_definition": "minimum validator committed counter; RPC request brackets retained; sensitivity interval is not a finality guarantee"}
    if len(measured) >= 2:
        first, last = measured[0], measured[-1]
        delta = last["count_min"] - first["count_min"]
        dt_min = last["bracket_begin_epoch"] - first["bracket_end_epoch"]
        dt_max = last["bracket_end_epoch"] - first["bracket_begin_epoch"]
        result.update(common_count_delta=delta, first_count_sample=first, last_count_sample=last,
                      tps_midpoint_estimate=delta / ((dt_min + dt_max) / 2),
                      denominator_bounds_seconds=[dt_min, dt_max],
                      tps_time_bracket_sensitivity=[delta / dt_max, delta / dt_min] if dt_min > 0 else None,
                      pending_mean_start=first["pending_mean"], pending_mean_end=last["pending_mean"])
    for key in ["requested", "submitted", "unsubmitted", "success", "failures", "unresolved", "actual_send_tps", "receipt_tps"]:
        result[key] = sum(driver.get(key, 0) for driver in drivers if driver)
    result["submitted_semantics"] = "SDK submission attempts, including calls that may fail; not node admission acknowledgements"
    result["receipt_success_fraction_of_attempts"] = result["success"] / result["submitted"] if result["submitted"] else None
    result["latency_success_samples"] = sum((driver or {}).get("latency_success_samples", 0) for driver in drivers)
    result["latency_is_success_only_and_censored"] = bool(result["unresolved"] or result["unsubmitted"] or result["failures"])
    result["short_measurement_only"] = True
    result["complete_driver_results"] = sum(driver is not None for driver in drivers)
    attributes = []
    for driver in drivers:
        if not driver:
            continue
        attribute = (driver.get("signed_transaction_inspection") or {}).get("attribute")
        attributes.append({"sender": driver.get("sender_host"), "attribute": attribute,
                           "dag_bit_set": bool(Number(attribute) & 4) if attribute is not None else None})
    result["serialized_transaction_attribute_samples"] = attributes
    result["all_sender_dag_bits_clear"] = len(attributes) == 10 and all(row["dag_bit_set"] is False for row in attributes)
    result["attribute_caveat"] = "SDK may retain EVM_ABI_CODEC bit 1; no DAG requires attribute & 4 == 0, not serialized attribute == 0"
    for field in ["latency_histogram_ms", "scheduled_latency_histogram_ms"]:
        histogram = {}
        for driver in drivers:
            for value, count in (driver or {}).get(field, {}).items():
                histogram[int(value)] = histogram.get(int(value), 0) + count
        result[field] = histogram
        def Percentile(fraction):
            needed = math.ceil(sum(histogram.values()) * fraction)
            count = 0
            for value in sorted(histogram):
                count += histogram[value]
                if count >= needed:
                    return value
            return None
        result[field + "_p50_ms"] = Percentile(.5)
        result[field + "_p99_ms"] = Percentile(.99)
    start_by_host = {driver["sender_host"]: driver["start_epoch_ms"] / 1000 for driver in drivers if driver and "start_epoch_ms" in driver}
    all_node_latencies = [(row["observed_epoch"] - (start_by_host.get(row["sender"], target_epoch)
                          + row["scheduled_offset_ms"] / 1000)) * 1000 for row in observations
                          if row["all_nodes_same_position"] and row["within_observation_budget"]]
    result.update(all_node_observation_samples=len(all_node_latencies),
                  all_node_observation_p50_ms=Quantile(all_node_latencies, .5),
                  all_node_observation_p99_ms=Quantile(all_node_latencies, .99),
                  all_node_observation_caveat="Low-frequency query observation includes polling/RPC time; not equivalent to PBFT finality time")
    return result


def OneCase(controller, rate, name, batch_out):
    out = batch_out / name
    out.mkdir(exist_ok=False)
    node_unit, driver_unit = "fisco-node-" + name, "fisco-driver-" + name
    prepared, launched = [], []
    points, observations, errors = [], [], []
    cleanup_ok = True
    users = [104, 104] + [102] * 8
    target_epoch = None
    pool = None
    snapshot_future = observe_future = None
    watchdogs = []
    watchdog_results = []
    node_runs = controller.hosts
    try:
        controller.block_cache.clear()
        controller.rpc_log = (out / "rpc.jsonl").open("w", buffering=1, encoding="utf-8")
        prepared = Parallel(lambda host: controller.PrepareNode(host, name, node_unit), controller.hosts)
        Save(out / "node-launches.json", prepared)
        before = controller.Ready(30)
        Save(out / "fresh-chain.json", before)
        if any(Number(row["transactionCount"]) != 0 for row in before["nodes"]):
            raise RuntimeError("Expected an empty fresh ledger")
        launched = Parallel(lambda pair: controller.LaunchDriver(pair[1], name, rate, users[pair[0]], driver_unit), list(enumerate(controller.hosts)))
        Save(out / "driver-launches.json", launched)
        deadline = time.monotonic() + 150
        while True:
            states = Parallel(lambda host: controller.DriverState(host, name, driver_unit), controller.hosts)
            if all(state["ready"] and state["samples"] for state in states):
                break
            if any("ActiveState=failed" in state["status"] for state in states) or time.monotonic() >= deadline:
                raise RuntimeError("Driver preparation failed or timed out: " + json.dumps(states))
            time.sleep(.5)
        plans = []
        for host in controller.hosts:
            for sample in json.loads(controller.ReadRemote(host, controller.driver / "runs" / name / "go.samples.json")):
                plans.append({**sample, "sender": host})
        selected = []
        groups = {}
        for sample in plans:
            groups.setdefault(round(sample["scheduled_offset_ms"]), []).append(sample)
        for index, offset in enumerate(sorted(groups)):
            samples = groups[offset]
            preferred = controller.hosts[index % 10]
            selected.append(next((sample for sample in samples if sample["sender"] == preferred), samples[0]))
        Save(out / "all-sample-plans.json", plans)
        Save(out / "selected-sample-plans.json", selected)
        target_epoch = int((time.time() + 3) * 1000) / 1000
        target_monotonic = time.monotonic() + target_epoch - time.time()
        cutoff = target_monotonic + 17
        def Watchdog(unit, label):
            started = {"phase": label, "callback_begin_epoch": time.time(), "callback_begin_monotonic": time.monotonic()}
            started["hosts"] = Parallel(lambda host: controller.WatchdogStopUnit(host, unit), controller.hosts)
            watchdog_results.append(started)
            Save(out / (label + ".json"), started)
        # Independent timers cannot be held up by snapshots, TLS calls, or SSH preparation.
        for at, unit, label in [(cutoff, node_unit, "node-deadline-stop"),
                                (cutoff + 1, driver_unit, "driver-deadline-stop")]:
            timer = threading.Timer(max(0, at - time.monotonic()), Watchdog, args=(unit, label))
            timer.daemon = True;timer.start();watchdogs.append(timer)
        def Gate(host):
            p = controller.driver / "runs" / name / "go"
            return controller.Remote(host, f"import pathlib,os\np=pathlib.Path({str(p)!r});t=p.with_suffix('.tmp');t.write_text({str(int(target_epoch*1000))!r});os.replace(t,p)\n")
        Parallel(Gate, controller.hosts)
        Save(out / "timing.json", {"gate_epoch": target_epoch, "gate_monotonic": target_monotonic,
                                   "send_deadline_epoch": target_epoch + 12, "observation_deadline_epoch": target_epoch + 17,
                                   "write_grace_seconds": 1, "warmup_seconds": 2, "measure_seconds": 10})
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        waiting, confirmed = list(selected), set()
        next_snapshot, next_observe = target_monotonic, target_monotonic
        active_sample = None
        timeline = (out / "chain.jsonl").open("w", buffering=1, encoding="utf-8")
        observation_log = (out / "observations.jsonl").open("w", buffering=1, encoding="utf-8")
        try:
            while time.monotonic() < cutoff:
                now = time.monotonic()
                if snapshot_future and snapshot_future.done():
                    try:
                        point = snapshot_future.result();points.append(point)
                        timeline.write(json.dumps(point) + "\n")
                    except Exception as error:errors.append({"phase": "snapshot", "error": repr(error)})
                    snapshot_future = None
                if observe_future and observe_future.done():
                    try:
                        observation = observe_future.result();observation_log.write(json.dumps(observation) + "\n")
                        if observation["all_nodes_same_position"] and observation["within_observation_budget"]:
                            observations.append(observation);confirmed.add(active_sample["hash"])
                    except Exception as error:errors.append({"phase": "sample", "hash": active_sample["hash"], "error": repr(error)})
                    observe_future = None
                if snapshot_future is None and now >= next_snapshot and now >= target_monotonic:
                    snapshot_future = pool.submit(controller.SnapshotTimed, cutoff)
                    next_snapshot = now + 1
                if observe_future is None and now >= next_observe:
                    due = [sample for sample in waiting if sample["hash"] not in confirmed
                           and target_monotonic + sample["scheduled_offset_ms"] / 1000 <= now]
                    if due:
                        active_sample = due[0]
                        waiting.remove(active_sample);waiting.append(active_sample)
                        observe_future = pool.submit(controller.Observe, active_sample, cutoff)
                    next_observe = now + .2
                time.sleep(min(.02, max(0, cutoff - time.monotonic())))
        finally:
            timeline.close();observation_log.close()
            for future in [snapshot_future, observe_future]:
                if future:future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
        # Independent watchdog starts node shutdown at 17 s and driver shutdown at 18 s.
        # The additional second is file writing only, never an observation extension.
        remaining = cutoff + 1 - time.monotonic()
        if remaining > 0:time.sleep(remaining)
    except BaseException as error:
        errors.append({"phase": "case", "error": repr(error)})
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        for timer in watchdogs:
            if timer.is_alive() and time.monotonic() < (target_monotonic + 17 if target_epoch is not None else 0):
                timer.cancel()
        def Finish(host):
            row = {"host": host}
            try:
                proof = controller.StopOwned(host, name, node_unit, driver_unit)
                row["stop"] = proof
                row["evidence"] = controller.Evidence(host, name, node_unit, driver_unit, out)
                # Do not erase a database unless its evidence was successfully downloaded.
                row["cleanup"] = controller.CleanupData(host, name, node_unit, driver_unit)
            except Exception as error:row["error"] = repr(error)
            return row
        cleanup = Parallel(Finish, node_runs)
        for timer in watchdogs:
            timer.join(timeout=15)
        active = [future for future in [snapshot_future, observe_future] if future and not future.done()]
        if active:
            _, unfinished = concurrent.futures.wait(active, timeout=3)
            if unfinished:
                errors.append({"phase": "observer-stop", "error": "RPC observer still active after node stop"})
        Save(out / "watchdog-stops.json", watchdog_results)
        Save(out / "cleanup.json", cleanup)
        cleanup_ok = all("error" not in row for row in cleanup)
        with controller.rpc_lock:
            if controller.rpc_log is not None:
                controller.rpc_log.close();controller.rpc_log = None
        drivers = []
        for host in controller.hosts:
            try:
                driver = json.loads(controller.ReadRemote(host, controller.driver / "runs" / name / "driver.json"))
                driver["sender_host"] = host
                Save(out / (host + "-driver.json"), driver)
                drivers.append(driver)
            except Exception as error:
                drivers.append(None);errors.append({"phase": "driver-result", "host": host, "error": repr(error)})
        summary = Summarize(points, observations, drivers, target_epoch) if target_epoch is not None else {}
        summary.update(name=name, target_tps=rate, nodes=10, mode="solidity", users_per_sender=users,
                       total_users=sum(users), node_parameters="official generated defaults; copied byte for byte",
                       runtime_policy="12 s send deadline; 5 s further observation; node stop requested independently at 17 s; driver stop requested at 18 s; exact transient units verified",
                       requested_observation_samples=20, completed_observation_samples=len(observations),
                       unresolved_observation_samples=20-len(observations),
                       cleanup_ok=cleanup_ok, errors=errors,
                       workload_caveat="Functional native-Chain versus Solidity-FISCO transfer comparison; not isolated consensus or same-EVM comparison")
        summary["measurement_valid"] = bool(summary.get("complete_driver_results") == 10
                    and summary.get("all_sender_dag_bits_clear")
                    and summary.get("measurement_samples", 0) >= 2
                    and not any(error["phase"] in ["case", "observer-stop"] for error in errors))
        summary["measurement_status"] = "complete" if summary["measurement_valid"] else "incomplete"
        summary["performance_success_not_implied_by_cleanup"] = True
        summary["all_node_observation_success_fraction"] = len(observations) / 20
        if points:
            last_nodes = points[-1]["nodes"]
            summary["last_complete_observed_snapshot"] = points[-1]
            summary["last_observed_state_agrees_and_drained"] = bool(
                len({row["height"] for row in last_nodes}) == 1
                and len({row["count"] for row in last_nodes}) == 1
                and len({row["head_hash"] for row in last_nodes}) == 1
                and len({row["state_root"] for row in last_nodes}) == 1
                and all(row["pending"] == 0 and row["state_root"] is not None for row in last_nodes))
        Save(out / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return cleanup_ok and not any(error["phase"] == "case" for error in errors)


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/opt/fisco-control")
    parser.add_argument("--hosts", default=str(Path(__file__).with_name("hosts.json")))
    parser.add_argument("--generated", default="default-v3.7.3")
    parser.add_argument("--key", default="/root/.ssh/chain_ansible_ed25519")
    parser.add_argument("--known-hosts", default="/opt/chain-control/known_hosts")
    parser.add_argument("--inflight", type=int, default=5000)
    parser.add_argument("--name", default="default-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--rates", nargs="+", type=int, default=[20000, 30000, 40000, 50000, 60000])
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,50}", args.name):
        raise ValueError("Use a safe unique batch name of at most 51 characters")
    if args.rates != [20000, 30000, 40000, 50000, 60000]:
        raise ValueError("This script is restricted to the authorized five rates")
    controller = Formal(args)
    out = controller.root / "results" / args.name
    out.mkdir(parents=True, exist_ok=False)
    def Interrupt(signum, frame):raise KeyboardInterrupt("Received signal " + str(signum))
    signal.signal(signal.SIGTERM, Interrupt)
    signal.signal(signal.SIGINT, Interrupt)
    try:
        Save(out / "defaults-stopped.json", controller.SetDefaults("stop"))
        for rate in args.rates:
            if not OneCase(controller, rate, args.name + "-r" + str(rate), out):
                break
    finally:
        try:Save(out / "defaults-restored.json", controller.SetDefaults("start"))
        except Exception as error:
            Save(out / "restore-error.json", {"error": repr(error)})
            raise


if __name__ == "__main__":
    Main()
