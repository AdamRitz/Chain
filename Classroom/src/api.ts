export type Row = Record<string, any>;
import { NetworkRequest, NetworkView, StaleViewError } from "./model.ts";
export const apiView = new NetworkView();
let token = location.hash.slice(1);
// Keep the local session token out of copied URLs and referrers.
if (token) {
  sessionStorage.setItem("chain-session", token);
  history.replaceState(null, "", location.pathname);
}
token ||= sessionStorage.getItem("chain-session") || "";
export async function api(action: string, values: Row = {}): Promise<any> {
  const scope = apiView.Capture();
  try {
    const response = await fetch("/api", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(NetworkRequest(action, values, scope)),
      signal: AbortSignal.timeout(60000),
    });
    const data = await response.json();
    if (!apiView.Current(scope)) throw new StaleViewError();
    if (!response.ok || data.error) throw Error(data.error || "本地服务连接失败");
    return data;
  } catch (failure) {
    if (!apiView.Current(scope)) throw new StaleViewError();
    throw failure;
  }
}
export const bytes = (value: string) => new TextEncoder().encode(value).length;
export const short = (value: string = "", length = 9) =>
  value.length > length * 2
    ? `${value.slice(0, length)}…${value.slice(-6)}`
    : value;
export const number = (value: string | number = 0) => {
  try {
    return BigInt(value).toLocaleString("zh-CN");
  } catch {
    return String(value);
  }
};
export function download(name: string, value: unknown) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
