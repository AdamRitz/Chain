"""Read public node/index evidence from an explicitly named, isolated desktop QA directory.

No wallet vaults, HTTP session tokens, or raw signed outbox bytes are exported.
This script neither launches nodes nor performs transactions or UI actions.
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "Test"))
from NetworkTest import GetStats, GetAccount
from CollectEvidence import NoSecrets


def Main(args):
    work = args.work.resolve()
    assert work.parent == (REPO/"build").resolve() and work.name.startswith("classroom-native-gui-")
    session = json.loads((work/"data/session.json").read_text(encoding="utf-8"))
    network = work/"data/networks"/session["id"]
    profile = json.loads((network/"profile.json").read_text(encoding="utf-8"))
    profile.pop("grants", None)
    nodes = [GetStats(port) for port in profile["ports"]]
    assert len(nodes) == 3 and len({n["head"] for n in nodes}) == 1
    with sqlite3.connect(network.as_uri()+"/index.sqlite?mode=ro", uri=True) as connection:
        deadline = time.monotonic() + 8
        while (connection.execute("SELECT COALESCE(MAX(height),0) FROM blocks").fetchone()[0] < nodes[0]["height"]):
            if time.monotonic() >= deadline:
                raise AssertionError("Desktop index did not reach the observed canonical height")
            time.sleep(.15)
        def Rows(table):
            rows = [json.loads(row[0]) for row in connection.execute("SELECT data FROM "+table)]
            for row in rows:
                row.pop("raw", None)
            return rows
        events, outbox = Rows("events"), Rows("outbox")
    accounts = {a["public_key"]: GetAccount(profile["ports"][0], a["public_key"]) for a in profile["genesis"]["accounts"]}
    value = {"collected_at": datetime.now().astimezone().isoformat(), "profile": profile,
             "nodes": nodes, "events": events, "outbox": outbox, "accounts": accounts}
    NoSecrets(value)
    target = Path(__file__).resolve().parents[1]/"data"/("native-"+args.tag+".json")
    assert not target.exists(), str(target)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"head": nodes[0]["head"], "height": nodes[0]["height"], "nodes": len(nodes), "events": len(events)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--tag", choices=["after-transfer", "after-chat", "after-chat-settled", "after-restart", "after-desktop-relaunch"], required=True)
    Main(parser.parse_args())
