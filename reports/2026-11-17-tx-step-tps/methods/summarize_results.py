#!/usr/bin/env python3
"""Summarize the fixed 12-case, 20-node block-step experiment offline.

Only named evidence JSON files are read; inventories, credentials and logs are
never copied. Outputs use a field allowlist, aggregate node information and
categorize errors without retaining their arbitrary text. A benchmark failure
is data, not a script error. Exit 2 means evidence validation found an inconsistency.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import statistics
import sys


STEPS = (0, 500, 1000, 2000)
RATES = (20000, 30000)
SCHEDULE = [("throughput", rate, step) for rate in RATES for step in STEPS]
SCHEDULE += [("latency", 20000, step) for step in STEPS]
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
METRICS = (
    "block_tx_step", "local_proposals", "block_step_proposals", "block_tail_proposals",
    "block_step_wait_ms", "reorganizations", "max_reorg_depth", "fork_reexecute_transactions",
    "branch_append_transactions", "replayed_transactions", "committed", "chain_transactions",
    "candidate_executions", "candidate_rank_skips", "execution_cache_hits", "execution_cache_misses",
    "block_tasks", "block_queue_wait_ms", "metadata_tasks", "metadata_queue_wait_ms", "db_writes",
    "db_write_ms", "db_batch_bytes", "network_bytes", "network_sent_bytes", "missing_transactions",
    "process_cpu_seconds", "process_write_bytes", "peak_working_set_bytes", "signature_checks",
    "signature_cache_hits", "candidate_execute_ms", "commit_execute_ms", "block_verify_ms",
    "pool_refresh_ms", "peak_pending", "elapsed_seconds",
)
EXPECTED_SETTINGS = {
    "io-threads": 4, "verify-threads": 8, "sync": 1, "produce": 1,
    "experiment-index": 1, "clock-sync": 1, "sync-period-ms": 100,
    "sync-gain": .25, "round-ms": 50, "candidate-ms": 12,
    "relay-transactions": 0, "block-ms": 50, "max-block-txs": 6000,
    "run-seconds": 300, "clock-offset-ms": 0, "clock-drift-ppm": 0,
    "port": 18089, "max-peers": 64, "max-connections": 128,
}


def Require(condition, message):
    if not condition:
        raise ValueError(message)


def Number(value):
    return type(value) in (int, float) and math.isfinite(value)


def Close(a, b):
    return Number(a) and Number(b) and math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-8)


def Sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def Read(path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def Write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def Dict(value):
    return value if isinstance(value, dict) else {}


def ErrorSummary(value):
    """Never emit free-form exception messages, addresses, paths or log fragments."""
    if not value:
        return {"present": False, "categories": [], "sha256": None}
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True)
    lower = raw.lower()
    categories = [name for pattern, name in (
        ("timeout", "timeout"), ("timed out", "timeout"), ("connection refused", "connection_refused"),
        ("connection reset", "connection_reset"), ("missing exact measurement endpoint", "missing_endpoint"),
        ("persisted canonical proof failed", "canonical_proof_failed"),
        ("final state audit", "state_audit_failed"), ("acquisition span", "endpoint_span_failed"),
        ("target-to-target", "endpoint_window_failed"), ("not found", "dependency_or_file_missing"),
        ("interrupted", "interrupted"), ("cleanup", "cleanup_failed"),
        ("start failed", "start_failed"), ("stop failed", "stop_failed"),
        ("state digest", "state_digest_mismatch"), ("balance mismatch", "account_balance_mismatch"),
    ) if pattern in lower]
    return {"present": True, "categories": sorted(set(categories)) or ["unclassified_redacted"],
            "sha256": hashlib.sha256(raw.encode()).hexdigest()}


def Scalar(source, key, errors, kind="number"):
    value = source.get(key)
    if value is None:
        return None
    good = Number(value) if kind == "number" else type(value) is bool
    if not good:
        errors.append("invalid_type_" + key)
        return None
    return value


def Summary(values):
    observed = [value for value in values if Number(value)]
    return {"observed_nodes": len(observed), "expected_nodes": 20, "complete": len(observed) == 20,
            "median": statistics.median(observed) if observed else None,
            "min": min(observed) if observed else None, "max": max(observed) if observed else None,
            "sum": sum(observed) if observed else None}


def Ratio(s, numerator, denominator):
    a, b = s.get(numerator), s.get(denominator)
    return a / b if Number(a) and Number(b) and b > 0 else None


def FindRoot(root):
    root = root.resolve(strict=True)
    if root.name == "benchmark" and (root / "results.json").is_file():
        return root.parent
    if (root / "benchmark/results.json").is_file() or (root / "comparison-result.json").is_file():
        return root
    candidates = sorted({path.parent.parent for path in root.rglob("benchmark/results.json")})
    Require(len(candidates) == 1, "Select one experiment run directory; exactly one benchmark/results.json is required")
    return candidates[0]


def CaseKey(row):
    case = Dict(row.get("case"))
    return case.get("group"), case.get("rate"), row.get("block_tx_step", case.get("block_tx_step"))


def ArgOptions(command):
    Require(isinstance(command, list) and command and all(isinstance(x, str) for x in command), "Invalid process command")
    Require(len(command) % 2 == 1, "Process command must consist of executable and option/value pairs")
    result = {}
    for option, value in zip(command[1::2], command[2::2]):
        Require(option.startswith("--"), "Invalid process option")
        result.setdefault(option[2:], []).append(value)
    return result


def CaseEvidence(directory, step, expected_binary, expected_run, errors):
    if directory is None:
        return ({"load_snapshots": 0, "complete": False, "statistics": {}},
                {"proven": False, "actual_step_matches_nodes": 0, "binary_matches_nodes": 0,
                 "argv_matches_nodes": 0, "expected_nodes": 20},
                {"expected_databases": 20, "records": 0, "removed": 0, "paths_verified": 0, "proved": False})
    machines = Dict(Read(directory / "machines.json", {}))
    loaded, seen, argv_counts, binary_counts, step_counts = [], set(), 0, 0, 0
    source_problems, observed_settings = [], {}
    for path in sorted(directory.glob("*-load-stop.json")):
        name = path.name.removesuffix("-load-stop.json")
        if name in seen:
            errors.append("duplicate_load_snapshot")
        seen.add(name)
        value = Dict(Read(path))
        stats = Dict(value.get("final.json"))
        if not stats:
            source_problems.append("load_final_missing")
            continue
        loaded.append(stats)
        if type(stats.get("block_tx_step")) is int and stats["block_tx_step"] == step:
            step_counts += 1
        else:
            errors.append("load_final_step_mismatch")
        metadata, process = Dict(machines.get(name)), Dict(value.get("process.json"))
        binary_good = bool(SHA.fullmatch(str(expected_binary)) and metadata.get("binary_sha256") == expected_binary
                           and metadata.get("binary") and metadata.get("binary") == process.get("binary"))
        binary_counts += int(binary_good)
        if not binary_good:
            source_problems.append("machine_binary_proof_missing_or_different")
        try:
            command = process.get("command")
            options = ArgOptions(command)
            actual = {}
            wanted = {**EXPECTED_SETTINGS, "block-tx-step": step}
            for key, target in wanted.items():
                values = options.get(key, [])
                Require(len(values) == 1, "Required numeric setting missing or duplicated")
                number = float(values[0])
                Require(math.isfinite(number) and number == target, "Actual numeric setting mismatch")
                actual[key] = int(number) if number.is_integer() else number
            Require(command[0] == process.get("binary"), "Executable identity differs in command")
            for key in ("data", "genesis", "metrics", "bind"):
                Require(len(options.get(key, [])) == 1, "Owned setting missing or duplicated")
            data = PurePosixPath(options["data"][0])
            Require(data.is_absolute() and ".." not in data.parts and data.parts[-3:] == (expected_run, name, "db"),
                    "Load DB path identity mismatch")
            argv_counts += 1
            for key, val in actual.items():
                observed_settings.setdefault(key, set()).add(val)
        except (ValueError, TypeError, OverflowError):
            errors.append("actual_load_argv_invalid_or_mismatched")
    if set(machines) != seen or len(machines) != 20:
        source_problems.append("machine_snapshot_node_set_incomplete")
    aggregates = {}
    for key in METRICS:
        values = [s.get(key) for s in loaded]
        if any(value is not None and not Number(value) for value in values):
            errors.append("invalid_diagnostic_type_" + key)
        aggregates[key] = Summary(values)
    for key, numerator, denominator in (
        ("db_ms_per_write", "db_write_ms", "db_writes"),
        ("block_wait_ms_per_task", "block_queue_wait_ms", "block_tasks"),
        ("metadata_wait_ms_per_task", "metadata_queue_wait_ms", "metadata_tasks"),
        ("fork_reexecute_per_committed", "fork_reexecute_transactions", "committed"),
    ):
        aggregates[key] = Summary([Ratio(s, numerator, denominator) for s in loaded])
    cache = []
    for s in loaded:
        hits, misses = s.get("execution_cache_hits"), s.get("execution_cache_misses")
        cache.append(hits / (hits + misses) if Number(hits) and Number(misses) and hits + misses > 0 else None)
    aggregates["execution_cache_hit_fraction"] = Summary(cache)
    diagnostics = {"load_snapshots": len(loaded), "complete": len(loaded) == 20,
                   "evidence_missing": sorted(set(source_problems)), "statistics": aggregates}
    proof = {"expected_nodes": 20, "actual_step_matches_nodes": step_counts, "binary_matches_nodes": binary_counts,
             "argv_matches_nodes": argv_counts, "machine_records": len(machines),
             "actual_numeric_argv_values": {key: sorted(values) for key, values in sorted(observed_settings.items())},
             "proven": len(loaded) == len(machines) == step_counts == binary_counts == argv_counts == 20}
    cleanup = {"expected_databases": 20, "records": 0, "removed": 0, "paths_verified": 0}
    cleanup_nodes = set()
    for path in sorted(directory.glob("*-database-cleanup.json")):
        name = path.name.removesuffix("-database-cleanup.json")
        cleanup_nodes.add(name)
        value = Dict(Read(path))
        cleanup["records"] += 1
        cleanup["removed"] += int(value.get("removed") is True)
        target = PurePosixPath(str(value.get("path", "")))
        cleanup["paths_verified"] += int(target.is_absolute() and ".." not in target.parts
                                           and target.parts[-3:] == (expected_run, name, "db"))
    cleanup["proved"] = (cleanup["records"] == cleanup["removed"] == cleanup["paths_verified"] == 20
                          and cleanup_nodes == seen)
    return diagnostics, proof, cleanup


def StateAudit(value, errors):
    value = Dict(value)
    nodes = value.get("nodes", [])
    if not isinstance(nodes, list) or any(not isinstance(x, dict) for x in nodes):
        errors.append("invalid_state_audit_nodes")
        nodes = []
    heads = {node.get("head") for node in nodes}
    hashes = {Dict(node.get("state")).get("state_hash") for node in nodes}
    independent = (len(nodes) == 20 and len({node.get("node") for node in nodes}) == 20
        and all(node.get("passed") is True and node.get("accounts_checked") == 1024 for node in nodes)
        and len(heads) == len(hashes) == 1
        and all(SHA.fullmatch(str(x)) for x in heads | hashes)
        and len({json.dumps(node.get("state"), sort_keys=True) for node in nodes}) == 1
        and all(len(node.get("anchor_checks", [])) == 2
                and all(Dict(check).get("matches") is True for check in node["anchor_checks"]) for node in nodes))
    if value.get("passed") is True and not independent:
        errors.append("passed_state_audit_lacks_twenty_node_proof")
    result = {"reported_passed": Scalar(value, "passed", errors, "bool"), "independently_consistent": independent,
        "node_count": len(nodes), "accounts_checked_min": min((n.get("accounts_checked", 0) for n in nodes), default=None),
        "local_account_audits_passed": sum(n.get("passed") is True for n in nodes),
        "head_variants": len(heads) if nodes else None, "account_state_hash_variants": len(hashes) if nodes else None,
        "reported_heads_equal": Scalar(value, "all_heads_equal", errors, "bool"),
        "reported_states_equal": Scalar(value, "all_states_equal", errors, "bool"),
        "confirmed_transactions": Summary([n.get("confirmed") for n in nodes]),
        "heights": Summary([n.get("height") for n in nodes])}
    return result


def Measurement(value, errors):
    value = Dict(value)
    result = {"reported_passed": Scalar(value, "passed", errors, "bool"),
              "failure": ErrorSummary(value.get("reasons"))}
    for key in ("measurement_seconds", "transaction_delta", "height_delta", "mean_common_block_interval_ms"):
        result[key] = Scalar(value, key, errors)
    for key, length in (("tps_bounds", 2), ("endpoint_spans", 2), ("block_interval_bounds_ms", 2)):
        observed = value.get(key)
        if observed is not None and not (isinstance(observed, list) and len(observed) == length and all(Number(x) for x in observed)):
            errors.append("invalid_measurement_" + key)
            observed = None
        result[key] = observed
    intervals = value.get("endpoint_intervals")
    interval_shape = (isinstance(intervals, list) and len(intervals) == 2
                      and all(isinstance(x, list) and len(x) == 2 and all(Number(y) for y in x) for x in intervals))
    if intervals is not None and not interval_shape:
        errors.append("invalid_measurement_endpoint_intervals")
    result["endpoint_intervals"] = intervals if interval_shape else None
    consistent = False
    if value.get("passed") is True:
        if interval_shape and all(Number(value.get(k)) for k in ("transaction_delta", "height_delta", "measurement_seconds", "tps")):
            (a0, a1), (b0, b1) = intervals
            count, height, seconds = value["transaction_delta"], value["height_delta"], value["measurement_seconds"]
            consistent = (2 <= a0 <= a1 <= 2.5 and 12 <= b0 <= b1 <= 12.5 and a1 < b0
                and height > 0 and count >= 0 and seconds > 0 and Close(seconds, (b0 + b1 - a0 - a1) / 2)
                and Close(value["tps"], count / seconds)
                and result["tps_bounds"] is not None
                and all(Close(x, y) for x, y in zip(result["tps_bounds"], (count / (b1 - a0), count / (b0 - a1)))))
        if not consistent:
            errors.append("passed_measurement_arithmetic_or_endpoints_invalid")
    result["independently_consistent"] = consistent
    return result


def Latency(value, errors):
    if not isinstance(value, dict):
        return {"available": False, "expected_samples": 100, "observed_and_persisted_samples": None,
                "missing_samples": None, "all_samples_within_one_second": None}
    out = {"available": True}
    for key in ("expected_samples", "observed_and_persisted_samples", "missing_samples", "missing_fraction",
                "p50_upper_ms", "p95_upper_ms", "p99_upper_ms", "unbounded_samples_in_percentile_population",
                "observed_above_one_second_samples", "within_one_second_fraction"):
        out[key] = Scalar(value, key, errors)
    for key in ("passed", "all_samples_within_one_second"):
        out[key] = Scalar(value, key, errors, "bool")
    samples = value.get("samples")
    if not isinstance(samples, list) or len(samples) != 100 or any(not isinstance(s, dict) for s in samples):
        errors.append("latency_population_not_one_hundred")
        out["independently_consistent"] = False
        return out
    if len({s.get("index") for s in samples}) != 100 or len({s.get("hash") for s in samples}) != 100:
        errors.append("latency_duplicate_sample_identity")
    population, batch_values, complete, late, ontime = [], [], 0, 0, 0
    for s in samples:
        good = s.get("observation_complete") is True
        t = s.get("scheduled_to_all_confirmation_upper_ms")
        if good:
            valid = (Number(t) and t >= 0 and s.get("persisted_on_all_canonical_chains") is True
                     and all(Number(s.get(k)) for k in ("scheduled", "send_begin", "included_at"))
                     and s["scheduled"] <= s["send_begin"] <= s["included_at"]
                     and Close(t, 1000 * (s["included_at"] - s["scheduled"])))
            if not valid:
                errors.append("latency_complete_sample_invalid")
                good = False
        population.append(t if good else math.inf)
        complete += int(good)
        late += int(good and t > 1000)
        ontime += int(good and t <= 1000)
        if s.get("within_one_second") is not (good and t <= 1000):
            errors.append("latency_sample_deadline_flag_mismatch")
        if good:
            batch = s.get("batch_start_to_all_confirmation_upper_ms")
            if not Close(batch, 1000 * (s["included_at"] - s["send_begin"])):
                errors.append("latency_batch_timestamp_mismatch")
            else:
                batch_values.append(batch)
    population.sort()
    derived = {"expected_samples": 100, "observed_and_persisted_samples": complete,
               "missing_samples": 100 - complete, "missing_fraction": (100 - complete) / 100,
               "unbounded_samples_in_percentile_population": 100 - complete,
               "observed_above_one_second_samples": late, "within_one_second_fraction": ontime / 100,
               "passed": complete == 100, "all_samples_within_one_second": ontime == 100}
    for percent in (50, 95, 99):
        p = population[math.ceil(percent) - 1]
        derived["p%d_upper_ms" % percent] = p if math.isfinite(p) else None
    for key, expected in derived.items():
        observed = value.get(key)
        if not (observed is None and expected is None or type(expected) is bool and observed is expected
                or Number(expected) and Close(observed, expected)):
            errors.append("latency_aggregate_mismatch_" + key)
    out["independently_consistent"] = not any(x.startswith("latency_") for x in errors)
    out["observed_max_upper_ms"] = max((x for x in population if math.isfinite(x)), default=None)
    out["batch_start_observed_max_upper_ms"] = max(batch_values, default=None)
    return out


def Drain(row, errors):
    metrics = Dict(row.get("metrics"))
    point = Dict(metrics.get("head_drain_observation"))
    nodes = point.get("nodes", [])
    accepted = {metrics.get("sent_unique_completed_all_ingress"), metrics.get("sent_unique_attempted_upper_bound")}
    valid = (all(Number(x) for x in accepted) and isinstance(nodes, list) and len(nodes) == 20 and all(isinstance(n, dict) for n in nodes)
        and len({n.get("node") for n in nodes}) == 20
        and all(isinstance(n.get("head"), dict) for n in nodes)
        and len({n["head"].get("hash") for n in nodes}) == 1
        and all(SHA.fullmatch(str(n["head"].get("hash"))) for n in nodes)
        and all(n["head"].get("transactions") in accepted and not n.get("error")
                and Number(n.get("start_seconds")) and Number(n.get("end_seconds"))
                and 12 <= n["start_seconds"] <= n["end_seconds"] <= 17 for n in nodes))
    reported = row.get("drained_within_budget")
    if reported is not None and reported is not valid:
        errors.append("drain_budget_flag_mismatch")
    audit = Dict(row.get("state_audit"))
    audit_nodes = audit.get("nodes", [])
    final = bool(audit.get("passed") is True and isinstance(audit_nodes, list) and len(audit_nodes) == 20
                 and all(Dict(node).get("confirmed") in accepted for node in audit_nodes))
    if row.get("final_state_matches_accepted") is not None and row["final_state_matches_accepted"] is not final:
        errors.append("drain_final_state_flag_mismatch")
    if row.get("drained") is not None and row["drained"] is not (valid and final is True):
        errors.append("drain_combined_flag_mismatch")
    return {"reported_drained": Scalar(row, "drained", errors, "bool"),
        "reported_within_five_second_budget": Scalar(row, "drained_within_budget", errors, "bool"),
        "independently_observed_within_budget": valid,
        "final_state_matches_accepted": Scalar(row, "final_state_matches_accepted", errors, "bool"),
        "observation_nodes": len(nodes) if isinstance(nodes, list) else None,
        "last_observation_end_seconds": max((n.get("end_seconds") for n in nodes if isinstance(n, dict)
              and Number(n.get("end_seconds"))), default=None) if isinstance(nodes, list) else None}


def Recovery(root, errors):
    wrapper = Dict(Read(root / "comparison-result.json", {}))
    out = {key: Scalar(wrapper, key, errors, "bool") for key in
           ("completed", "restored", "cleanup", "permanent_peers_restored", "permanent_chain_unchanged")}
    out.update({key: Scalar(wrapper, key, errors) for key in ("benchmark_exit", "restore_exit", "cleanup_exit", "finished")})
    out["failure"] = ErrorSummary([wrapper.get(k) for k in ("error", "restore_error", "cleanup_error") if wrapper.get(k)])
    for action in ("pause", "restore"):
        files = sorted((root / ("services-" + action)).glob("*.json"))
        ok, matched = 0, 0
        for path in files:
            value = Dict(Read(path))
            result = Dict(value.get("result"))
            ok += int(value.get("ok") is True)
            if action == "restore":
                before, after = Dict(result.get("before")), Dict(result.get("after"))
                match = result.get("restored") is True and set(before) == set(after) == {"chain-node.service", "fisco-bcos.service"}
                for service in before:
                    a, b = Dict(before[service]), Dict(after.get(service))
                    match = bool(match and all(a.get(k) == b.get(k) for k in ("active", "load", "unit_sha256"))
                        and (a.get("active") != "active" or all(a.get(k) == b.get(k) for k in ("exe", "argv"))))
                matched += int(match)
        out[action + "_service_evidence"] = {"records": len(files), "successful": ok,
            "restored_identity_matches": matched if action == "restore" else None,
            "complete": len(files) == ok == 20 and (action != "restore" or matched == 20)}
    before, after = Dict(Read(root / "permanent-before.json", {})), Dict(Read(root / "permanent-after.json", {}))
    same_nodes = len(before) == len(after) == 20 and set(before) == set(after)
    unchanged = same_nodes and all({k: v for k, v in Dict(before[n]).items() if k != "peers"}
                                  == {k: v for k, v in Dict(after[n]).items() if k != "peers"} for n in before)
    peers = same_nodes and all(Dict(after[n]).get("peers") == 19 for n in after)
    out["permanent_snapshots"] = {"before_nodes": len(before), "after_nodes": len(after),
        "same_node_set": same_nodes, "chain_and_state_unchanged": unchanged, "all_peers_restored": peers}
    for key, value in (("permanent_chain_unchanged", unchanged), ("permanent_peers_restored", peers)):
        if wrapper.get(key) is True and not value:
            errors.append("recovery_" + key + "_unproved")
    files = sorted(root.glob("cleanup-*.json"))
    ok = checked = removed = absent = not_started = reported_errors = 0
    for path in files:
        value = Dict(Read(path))
        result = Dict(value.get("result"))
        ok += int(value.get("ok") is True and result.get("ok") is True)
        failure = result.get("errors", [])
        reported_errors += len(failure) if isinstance(failure, (list, dict)) else int(bool(failure))
        for item in result.get("results", []):
            if not isinstance(item, dict):
                errors.append("invalid_outer_cleanup_entry")
                continue
            if item.get("not_started") is True:
                not_started += 1
            else:
                checked += 1
                database = Dict(item.get("database"))
                removed += int(database.get("removed") is True)
                absent += int(database.get("removed") is False)
    out["outer_cleanup"] = {"host_records": len(files), "successful_host_records": ok,
        "case_checks": checked, "removed_during_outer_cleanup": removed,
        "already_absent_responses": absent, "not_started": not_started, "reported_errors": reported_errors,
        "complete": len(files) == ok == 20 and reported_errors == 0}
    return out


def Csv(path, rows, columns):
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})


def Run(evidence, output):
    root = FindRoot(evidence)
    output = output.resolve()
    Require(output != root and not output.is_relative_to(root), "Output must be outside the immutable evidence run directory")
    Require(not output.exists(), "Output directory already exists; select a new directory")
    benchmark = root / "benchmark"
    source = Dict(Read(benchmark / "results.json", {}))
    protocol = Dict(Read(benchmark / "protocol.json", {}))
    global_errors = []
    if protocol.get("nodes") != 20 or protocol.get("rates") != list(RATES) or protocol.get("block_tx_steps") != list(STEPS):
        global_errors.append("fixed_protocol_schedule_mismatch_or_missing")
    releases = protocol.get("releases", [])
    Require(isinstance(releases, list) and len(releases) <= 1, "Evidence contains multiple releases; wrong comparison selected")
    release = Dict(releases[0]) if releases else {}
    binary = release.get("binary_sha256", release.get("sha256"))
    if not SHA.fullmatch(str(binary)):
        global_errors.append("pinned_binary_sha256_missing_or_invalid")
        binary = None
    code = release.get("source_commit")
    if code is not None and not COMMIT.fullmatch(str(code)):
        global_errors.append("source_commit_invalid")
        code = None
    indexed = {}
    for row in source.get("cases", []):
        Require(isinstance(row, dict), "Invalid case row")
        key = CaseKey(row)
        Require(key in SCHEDULE and key not in indexed, "Duplicate or unexpected case; refusing ambiguous aggregation")
        indexed[key] = row
    directories = {}
    for directory in sorted(benchmark.iterdir()) if benchmark.is_dir() else []:
        if not directory.is_dir() or directory.name == "ansible":
            continue
        saved = Read(directory / "result.json")
        p = Read(directory / "protocol.json")
        if not isinstance(saved, dict) and not isinstance(p, dict):
            continue
        key = CaseKey(saved if isinstance(saved, dict) else p)
        if key not in SCHEDULE:
            global_errors.append("unexpected_case_directory")
            continue
        Require(key not in directories, "Duplicate case evidence directories")
        directories[key] = directory
        if isinstance(saved, dict):
            if key in indexed and saved != indexed[key]:
                global_errors.append("case_result_differs_from_global_summary")
            elif key not in indexed:
                indexed[key] = saved
                global_errors.append("case_result_missing_from_global_summary")
    rows, diagnostics = [], []
    for ordinal, key in enumerate(SCHEDULE, 1):
        kind, rate, step = key
        row = Dict(indexed.get(key))
        directory = directories.get(key)
        errors = []
        measurement = Measurement(row.get("audited_measurement"), errors)
        state = StateAudit(row.get("state_audit"), errors)
        metrics = Dict(row.get("metrics"))
        d, proof, cleanup = CaseEvidence(directory, step, binary, row.get("run"), errors)
        diagnostics.append({"ordinal": ordinal, "kind": kind, "offered_tps": rate, "block_tx_step": step, **d})
        flags = bool(row.get("tps_valid") is True and metrics.get("load_success") is True
                     and Dict(row.get("state_audit")).get("passed") is True
                     and Dict(row.get("audited_measurement")).get("passed") is True)
        eligible = flags and state["independently_consistent"] and measurement["independently_consistent"] and proof["proven"]
        if row.get("tps_valid") is True and not eligible:
            errors.append("reported_formal_tps_lacks_consistent_proof")
        status = row.get("status") if row.get("status") in ("complete", "failed") else "not_recorded"
        result = {"ordinal": ordinal, "kind": kind, "offered_tps": rate, "block_tx_step": step,
            "status": status, "recorded": bool(row), "reported_success": Scalar(row, "success", errors, "bool"),
            "tps_valid_reported": Scalar(row, "tps_valid", errors, "bool"), "formal_tps_eligible": eligible,
            "formal_tps": Dict(row.get("audited_measurement")).get("tps") if eligible else None,
            "actual_send_tps": Scalar(metrics, "achieved_send_tps", errors),
            "load_success": Scalar(metrics, "load_success", errors, "bool"),
            "sent_unique_completed_all_ingress": Scalar(metrics, "sent_unique_completed_all_ingress", errors),
            "sent_unique_attempted_upper_bound": Scalar(metrics, "sent_unique_attempted_upper_bound", errors),
            "sent_wire_copies_completed": Scalar(metrics, "sent_wire_copies_completed", errors),
            "partial_batch_present": bool(metrics["partial_batch"]) if "partial_batch" in metrics else None,
            "measurement": measurement, "state_audit": state, "drain": Drain(row, errors),
            "actual_start_proof": proof, "database_cleanup": cleanup,
            "cleanup_error_count": len(row.get("cleanup_errors", [])) if isinstance(row.get("cleanup_errors"), list) else None,
            "failure": ErrorSummary([value for value in (row.get("error"), metrics.get("load_error"),
                row.get("observer_cleanup_error"), row.get("cleanup_errors")) if value])}
        if kind == "latency":
            result["latency"] = Latency(row.get("latency"), errors)
        if flags and not proof["proven"]:
            errors.append("formal_case_actual_configuration_unproved")
        if row.get("success") is True and not cleanup["proved"]:
            errors.append("successful_case_database_cleanup_unproved")
        result["validation_errors"] = sorted(set(errors))
        rows.append(result)
    for r in rows:
        baseline = next(x for x in rows if x["kind"] == r["kind"] and x["offered_tps"] == r["offered_tps"] and x["block_tx_step"] == 0)
        comparable = (r["kind"] == "throughput" and r["formal_tps_eligible"] and baseline["formal_tps_eligible"]
                      and r["actual_start_proof"]["proven"] and baseline["actual_start_proof"]["proven"]
                      and Number(baseline["formal_tps"]) and baseline["formal_tps"] > 0)
        r["same_rate_baseline_formal_tps"] = baseline["formal_tps"] if r["kind"] == "throughput" else None
        r["improvement_vs_step0_percent"] = 100 * (r["formal_tps"] / baseline["formal_tps"] - 1) if comparable else None
    recovery = Recovery(root, global_errors)
    if len(indexed) != len(SCHEDULE):
        global_errors.append("case_results_incomplete")
    summary = {"schema": 1, "source_commit": code, "binary_sha256": binary,
        "expected_cases": 12, "recorded_cases": len(indexed), "expected_nodes": 20,
        "predeclared_steps": list(STEPS), "predeclared_rates": list(RATES),
        "reported_comparison_passed": Scalar(source, "passed", global_errors, "bool"),
        "reported_latency_one_second_target_passed": Scalar(source, "latency_one_second_target_passed", global_errors, "bool"),
        "qualified_throughput_cases": sum(r["kind"] == "throughput" and r["formal_tps_eligible"] for r in rows),
        "same_binary_and_actual_step_proven_all_cases": all(r["actual_start_proof"]["proven"] for r in rows),
        "database_cleanup": {"expected": 240, "records": sum(r["database_cleanup"]["records"] for r in rows),
            "removed": sum(r["database_cleanup"]["removed"] for r in rows),
            "paths_verified": sum(r["database_cleanup"]["paths_verified"] for r in rows),
            "all_cases_proved": all(r["database_cleanup"]["proved"] for r in rows)},
        "recovery": recovery, "cases": rows,
        "source_evidence_sha256": {label: Sha(path) for label, path in (
            ("benchmark_results", benchmark / "results.json"), ("benchmark_protocol", benchmark / "protocol.json"),
            ("comparison_result", root / "comparison-result.json")) if path.is_file()},
        "validation_errors": sorted(set(global_errors)),
        "scope": {"tps": "All-node persisted common-ancestor net transaction growth between exact 2s/12s head observations; single short run per variant.",
            "failed_baseline": "An invalid K=0 window leaves its formal TPS and every same-rate relative improvement null; no diagnostic or historical value substitutes for it.",
            "state_audit": "Full state objects include the chain head. Full-state/head inequality alone does not establish an account balance or nonce error; inspect local account audit counts and account_state_hash_variants separately.",
            "latency": "Scheduled arrival to first full all-node identical-position observation, checked on final persisted canonical ledgers; includes batching, transmission, polling and RPC.",
            "diagnostics": "Load-process cumulative counters through stop, including warmup/drain/stop; not the exact ten-second TPS window. Per-node ratios are calculated before node aggregation.",
            "step_comparison": "K>1 also uses current-round scheduling; comparisons measure the complete step mode.",
            "privacy": "Field allowlist; no host identifiers or addresses, arbitrary errors, logs, account records, original argv paths or credentials are emitted."}}
    summary["validation_passed"] = not global_errors and all(not r["validation_errors"] for r in rows)
    output.mkdir(parents=True)
    Write(output / "summary.json", summary)
    Write(output / "diagnostics.json", {"scope": summary["scope"]["diagnostics"], "cases": diagnostics})
    throughput, latency = [], []
    for r in rows:
        flat = {key: r[key] for key in ("ordinal", "offered_tps", "block_tx_step", "status", "formal_tps_eligible",
            "formal_tps", "actual_send_tps", "same_rate_baseline_formal_tps", "improvement_vs_step0_percent")}
        flat.update(measurement_seconds=r["measurement"].get("measurement_seconds"),
            mean_common_block_interval_ms=r["measurement"].get("mean_common_block_interval_ms"),
            drained_within_budget=r["drain"]["reported_within_five_second_budget"],
            drained=r["drain"]["reported_drained"], state_audit_passed=r["state_audit"]["reported_passed"],
            actual_start_proven=r["actual_start_proof"]["proven"], cleanup_proven=r["database_cleanup"]["proved"],
            validation_error_count=len(r["validation_errors"]))
        if r["kind"] == "throughput":
            throughput.append(flat)
        else:
            flat.update({key: r["latency"].get(key) for key in ("expected_samples", "observed_and_persisted_samples",
                "missing_samples", "missing_fraction", "p50_upper_ms", "p95_upper_ms", "p99_upper_ms",
                "observed_above_one_second_samples", "within_one_second_fraction", "all_samples_within_one_second")})
            latency.append(flat)
    base_columns = ["ordinal", "offered_tps", "block_tx_step", "status", "formal_tps_eligible", "formal_tps",
        "actual_send_tps", "same_rate_baseline_formal_tps", "improvement_vs_step0_percent", "measurement_seconds",
        "mean_common_block_interval_ms", "drained_within_budget", "drained", "state_audit_passed",
        "actual_start_proven", "cleanup_proven", "validation_error_count"]
    Csv(output / "throughput.csv", throughput, base_columns)
    Csv(output / "latency.csv", latency, base_columns + ["expected_samples", "observed_and_persisted_samples",
        "missing_samples", "missing_fraction", "p50_upper_ms", "p95_upper_ms", "p99_upper_ms",
        "observed_above_one_second_samples", "within_one_second_fraction", "all_samples_within_one_second"])
    return summary


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    summary = Run(args.evidence_root, args.output)
    print(json.dumps({"expected_cases": 12, "recorded_cases": summary["recorded_cases"],
        "qualified_throughput_cases": summary["qualified_throughput_cases"],
        "validation_passed": summary["validation_passed"]}))
    return 0 if summary["validation_passed"] else 2


if __name__ == "__main__":
    try:
        sys.exit(Main())
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        # A malformed input might embed sensitive strings in an exception.
        print(json.dumps({"aggregation_failed": True, "error_type": type(error).__name__,
                          "error": ErrorSummary(str(error))}))
        sys.exit(2)
