import { test } from "node:test";
import assert from "node:assert/strict";
import {
  MergeMessages,
  CanSend,
  SelectedMessage,
  RestartMatches,
} from "../src/model.ts";
test("fresh canonical metadata replaces an older cached location of a replayed transaction", () => {
  const old = {
    id: "same-tx-0",
    height: 10,
    tx_index: 2,
    log_index: 0,
    body: "same signed message",
  };
  const replacement = { ...old, height: 12, tx_index: 0 };
  const rows = MergeMessages([old], [replacement]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].height, 12);
});
test("message order uses canonical block, transaction and event positions instead of local wall-clock", () => {
  const rows = MergeMessages(
    [],
    [
      { id: "b", height: 2, tx_index: 1, log_index: 0, observed_ms: 1 },
      { id: "a", height: 2, tx_index: 0, log_index: 1, observed_ms: 9999 },
    ],
  );
  assert.deepEqual(
    rows.map((x) => x.id),
    ["a", "b"],
  );
});
test("keyboard and button share permissions and a UTF-8 byte limit", () => {
  const state = {
    unlocked: true,
    running: true,
    contractReady: true,
    roomOpen: true,
    busy: false,
  };
  assert(CanSend("中".repeat(170) + "ab", state));
  assert(!CanSend("中".repeat(171), state));
  for (const key of ["unlocked", "running", "contractReady", "roomOpen"])
    assert(!CanSend("hello", { ...state, [key]: false }));
  assert(!CanSend("hello", { ...state, busy: true }));
  assert(!CanSend("   ", state));
});
test("message inspector ignores transfer selections and follows current canonical metadata", () => {
  const canonical = { body: "hello", tx_hash: "chat", height: 12 };
  assert.equal(
    SelectedMessage({ hash: "transfer", height: 3 }, [canonical]),
    undefined,
  );
  assert.equal(
    SelectedMessage({ ...canonical, height: 10 }, [canonical]),
    canonical,
  );
  assert.equal(SelectedMessage(canonical, []), undefined);
});
test("restart lesson requires a resumed contract and the same network and canonical head", () => {
  const checkpoint = { id: "class-a", head: "head-a" };
  const state = {
    running: true,
    contract_ready: true,
    network: { id: "class-a" },
    stats: { head: "head-a" },
  };
  assert(RestartMatches(checkpoint, state));
  assert(!RestartMatches(null, state));
  assert(!RestartMatches(checkpoint, { ...state, running: false }));
  assert(!RestartMatches(checkpoint, { ...state, contract_ready: false }));
  assert(!RestartMatches(checkpoint, { ...state, network: { id: "class-b" } }));
  assert(!RestartMatches(checkpoint, { ...state, stats: { head: "head-b" } }));
});
