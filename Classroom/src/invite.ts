import type { Row } from "./api";

export function ShareAddresses(prepared: Row | null): string[] {
  const entries: unknown[] = prepared?.invite?.seeds || [];
  return [...new Set(entries.filter((entry): entry is string => typeof entry === "string" && !!entry.trim()))];
}

export function ShareAddress(prepared: Row | null, selected = ""): string {
  return ShareAddresses(prepared).includes(selected) ? selected : "";
}

export function ShareAddressLabel(prepared: Row | null, address: string): string {
  const candidate = prepared?.connectivity?.addresses?.find((entry: Row) => entry.endpoint === address);
  const scope = prepared?.invite?.scope === "custom" ? "自定义入口" :
    ({ internet_candidate: "公网候选", lan: "局域网", private: "专用网络" } as Record<string, string>)[candidate?.scope] || "节点入口";
  return `${address} · ${scope}${candidate?.interface ? ` · ${candidate.interface}` : ""}`;
}

export function LastConnection(status: Row): string {
  return typeof status.last_connection === "string" ? status.last_connection.trim() : "";
}

export function JoinText(current: string, status: Row): string {
  return current.trim() ? current : LastConnection(status);
}

export function ConnectionText(status: Row): string {
  const phase = status.connection_state?.phase;
  return phase === "ready" ? "聊天室已就绪" : phase === "syncing" ? "正在同步聊天记录" :
    phase === "connecting" ? "正在连接聊天室" : "节点未运行";
}
export function ReceiptText(status: Row, publicKey: string): string {
  return `NornV2「${status.network?.title || "聊天室"}」聊天费用收款\n聊天室指纹：${status.network?.genesis_root || ""}\n收款地址：${publicKey}\n请在同一聊天室内普通转账。`;
}
export function CanJoinPreview(preview: Row | null, id: string): boolean {
  return !!id && preview?.id === id && preview?.phase === "ready" &&
    Date.now() - Number(preview.checked_ms) < 60000;
}
