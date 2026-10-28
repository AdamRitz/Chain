"""Read-only evidence collection for the two explicitly owned local GUI fixtures.

The private-session files stay in ignored build directories. Never copy their
session URL, wallet vault, or signed raw outbox into the public report.
"""
import hashlib
import json
import platform
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
REPORT = Path(__file__).resolve().parents[1]


def Save(name, value):
    (REPORT / "data" / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def Api(root, action, **values):
    ready = json.loads((root / "private-session.json").read_text(encoding="utf-8"))
    origin, token = ready["url"].split("/#")
    request = urllib.request.Request(origin + "/api", json.dumps(dict(action=action, **values)).encode(),
                                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def NoSecrets(value):
    if isinstance(value, dict):
        assert not ({"raw", "private_key", "password", "token", "seed"} & value.keys())
        for child in value.values():
            NoSecrets(child)
    elif isinstance(value, list):
        for child in value:
            NoSecrets(child)


def Main():
    teacher = REPO / "build/classroom-gui-browser-20261029-01"
    student = REPO / "build/classroom-gui-student-20261029-01"
    a, b = Api(teacher, "status"), Api(student, "status")
    NoSecrets(a)
    NoSecrets(b)
    Save("teacher-final.json", a)
    Save("student-final.json", b)
    assert len(a["nodes"]) == 3 and len(b["nodes"]) == 1
    nodes = a["nodes"] + b["nodes"]
    assert all(n["online"] for n in nodes)
    assert len({n["head"] for n in nodes}) == 1
    assert a["network"]["genesis_root"] == b["network"]["genesis_root"]
    assert a["network"]["contract"] == b["network"]["contract"]
    assert a["contract_ready"] and b["contract_ready"]
    assert any(c["ipv6"] and c["host"] == "::1" for n in b["nodes"] for c in n["connections"])
    messages_a = {m["tx_hash"]: {k: v for k, v in m.items() if k != "observed_ms"} for m in a["messages"]}
    messages_b = {m["tx_hash"]: {k: v for k, v in m.items() if k != "observed_ms"} for m in b["messages"]}
    assert messages_a == messages_b and len(messages_a) == 4
    sent = next(t for t in b["outbox"] if t.get("body"))
    assert sent["state"] == "included" and sent["execution_status"] == 0 and sent["hash"] in messages_a
    denied = next(t for t in a["outbox"] if t.get("body", "").startswith("未授权成员验证"))
    assert denied["state"] == "included" and denied["execution_status"] != 0 and denied["hash"] not in messages_a
    artifact = json.loads((REPO / "Contracts/ClassroomChat.compiled.json").read_text(encoding="utf-8"))
    selector = artifact["contracts"]["ClassroomChat"]["evm"]["methodIdentifiers"]["messageCount()"]
    count = Api(teacher, "call", to=a["network"]["contract"], input=selector)
    assert count["read_only"] and count["status"] == 0 and int(count["output"], 16) == 4
    Save("ledger-checks.json", {
        "collected_at": datetime.now().astimezone().isoformat(),
        "four_nodes_same_head": True, "head": nodes[0]["head"], "height": nodes[0]["height"],
        "same_genesis_and_contract": True, "pure_ipv6_student_seed": b["network"]["seeds"],
        "canonical_messages_equal": True, "canonical_message_count": 4,
        "student_transaction": sent["hash"], "student_message_height": sent["height"],
        "denied_transaction": denied["hash"], "denied_execution_status": denied["execution_status"],
        "denied_not_a_message_event": True, "read_only_message_count": count,
        "initial_native_transfer": next(t for t in a["outbox"] if t.get("hash", "").startswith("2a773c81")),
    })
    files = ["Classroom/src-tauri/target/release/chain-classroom.exe", "build/classroom-20261028/chain_classroom.exe",
             "build/classroom-20261028/boost.exe", "Contracts/ClassroomChat.compiled.json",
             "Classroom/src/main.tsx", "Classroom/src/model.ts", "Classroom/src/style.css", "Classroom/Gateway/App.h"]
    Save("tested-files.json", {"base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "platform": platform.platform(), "files": [{"path": p, "sha256": hashlib.sha256((REPO/p).read_bytes()).hexdigest()} for p in files]})
    screenshots = [{"file": p.name, "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                   for p in sorted((REPORT/"figures").iterdir()) if p.suffix.lower() in {".png", ".jpg"}]
    Save("screenshots.json", {"source": "Actual in-app browser screenshots; no native desktop screenshots", "files": screenshots})
    print(json.dumps({"four_nodes_same_head": True, "height": nodes[0]["height"], "messages": 4, "screenshots": len(screenshots)}))


if __name__ == "__main__":
    Main()
