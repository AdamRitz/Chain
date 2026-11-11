import type { Row } from "./api";
export type ViewScope = { networkId: string; generation: number; walletSession?: number };
export class NetworkView {
  private scope: ViewScope = { networkId: "", generation: 0 };
  Capture(): ViewScope { return { ...this.scope }; }
  Current(scope: ViewScope): boolean {
    return scope.networkId === this.scope.networkId && scope.generation === this.scope.generation;
  }
  Invalidate(): void { this.scope.generation++; }
  Select(networkId: string, walletSession = this.scope.walletSession): void {
    if (networkId !== this.scope.networkId || walletSession !== this.scope.walletSession) {
      this.scope = { networkId, walletSession, generation: this.scope.generation + 1 };
    }
  }
}
export class StaleViewError extends Error {
  constructor() { super(""); this.name = "StaleViewError"; }
}
export function NetworkRequest(action: string, values: Row, scope: ViewScope): Row {
  return { action, ...values, network_id: scope.networkId,
    ...(scope.walletSession === undefined ? {} : { wallet_session: scope.walletSession }) };
}
export function ChainLabel(network: Row): string {
  const fingerprint = String(network.genesis_root || network.id || "");
  return `${network.title || "未命名链"} · ${fingerprint.slice(0, 8)}…${fingerprint.slice(-6)}`;
}
export type ChainDraft = {
  body: string; invite: string; title: string; room: number; roomName: string;
  receiver: string; amount: string; height: string; nickname: string;
  view: "chat" | "wallet" | "blocks" | "network" | "home";
};
export class ChainDrafts {
  private entries = new Map<string, ChainDraft>();
  Read(networkId: string): ChainDraft {
    return { body: "", invite: "", title: "学术交流", room: 1, roomName: "", receiver: "",
      amount: "10000000", height: "", nickname: "", view: "chat", ...this.entries.get(networkId) };
  }
  Write(networkId: string, values: Partial<ChainDraft>): void {
    this.entries.set(networkId, { ...this.Read(networkId), ...values });
  }
  Sent(networkId: string, body: string): string {
    const current = this.Read(networkId).body;
    if (current === body) this.Write(networkId, { body: "" });
    return this.Read(networkId).body;
  }
  ClearWallet(): void {
    for (const [id, draft] of this.entries)
      this.entries.set(id, { ...draft, body: "", receiver: "", amount: "10000000", roomName: "" });
  }
}
export function FundingText(status: Row): string {
  if (FundingPending(status)) return status.funding_state?.error ? `正在领取入群代币：${status.funding_state.error}` : "正在领取入群代币";
  const funding = status.funding_state;
  if (funding?.phase === "error" || funding?.phase === "unavailable")
    return funding.error || (funding.phase === "unavailable" ? "聊天室补贴储备不足" : "领取入群代币失败");
  return "";
}
export function FundingPending(status: Row): boolean {
  return ["waiting", "claiming"].includes(status.funding_state?.phase);
}
export function BlockTransaction(transaction: Row, block: Row): Row {
  return {
    ...transaction,
    state: "included",
    height: block.height,
    block_hash: block.hash,
    ...(transaction.receipt?.status !== undefined ? { execution_status: transaction.receipt.status } : {}),
  };
}
export function MergeMessages(history: Row[], canonical: Row[]): Row[] {
  const records = new Map<string, Row>();
  for (const row of [...history, ...canonical]) records.set(row.id, row);
  return [...records.values()].sort(
    (a, b) =>
      a.height - b.height ||
      a.tx_index - b.tx_index ||
      a.log_index - b.log_index,
  );
}
export function SelectedMessage(
  selected: Row | null,
  canonical: Row[],
): Row | undefined {
  if (!selected?.body) return undefined;
  return canonical.find((row) => row.tx_hash === selected.tx_hash);
}
export function RestartMatches(checkpoint: Row | null, state: Row): boolean {
  return !!(
    checkpoint?.head &&
    state.running &&
    state.contract_ready &&
    state.network?.id === checkpoint.id &&
    state.stats?.head === checkpoint.head
  );
}
export function CanSend(
  body: string,
  state: {
    unlocked: boolean;
    running: boolean;
    contractReady: boolean;
    roomOpen: boolean;
    busy: boolean;
    funded?: boolean;
    fundingReady?: boolean;
  },
): boolean {
  return (
    state.unlocked &&
    state.running &&
    state.contractReady &&
    state.roomOpen &&
    !state.busy &&
    state.fundingReady !== false &&
    body.trim().length > 0 &&
    new TextEncoder().encode(body).length <= 512
  );
}
