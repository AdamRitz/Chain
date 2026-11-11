import type { Row } from "./api";

export const WalletActions = new Set(["lock", "logout", "wallet_lock", "unlock", "wallet_create", "restore", "wallet_add", "wallet_import", "wallet_select", "select_wallet"]);
export function WalletIdentityChanged(previous: Row, next: Row): boolean {
  return previous.wallet_session !== undefined && (
    previous.selected_wallet_id !== next.selected_wallet_id || previous.active_wallet !== next.active_wallet || previous.unlocked !== next.unlocked ||
    (previous.network?.id === next.network?.id && previous.wallet_session !== next.wallet_session));
}
export function ReadWalletBackup(text: string): Row {
  if (new TextEncoder().encode(text).length > 65536) throw Error("钱包备份文件过大。");
  let value: Row;
  try { value = JSON.parse(text); } catch { throw Error("请选择有效的加密钱包 JSON 备份。"); }
  const hex = (field: unknown, min: number, max = min) => typeof field === "string" && field.length >= min && field.length <= max && field.length % 2 === 0 && /^[0-9a-f]+$/i.test(field);
  if (!value || value.version !== 1 || value.kdf !== "argon2id-interactive" || !hex(value.salt, 32) || !hex(value.nonce, 48) || !hex(value.cipher, 32, 32768))
    throw Error("请选择 NornV2 导出的加密钱包备份。");
  return { version: 1, kdf: value.kdf, salt: value.salt, nonce: value.nonce, cipher: value.cipher };
}
