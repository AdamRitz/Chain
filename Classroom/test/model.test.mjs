import { test } from "node:test";
import assert from "node:assert/strict";
import {
  MergeMessages,
  CanSend,
  FundingText,
  FundingPending,
  BlockTransaction,
  SelectedMessage,
  RestartMatches,
  NetworkView,
  NetworkRequest,
  ChainDrafts,
  ChainLabel,
} from "../src/model.ts";

test("request scope binds its source chain and rejects an earlier A view after A-B-A", () => {
  const view = new NetworkView();
  view.Select("A");
  const first = view.Capture();
  assert.equal(NetworkRequest("network_switch", {id:"B",network_id:"wrong"}, first).network_id, "A");
  view.Invalidate();
  assert.equal(view.Current(first), false);
  view.Select("B");
  assert.equal(NetworkRequest("message", {}, view.Capture()).network_id, "B");
  view.Select("A");
  assert.equal(view.Current(first), false);
  assert.equal(view.Current(view.Capture()), true);
});

test("chain drafts and connection input survive switching without sharing fields", () => {
  const drafts = new ChainDrafts();
  drafts.Write("A", {body:"draft A",invite:"[::1]:8787",view:"wallet",room:2});
  drafts.Write("B", {body:"draft B",invite:"192.168.1.2:8787"});
  assert.equal(drafts.Read("A").body, "draft A");
  assert.equal(drafts.Read("A").invite, "[::1]:8787");
  assert.equal(drafts.Read("A").view, "wallet");
  assert.equal(drafts.Read("B").body, "draft B");
  assert.equal(drafts.Read("B").room, 1);
  const copy = drafts.Read("A"); copy.body="changed copy";
  assert.equal(drafts.Read("A").body, "draft A");
});

test("send completion consumes only its unchanged source draft", () => {
  const drafts = new ChainDrafts();
  drafts.Write("A", {body:"sent text"});
  drafts.Write("B", {body:"B keeps this"});
  drafts.Write("A", {body:"new text typed while sending"});
  assert.equal(drafts.Sent("A", "sent text"), "new text typed while sending");
  assert.equal(drafts.Read("B").body, "B keeps this");
  assert.equal(drafts.Sent("A", "new text typed while sending"), "");
});

test("same-name chains are distinguished by their genesis fingerprints", () => {
  const first = ChainLabel({title:"同名链",genesis_root:"a".repeat(64)});
  const second = ChainLabel({title:"同名链",genesis_root:"b".repeat(64)});
  assert.notEqual(first, second);
  assert.match(first, /同名链.*aaaaaaaa/);
  assert.ok(first.length < 64);
});

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

test("a balance snapshot does not prevent an actual send attempt", () => {
  const state = {
    unlocked: true,
    running: true,
    contractReady: true,
    roomOpen: true,
    busy: false,
    funded: false,
  };
  assert(CanSend("hello", state));
  assert(!CanSend("hello", { ...state, running: false }));
  assert(!CanSend("hello", { ...state, busy: true }));
  assert(!CanSend("hello", { ...state, fundingReady: false }));
});

test("automatic funding has a lightweight pending state and leaves legacy chats ready", () => {
  for (const phase of ["waiting", "claiming"]) {
    assert.equal(FundingText({funding_state:{phase}}), "正在领取入群代币");
    assert(FundingPending({funding_state:{phase}}));
  }
  for (const phase of ["ready", "legacy"])
    assert.equal(FundingText({funding_state:{phase}}), "");
  assert.equal(FundingText({}), "");
  assert(!FundingPending({}));
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

test("funding failures expose their reason without keeping the pending send gate", () => {
  assert.match(FundingText({funding_state:{phase:"waiting",error:"等待可用的自动资助入口"}}), /等待可用的自动资助入口/);
  for (const phase of ["error", "unavailable"]) {
    const status = {funding_state:{phase,error:"领取交易未执行成功，请查看交易回执后重试"}};
    assert.equal(FundingText(status), status.funding_state.error);
    assert(!FundingPending(status));
  }
  assert.equal(FundingText({funding_state:{phase:"error"}}), "领取入群代币失败");
  assert.equal(FundingText({funding_state:{phase:"unavailable"}}), "聊天室补贴储备不足");
});

test("a block transaction carries its canonical inclusion even when execution failed", () => {
  const transaction = {hash:"tx",nonce:"1",receipt:{status:2,gas_used:21000}};
  const item = BlockTransaction(transaction, {height:12,hash:"block"});
  assert.equal(item.state, "included");
  assert.equal(item.height, 12);
  assert.equal(item.block_hash, "block");
  assert.equal(item.execution_status, 2);
  assert.equal(item.receipt, transaction.receipt);
  assert.equal(transaction.state, undefined);
  assert.equal(BlockTransaction({hash:"success",receipt:{status:0}}, {height:13,hash:"next"}).execution_status, 0);
  assert.equal(BlockTransaction({hash:"native",kind:"transfer"}, {height:14,hash:"native-block"}).state, "included");
});
