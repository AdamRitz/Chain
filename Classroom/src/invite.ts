import type { Row } from "./api";

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
