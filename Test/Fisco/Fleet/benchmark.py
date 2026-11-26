#!/usr/bin/env python3
"""Benchmark already running FISCO nodes; never starts/stops remote processes.

python3 benchmark.py --hosts hosts.json --rate 8000 --output results/pbft100-8000
Uses one prepared Java sender, four SDK ingress peers, and persistent RPC sessions.
"""
import argparse
import concurrent.futures as cf
import gzip
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import signal
import ssl
import statistics
import struct
import subprocess
import threading
import time
import traceback


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def number(value):
    return int(value, 0) if isinstance(value, str) and value.startswith("0x") else int(value)


def percentile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            value.update(part)
    return value.hexdigest()


class Rpc:
    def __init__(self, hosts, port, cert, workers, output, timeout=3, scheme="https"):
        self.hosts, self.port, self.timeout, self.scheme = hosts, port, timeout, scheme
        self.context = ssl.create_default_context(cafile=str(cert / "ca.crt"))
        self.context.check_hostname = False
        self.context.load_cert_chain(str(cert / "sdk.crt"), str(cert / "sdk.key"))
        self.connections = {}
        self.locks = {host: threading.Lock() for host in hosts}
        self.pool = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fisco-rpc")
        self.log_lock = threading.Lock()
        self.log = gzip.open(output / "rpc.jsonl.gz", "wt", encoding="utf-8", compresslevel=1)

    def call(self, host, method, *params, deadline=None):
        begin = time.monotonic()
        record = {"host": host, "method": method, "params": params,
                  "begin_monotonic": begin, "begin_epoch": time.time()}
        acquired = False
        try:
            timeout = self.timeout if deadline is None else min(self.timeout, deadline - begin)
            if timeout <= 0 or not self.locks[host].acquire(timeout=timeout):
                raise TimeoutError("RPC observation deadline")
            acquired = True
            timeout = self.timeout if deadline is None else min(self.timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("RPC observation deadline")
            connection = self.connections.get(host)
            if connection is None:
                cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
                options = {"context": self.context} if self.scheme == "https" else {}
                connection = cls(host, self.port, timeout=timeout, **options)
                self.connections[host] = connection
            connection.timeout = timeout
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            body = json.dumps({"jsonrpc": "2.0", "method": method, "params": ["group0", "", *params], "id": 1})
            connection.request("POST", "/", body, {"Content-Type": "application/json", "Connection": "keep-alive"})
            response = connection.getresponse()
            value = json.loads(response.read())
            if response.status != 200 or "error" in value:
                raise RuntimeError(str(value.get("error", response.status)))
            record["result"] = value["result"]
            return record
        except Exception as error:
            record["error"] = str(error)
            if acquired:
                connection = self.connections.pop(host, None)
                if connection:
                    connection.close()
            raise
        finally:
            record.update(end_monotonic=time.monotonic(), end_epoch=time.time())
            if acquired:
                self.locks[host].release()
            with self.log_lock:
                self.log.write(json.dumps(record) + "\n")

    def all(self, function):
        futures = [self.pool.submit(function, host) for host in self.hosts]
        result = []
        for host, future in zip(self.hosts, futures):
            try:
                result.append(future.result())
            except Exception as error:
                result.append({"host": host, "error": str(error)})
        return result

    def snapshot(self, deadline=None, headers=False):
        def one(host):
            value = self.call(host, "getTotalTransactionCount", deadline=deadline)
            count = value["result"]
            row = {"host": host, "height": number(count["blockNumber"]),
                   "count": number(count["transactionCount"]), "failed": number(count["failedTransactionCount"]),
                   "begin_monotonic": value["begin_monotonic"], "end_monotonic": value["end_monotonic"]}
            row["pending"] = number(self.call(host, "getPendingTxSize", deadline=deadline)["result"])
            if headers:
                block = self.call(host, "getBlockByNumber", row["height"], True, True, deadline=deadline)["result"]
                row.update(hash=block["hash"], state_root=block.get("stateRoot"))
            return row
        rows = self.all(one)
        complete = all("error" not in row for row in rows)
        valid = [row for row in rows if "count" in row]
        return {"nodes": rows, "complete": complete,
                "count_min": min(row["count"] for row in valid) if complete else None,
                "count_max": max(row["count"] for row in valid) if complete else None,
                "bracket_begin_monotonic": min(row["begin_monotonic"] for row in valid) if valid else None,
                "bracket_end_monotonic": max(row["end_monotonic"] for row in valid) if valid else None}

    def heights(self, deadline):
        def one(host):
            row = self.call(host, "getBlockNumber", deadline=deadline)
            return {"host": host, "height": number(row["result"]), "end_monotonic": row["end_monotonic"]}
        rows = self.all(one)
        complete = all("error" not in row for row in rows)
        return {"nodes": rows, "complete": complete,
                "minimum_height": min(row["height"] for row in rows) if complete else None,
                "end_monotonic": max((row.get("end_monotonic", 0) for row in rows), default=0)}

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        for connection in self.connections.values():
            connection.close()
        self.log.close()


def sdk_config(path, cert, peers, port, account):
    text = ("[cryptoMaterial]\ncertPath = " + json.dumps(str(cert)) +
            '\ndisableSsl = "false"\nuseSMCrypto = "false"\n[network]\npeers = ' +
            json.dumps([host + ":" + str(port) for host in peers]) +
            '\nsendRpcRequestToHighestBlockNode = "false"\nmessageTimeout = "120000"\n' +
            "[account]\nkeyStoreDir = " + json.dumps(str(account)) +
            '\n[threadPool]\nthreadPoolSize = "4"\n')
    path.write_text(text, encoding="utf-8")


def stop_driver(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except ProcessLookupError:
        pass


def boundaries(first, last):
    if not first or not last or not first.get("complete") or not last.get("complete"):
        return {"tps": None, "measurement_seconds": None, "committed_delta": None}
    a = (first["bracket_begin_monotonic"] + first["bracket_end_monotonic"]) / 2
    b = (last["bracket_begin_monotonic"] + last["bracket_end_monotonic"]) / 2
    delta = last["count_min"] - first["count_min"]
    return {"tps": delta / (b - a) if b > a else None,
            "measurement_seconds": b - a, "committed_delta": delta,
            "endpoint_span_limit_passed": max(first["bracket_end_monotonic"] - first["bracket_begin_monotonic"],
                                                last["bracket_end_monotonic"] - last["bracket_begin_monotonic"]) <= .5,
            "endpoint_spans_seconds": [first["bracket_end_monotonic"] - first["bracket_begin_monotonic"],
                                       last["bracket_end_monotonic"] - last["bracket_begin_monotonic"]]}


def expected_balances(sent_offsets, users, initial=1_000_000_000):
    if len(sent_offsets) % 8:
        raise ValueError("Invalid sent-offset-ns.bin length")
    balances = [initial] * users
    count = 0
    for index, (offset,) in enumerate(struct.iter_unpack(">q", sent_offsets)):
        if offset >= 0:
            balances[index % users] -= 1
            balances[(index + 1) % users] += 1
            count += 1
    return balances, count


def run(args):
    root, output = Path(args.root).resolve(), Path(args.output).resolve()
    hosts = load(args.hosts)
    if not isinstance(hosts, list) or not hosts or any(not isinstance(host, str) for host in hosts) or len(set(hosts)) != len(hosts):
        raise ValueError("--hosts must contain unique IP strings")
    if args.drain != 5:
        raise ValueError("Existing Java short-budget has a fixed 5-second drain")
    if (args.rate <= 0 or args.warmup < 0 or args.seconds <= 0 or args.users <= 0 or args.users % 2 or
            args.sample_hz < 1 or args.ingress < 1 or args.head_poll <= 0 or args.query_workers < 1):
        raise ValueError("Invalid workload parameters")
    output.mkdir(parents=True, exist_ok=False)
    cert = Path(args.cert_dir).resolve() if args.cert_dir else root / "generated" / hosts[0] / "sdk"
    peers = hosts[:min(args.ingress, len(hosts))]
    config = output / "sdk-config.toml"
    sdk_config(config, cert, peers, args.rpc_port, output / "account")
    java = Path(args.java).resolve() if args.java else root / "toolchain/jdk/bin/java"
    base_java = [str(java), "-Xms64m", "-Xmx1g", "-XX:ActiveProcessorCount=2",
                 "-Dlog4j.configurationFile=" + str(root / "driver/sdk-log4j2.xml"),
                 "-cp", str(root / "driver/classes") + os.pathsep + str(root / "driver/lib/*"), "NativeAlignedBench"]
    gate = output / "go"
    driver_path = output / "driver.json"
    command = base_java + [str(config), "native", str(args.rate), str(args.warmup), str(args.seconds),
                           str(driver_path), str(args.users), str(gate), str(args.inflight), "short-budget"]
    environment = os.environ.copy()
    environment.update(FISCO_BENCH_SHARDS="1", FISCO_BENCH_SHARD="0", FISCO_BENCH_SAMPLE_HZ=str(args.sample_hz),
                       FISCO_BENCH_PREFIX="bench-" + output.name)
    summary = {"status": "failed", "ok": False, "nodes": len(hosts), "ingress_peers": peers,
               "target_tps": args.rate, "warmup_seconds": args.warmup, "requested_measurement_seconds": args.seconds,
               "drain_seconds": args.drain, "users": args.users, "amount": 1, "routing": "ring",
               "sample_hz": args.sample_hz, "head_poll_seconds": args.head_poll, "errors": []}
    save(output / "protocol.json", {**summary, "java_command": command, "rpc_port": args.rpc_port,
         "signature_preparation": "before gate", "metrics": "100-node minimum committed count; scheduled send to common block height",
         "java_source_sha256": digest(root / "driver/NativeAlignedBench.java"),
         "java_class_sha256": digest(root / "driver/classes/NativeAlignedBench.class")})
    process, rpc = None, None
    jobs = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="fisco-jobs")
    receipt_pool = cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="fisco-samples")
    try:
        rpc = Rpc(hosts, args.rpc_port, cert, args.query_workers, output, args.rpc_timeout, args.rpc_scheme)
        fresh = rpc.snapshot(headers=True)
        save(output / "fresh.json", fresh)
        if not fresh["complete"] or any(row["pending"] for row in fresh["nodes"]):
            raise RuntimeError("Nodes are not ready before setup")
        log = (output / "driver.log").open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(command, cwd=root / "driver", env=environment,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            prepare_deadline = time.monotonic() + args.setup_timeout
            while not gate.with_suffix(".ready").exists():
                if process.poll() is not None:
                    raise RuntimeError("Driver exited during preparation: " + str(process.returncode))
                if time.monotonic() >= prepare_deadline:
                    raise TimeoutError("Driver preparation timed out")
                time.sleep(.1)
            while True:
                setup = rpc.snapshot(headers=True)
                if (setup["complete"] and len({row["count"] for row in setup["nodes"]}) == 1 and
                        len({row["hash"] for row in setup["nodes"]}) == 1 and
                        all(row["pending"] == 0 for row in setup["nodes"])):
                    break
                if time.monotonic() >= prepare_deadline:
                    raise TimeoutError("Funding has not reached all nodes")
                time.sleep(.2)
            save(output / "setup.json", setup)
            sample_plan = load(str(gate) + ".samples.json")
            save(output / "sample-plan.json", sample_plan)
            gate_epoch = round((time.time() + 2) * 1000) / 1000
            start = time.monotonic() + gate_epoch - time.time()
            deadline, send_end = start + args.warmup + args.seconds + args.drain, start + args.warmup + args.seconds
            save(output / "timing.json", {"gate_epoch": gate_epoch, "gate_monotonic": start,
                  "measurement_begin_monotonic": start + args.warmup, "measurement_end_monotonic": send_end,
                  "observation_end_monotonic": deadline})
            gate.with_suffix(".tmp").write_text(str(round(gate_epoch * 1000)), encoding="utf-8")
            gate.with_suffix(".tmp").replace(gate)
            plans = {row["hash"].lower(): row for row in sample_plan}
            attempted, completed, receipt_known, pending_receipts, next_receipt_poll = set(), {}, {}, {}, {}
            sent_path, sent_position, sent_fragment = output / "sent-samples.jsonl", 0, ""
            snapshots, snapshot_pending = [], {}
            height_pending, latest_heights, next_head = None, None, start
            next_stats, boundary_targets = start, {start + args.warmup: "begin", send_end: "end"}
            endpoint_futures, endpoints = {}, {}
            with gzip.open(output / "chain.jsonl.gz", "wt", encoding="utf-8", compresslevel=1) as chain_log, \
                    gzip.open(output / "heights.jsonl.gz", "wt", encoding="utf-8", compresslevel=1) as height_log, \
                    (output / "confirmation-samples.jsonl").open("w", encoding="utf-8", buffering=1) as sample_log:
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    for target, name in list(boundary_targets.items()):
                        if now >= target:
                            endpoint_futures[jobs.submit(rpc.snapshot, deadline, True)] = name
                            del boundary_targets[target]
                    if now >= next_stats and not snapshot_pending:
                        snapshot_pending[jobs.submit(rpc.snapshot, deadline, False)] = now - start
                        next_stats = now + 1
                    if now >= next_head and height_pending is None:
                        height_pending = jobs.submit(rpc.heights, deadline)
                        next_head = now + args.head_poll
                    for future, target in list(snapshot_pending.items()):
                        if future.done():
                            point = future.result(); point["seconds"] = target
                            snapshots.append(point); chain_log.write(json.dumps(point) + "\n")
                            del snapshot_pending[future]
                    for future, name in list(endpoint_futures.items()):
                        if future.done():
                            endpoints[name] = future.result()
                            save(output / ("endpoint-" + name + ".json"), endpoints[name])
                            del endpoint_futures[future]
                    if height_pending is not None and height_pending.done():
                        latest_heights = height_pending.result()
                        latest_heights["seconds"] = latest_heights["end_monotonic"] - start
                        height_log.write(json.dumps(latest_heights) + "\n")
                        height_pending = None
                    if sent_path.exists():
                        with sent_path.open(encoding="utf-8") as source:
                            source.seek(sent_position); text = sent_fragment + source.read(); sent_position = source.tell()
                        parts = text.split("\n"); sent_fragment = parts.pop()
                        for line in parts:
                            if line.strip():
                                attempted.add(json.loads(line)["hash"].lower())
                    for future, txhash in list(pending_receipts.items()):
                        if not future.done():
                            continue
                        del pending_receipts[future]
                        next_receipt_poll[txhash] = time.monotonic() + args.head_poll
                        try:
                            record = future.result()
                            receipt = record["result"]
                            if receipt is not None and number(receipt.get("status", -1)) == 0 and int(receipt.get("output", "-1"), 16) == 0:
                                receipt_known[txhash] = {"height": number(receipt["blockNumber"]),
                                    "receipt_end_monotonic": record["end_monotonic"], "receipt": receipt}
                        except Exception as error:
                            summary["errors"].append({"phase": "sample_receipt", "hash": txhash, "error": str(error)})
                    for txhash, known in list(receipt_known.items()):
                        if (txhash not in completed and latest_heights and latest_heights["complete"] and
                                latest_heights["minimum_height"] >= known["height"]):
                            observed = max(known["receipt_end_monotonic"], latest_heights["end_monotonic"])
                            if observed <= deadline:
                                plan = plans[txhash]
                                row = {**plan, "height": known["height"], "observed_seconds": observed - start,
                                       "confirmation_ms": (observed - start) * 1000 - plan["scheduled_offset_ms"]}
                                completed[txhash] = row
                                sample_log.write(json.dumps(row) + "\n")
                    active_hashes = set(pending_receipts.values())
                    available = [txhash for txhash in attempted if txhash in plans and txhash not in receipt_known and
                                 txhash not in active_hashes and now >= next_receipt_poll.get(txhash, 0)]
                    for txhash in sorted(available, key=lambda key: plans[key]["index"])[:max(0, 16 - len(pending_receipts))]:
                        peer = peers[int(txhash.removeprefix("0x")[:8], 16) % len(peers)]
                        pending_receipts[receipt_pool.submit(rpc.call, peer, "getTransactionReceipt", txhash, False, deadline=deadline)] = txhash
                    if process.poll() is not None and process.returncode != 0:
                        raise RuntimeError("Driver failed during measurement: " + str(process.returncode))
                    time.sleep(.005)
            for future, name in endpoint_futures.items():
                endpoints[name] = future.result()
                save(output / ("endpoint-" + name + ".json"), endpoints[name])
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                stop_driver(process)
            summary["driver_exit_code"] = process.returncode
        finally:
            stop_driver(process)
            log.close()
        driver = load(driver_path) if driver_path.exists() else {}
        summary.update(boundaries(endpoints.get("begin"), endpoints.get("end")))
        summary.update(actual_send_tps=driver.get("actual_send_tps"), submitted=driver.get("submitted"),
                       callback_success=driver.get("success"), callback_errors=driver.get("failures"),
                       callback_unresolved=driver.get("unresolved"), unsubmitted=driver.get("unsubmitted"),
                       driver_errors=driver.get("errors"), receipt_p50_ms=driver.get("p50_ms"),
                       receipt_p95_ms=driver.get("p95_ms"), receipt_p99_ms=driver.get("p99_ms"),
                       receipt_success_samples=driver.get("latency_success_samples"))
        latencies = [row["confirmation_ms"] for row in completed.values()]
        summary.update(p50_ms=percentile(latencies, .5), p95_ms=percentile(latencies, .95), p99_ms=percentile(latencies, .99),
                       planned_samples=len(plans), confirmation_samples=len(completed),
                       attempted_samples=len(attempted & plans.keys()), unresolved_samples=len(plans) - len(completed),
                       within_one_second_fraction=sum(value <= 1000 for value in latencies) / len(plans) if plans else None)
        final = rpc.snapshot(headers=True)
        save(output / "final.json", final)
        summary["final_count_min"] = final["count_min"]
        summary["transactions_received_on_all_nodes"] = final["count_min"] - setup["count_min"] if final["complete"] else None
        summary["drained"] = (final["complete"] and driver.get("submitted") is not None and
                              all(row["count"] - setup["count_min"] == driver["submitted"] and row["pending"] == 0
                                  for row in final["nodes"]))
        final_height = min((row["height"] for row in final["nodes"] if "height" in row), default=None)
        def anchor(host):
            block = rpc.call(host, "getBlockByNumber", final_height, True, True)["result"]
            return {"host": host, "height": final_height, "hash": block["hash"], "state_root": block.get("stateRoot")}
        anchors = rpc.all(anchor) if final_height is not None else []
        save(output / "same-height-state.json", anchors)
        state_ok = (len(anchors) == len(hosts) and all("error" not in row and row.get("state_root") for row in anchors) and
                    len({row.get("hash") for row in anchors}) == 1 and len({row.get("state_root") for row in anchors}) == 1)
        summary.update(state_nodes_checked=len([row for row in anchors if "error" not in row]),
                       same_height_state_passed=bool(state_ok), final_common_height=final_height,
                       all_final_heights_equal=bool(final["complete"] and len({row["height"] for row in final["nodes"]}) == 1))
        if final_height is not None and completed:
            summary["confirmed_samples_within_audited_height"] = sum(row["height"] <= final_height for row in completed.values())
        # Balance queries are read-only and occur after the fixed measurement/observation periods.
        audit_command = base_java + [str(config), "balances", driver.get("prefix", ""), str(args.users), str(output / "balances.json")]
        audit = subprocess.run(audit_command, cwd=root / "driver", env=environment, capture_output=True, text=True, timeout=120)
        (output / "balance-audit.log").write_text(audit.stdout + audit.stderr, encoding="utf-8")
        balances = load(output / "balances.json") if (output / "balances.json").exists() else {}
        sent_file = output / "sent-offset-ns.bin"
        expected, submitted = expected_balances(sent_file.read_bytes(), args.users) if sent_file.exists() else (None, None)
        eligible = (summary["drained"] and state_ok and driver.get("failures") == 0 and driver.get("unresolved") == 0 and
                    submitted == driver.get("submitted") and audit.returncode == 0 and balances.get("complete") and
                    balances.get("errors") == 0)
        actual = [int(value) if value is not None else None for value in balances.get("balances", [])]
        mismatch = sum(value != expected[index] for index, value in enumerate(actual)) if eligible and len(actual) == args.users else None
        summary.update(balance_accounts_checked=len(actual), balance_mismatches=mismatch,
                       balance_audit_passed=bool(eligible and mismatch == 0),
                       balance_audit_basis="all submitted transfers" if eligible else "raw balances retained; transfers not all verified")
        if expected is not None:
            save(output / "expected-balances.json", {"submitted": submitted, "balances": expected})
        summary["ok"] = bool(summary["drained"] and state_ok and summary["balance_audit_passed"] and
                             summary.get("endpoint_span_limit_passed") and not summary["unresolved_samples"])
        summary["status"] = "complete" if summary["ok"] else "incomplete"
    except Exception as error:
        summary["errors"].append({"phase": "run", "error": str(error), "trace": traceback.format_exc()})
    finally:
        stop_driver(process)
        receipt_pool.shutdown(wait=True, cancel_futures=True)
        jobs.shutdown(wait=True, cancel_futures=True)
        if rpc:
            rpc.close()
        save(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/opt/fisco-fleet")
    parser.add_argument("--hosts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rate", required=True, type=int)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--drain", type=int, default=5)
    parser.add_argument("--users", type=int, default=1024)
    parser.add_argument("--ingress", type=int, default=4)
    parser.add_argument("--inflight", type=int, default=50000)
    parser.add_argument("--sample-hz", type=int, default=20)
    parser.add_argument("--head-poll", type=float, default=.05)
    parser.add_argument("--query-workers", type=int, default=32)
    parser.add_argument("--rpc-port", type=int, default=20200)
    parser.add_argument("--rpc-timeout", type=float, default=3)
    parser.add_argument("--rpc-scheme", choices=["https", "http"], default="https")
    parser.add_argument("--cert-dir")
    parser.add_argument("--java")
    parser.add_argument("--setup-timeout", type=float, default=300)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps({key: result.get(key) for key in ("status", "nodes", "target_tps", "tps", "actual_send_tps", "p50_ms", "p95_ms", "drained", "state_nodes_checked", "balance_audit_passed")}), flush=True)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
