"""Run on the Linux controller: stage SDK drivers, check ten nodes, or send ten smoke transfers.

Does not install packages, edit node configuration, start/stop nodes, or delete databases.
Requires an already compiled driver-package.tar.gz and the official generated SDK certificates.
"""
import argparse
import concurrent.futures
import http.client
import json
from pathlib import Path
import shlex
import ssl
import subprocess
import time


def Load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def Save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def Run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, capture_output=True, **kwargs).stdout


def Parallel(function, values):
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(values)) as pool:
        return list(pool.map(function, values))


class Controller:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.root)
        self.hosts = Load(args.hosts)
        if not self.hosts or len(set(self.hosts)) != len(self.hosts):
            raise ValueError("Hosts must be non-empty and unique")
        self.generated = self.root / "generated" / args.generated
        self.driver = self.root / "driver"
        cert = self.generated / self.hosts[0] / "sdk"
        self.context = ssl.create_default_context(cafile=str(cert / "ca.crt"))
        # Generated membership certificates authenticate via the generated private CA.
        self.context.check_hostname = False
        self.context.load_cert_chain(str(cert / "sdk.crt"), str(cert / "sdk.key"))
        self.ssh_options = ["-i", args.key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                            "-o", "UserKnownHostsFile=" + args.known_hosts, "-o", "ConnectTimeout=10"]

    def Remote(self, host, script):
        return Run(["ssh", *self.ssh_options, "root@" + host, "python3 -"], input=script, timeout=180)

    def Copy(self, source, host, destination):
        return Run(["scp", *self.ssh_options, str(source), "root@" + host + ":" + destination], timeout=180)

    def Rpc(self, host, method, *params):
        begin = time.time()
        connection = http.client.HTTPSConnection(host, 20200, context=self.context, timeout=5)
        try:
            body = json.dumps({"jsonrpc": "2.0", "method": method,
                               "params": ["group0", "", *params], "id": 1})
            connection.request("POST", "/", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read())
            if "error" in result:
                raise RuntimeError(result["error"])
            return {"begin_epoch": begin, "end_epoch": time.time(), "result": result["result"]}
        finally:
            connection.close()

    def Snapshot(self):
        def One(host):
            count = self.Rpc(host, "getTotalTransactionCount")
            pending = self.Rpc(host, "getPendingTxSize")
            return {"host": host, **count["result"], "count_begin_epoch": count["begin_epoch"],
                    "count_end_epoch": count["end_epoch"], "pending": pending["result"],
                    "pending_begin_epoch": pending["begin_epoch"], "pending_end_epoch": pending["end_epoch"]}
        return Parallel(One, self.hosts)

    def Check(self):
        rows = self.Snapshot()
        height = min(row["blockNumber"] for row in rows)
        def One(host):
            block = self.Rpc(host, "getBlockByNumber", height, True, True)["result"]
            return {"host": host, "height": height, "hash": block["hash"], "stateRoot": block.get("stateRoot"),
                    "sealers": len(self.Rpc(host, "getSealerList")["result"]),
                    "observers": len(self.Rpc(host, "getObserverList")["result"])}
        blocks = Parallel(One, self.hosts)
        valid = (len({row["blockNumber"] for row in rows}) == 1
                 and len({row["transactionCount"] for row in rows}) == 1
                 and len({row["hash"] for row in blocks}) == 1
                 and len({row["stateRoot"] for row in blocks}) == 1
                 and all(row["stateRoot"] is not None and row["sealers"] == len(self.hosts)
                         and row["observers"] == 0 for row in blocks))
        return {"valid": valid, "nodes": rows, "blocks": blocks}

    def Ready(self, seconds=30):
        deadline = time.monotonic() + seconds
        while True:
            value = self.Check()
            if value["valid"] and all(row["pending"] == 0 for row in value["nodes"]):
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError("Nodes are not a consistent drained chain: " + json.dumps(value))
            time.sleep(0.5)

    def Stage(self):
        package = Path(__file__).resolve().parent / "driver-package.tar.gz"
        if not package.is_file():
            raise FileNotFoundError("Run build-driver.sh beside this script first")
        destination = str(self.root / "packages" / "driver-package.tar.gz")
        def One(host):
            self.Remote(host, f"import pathlib,os\nos.umask(0o077)\npathlib.Path({str(self.root / 'packages')!r}).mkdir(parents=True,exist_ok=True)\npathlib.Path({str(self.driver)!r}).mkdir(parents=True,exist_ok=True)\n")
            self.Copy(package, host, destination)
            config = ("[cryptoMaterial]\ncertPath = " + json.dumps(str(self.driver / "conf")) +
                      '\ndisableSsl = "false"\nuseSMCrypto = "false"\n[network]\npeers = ["' + host +
                      ':20200"]\nsendRpcRequestToHighestBlockNode = "false"\nmessageTimeout = "15000"\n[account]\nkeyStoreDir = ' +
                      json.dumps(str(self.driver / "account")) + '\n[threadPool]\nthreadPoolSize = "2"\n')
            self.Remote(host, f"import os,pathlib,tarfile\nos.umask(0o077)\nroot=pathlib.Path({str(self.driver)!r})\nwith tarfile.open({destination!r}) as archive: archive.extractall(root)\n(root/'conf').mkdir(exist_ok=True)\n(root/'config.toml').write_text({config!r})\n")
            for name in ["ca.crt", "sdk.crt", "sdk.key"]:
                self.Copy(self.generated / host / "sdk" / name, host, str(self.driver / "conf" / name))
            return {"host": host, "driver": str(self.driver), "node_configuration_changed": False}
        return Parallel(One, self.hosts)

    def Smoke(self):
        before = self.Ready()
        output = self.root / "results" / self.args.name
        output.mkdir(parents=True, exist_ok=False)
        Save(output / "before.json", before)
        command = ["java", "-Xms64m", "-Xmx512m", "-XX:ActiveProcessorCount=2", "-cp", "classes:lib/*",
                   "FiscoBench", "config.toml", "solidity", "1", "0", "10", str(output / "driver.json"), "4"]
        process = subprocess.run(command, cwd=self.driver, capture_output=True, text=True, timeout=240)
        (output / "driver.log").write_text(process.stdout + process.stderr, encoding="utf-8")
        after = self.Ready()
        Save(output / "after.json", after)
        driver = Load(output / "driver.json") if (output / "driver.json").exists() else {}
        delta = after["nodes"][0]["transactionCount"] - before["nodes"][0]["transactionCount"]
        valid = (process.returncode == 0 and driver.get("success") == 10 and driver.get("failures") == 0
                 and driver.get("unresolved") == 0 and driver.get("balances_checked") == 4
                 and driver.get("balance_mismatches") == 0 and after["valid"] and delta == 15)
        summary = {"smoke_only": True, "name": self.args.name, "mode": "solidity", "transfers": 10,
                   "setup_transactions": 5, "committed_count_delta": delta, "valid": valid,
                   "observed_receipt_p50_ms": driver.get("p50_ms"), "observed_receipt_p99_ms": driver.get("p99_ms"),
                   "latency_caveat": "Ten transfers are a deployment check, not a stable percentile estimate",
                   "node_defaults_preserved": True, "balance_check_node": self.hosts[0]}
        Save(output / "summary.json", summary)
        if not valid:
            raise RuntimeError("Smoke verification failed: " + json.dumps(summary))
        return summary


def Main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stage", "check", "smoke"])
    parser.add_argument("--root", default="/opt/fisco-control")
    parser.add_argument("--hosts", default=str(Path(__file__).with_name("hosts.json")))
    parser.add_argument("--generated", default="default-v3.7.3")
    parser.add_argument("--key", default="/root/.ssh/chain_ansible_ed25519")
    parser.add_argument("--known-hosts", default="/opt/chain-control/known_hosts")
    parser.add_argument("--name", default="smoke-" + time.strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    controller = Controller(args)
    value = {"stage": controller.Stage, "check": controller.Check, "smoke": controller.Smoke}[args.action]()
    print(json.dumps(value, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    Main()
