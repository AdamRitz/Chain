import { useEffect, useRef, useState } from "react";
import { Copy, Download, LoaderCircle, RefreshCw, ShieldCheck, Users } from "lucide-react";
import { api, download, type Row } from "./api";
import { FirewallSummary, IPv6Summary, InviteEndpointText } from "./network";
import { ShareAddress } from "./invite";

export function InviteShare({ status, action }: { status: Row; action: (name: string, values?: Row, label?: string) => Promise<any> }) {
  const [prepared, setPrepared] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [manual, setManual] = useState("");
  const [selectedAddress, setSelectedAddress] = useState("");
  const manualValue = useRef("");
  const generation = useRef(0);
  const prepare = async (seeds?: string[]) => {
    const current = ++generation.current;
    setLoading(true); setError("");
    try {
      const value = await api("invite_prepare", seeds ? { seeds } : {});
      if (current === generation.current) setPrepared(value);
      return value;
    } catch (failure) {
      if (current === generation.current) { setPrepared(null); setError((failure as Error).message); }
    } finally { if (current === generation.current) setLoading(false); }
  };
  useEffect(() => {
    void prepare();
    if (!status.network_tools?.busy) void action("network_status", {}, "正在检查连接权限").catch(() => {});
    const timer = setInterval(() => { if (!manualValue.current.trim()) void prepare(); }, 15000);
    return () => { clearInterval(timer); generation.current++; };
  }, [status.network.id]);
  const connectivity = prepared?.connectivity || status.network_addresses || {};
  const addresses: string[] = prepared?.invite.seeds || [];
  const address = ShareAddress(prepared, selectedAddress);
  const chosen = connectivity.addresses?.find((a: Row) => a.endpoint === address);
  const hasGlobalV6 = chosen?.family === "IPv6" && chosen.scope === "internet_candidate" && chosen.default_route;
  const firewall = status.network_tools?.firewall;
  const allowed = !!firewall?.rules?.some((rule: Row) => rule.enabled === "True" && rule.action === "Allow" && rule.direction === "Inbound" && rule.profile === "Any" && rule.remote?.includes("Any"));
  const ready = !!prepared && !!status.running && !!status.contract_ready && !loading;
  return <>
    <span className="modal-icon"><Users size={26} /></span>
    <h2>邀请朋友加入</h2>
    <p>复制节点地址发给对方，在“加入聊天室”中粘贴。链配置和聊天记录会自动获取。</p>
    <div className="invite-summary">
      <strong>{status.network.title}</strong>
      <span>{loading ? "正在检查连接…" : prepared?.invite.scope === "custom" ? "使用自定义入口，等待对方连接验证" : hasGlobalV6 ? "已找到 IPv6 入口，等待对方连接验证" : chosen?.scope === "internet_candidate" ? "已找到公网地址候选，等待对方连接验证" : connectivity.scope === "none" ? "未找到可分享的网络入口" : "当前地址适用于同一局域网或可互通的专用网络"}</span>
      {connectivity.scope === "local" && <small>未检测到可用于跨网的本机 IPv6 入口。跨网络加入需要可用公网节点或可互通的专用网络。</small>}
    </div>
    {address && <label className="field">节点地址{addresses.length > 1
      ? <select aria-label="分享的节点地址" value={address} onChange={e => setSelectedAddress(e.target.value)}>{addresses.map(entry => <option key={entry} value={entry}>{entry}</option>)}</select>
      : <input aria-label="分享的节点地址" readOnly value={address} onFocus={e => e.target.select()} />}</label>}
    {error && <div className="alert error" role="alert">{error}</div>}
    {notice && <p className="text-teal" role="status">{notice}</p>}
    <button className="button primary full" disabled={!ready} onClick={async () => {
      try {
        const value = Date.now() - Number(prepared?.connectivity.checked_ms) > 30000
          ? await prepare(manual.trim() ? manual.trim().split(/\s+/) : undefined) : prepared;
        if (!value) return;
        await navigator.clipboard.writeText(ShareAddress(value, selectedAddress));
        setNotice("节点地址已复制。");
      } catch { setError("无法自动复制，请手动复制上方节点地址。"); }
    }}><Copy size={17} />复制地址</button>
    <div className="invite-secondary">
      <button className="text-button" disabled={loading} onClick={() => void prepare(manual.trim() ? manual.trim().split(/\s+/) : undefined)}><RefreshCw size={15} />重新检查</button>
    </div>
    {!allowed && <div className="invite-permission">
      <p>允许朋友主动连接本机节点。接下来 Windows 可能请求配置本应用的权限。</p>
      <button className="button secondary" disabled={status.network_tools?.busy} onClick={() => void action("network_configure", { profile: "Any", remote: "Any" }, "请留意 Windows 权限提示").catch(() => {})}><ShieldCheck size={16} />允许朋友连接</button>
      <small>{FirewallSummary(firewall)}</small>
    </div>}
    <details className="invite-details"><summary>连接详情与高级设置</summary>
      <p className="small muted">有 IPv6 不等于外部已可达。接收者会自动检查入口和聊天室指纹。</p>
      <p className="small">IPv6：{IPv6Summary(connectivity)}</p>
      {(connectivity.addresses || []).map((a: Row, i: number) => <p className="small mono" key={i}>{a.interface} · {a.address} · {a.selected ? "已选用" : "未自动选用"} · {a.scope}</p>)}
      <label className="field">已有节点入口（可选）<textarea value={manual} maxLength={2400} onChange={e => { manualValue.current = e.target.value; setManual(e.target.value); setPrepared(null); if (!e.target.value.trim()) void prepare(); }} placeholder="通常留空。公网映射、域名或专用网络入口，每行一个。" /></label>
      {manual.trim() && <button className="button secondary" disabled={loading} onClick={() => void prepare(manual.trim().split(/\s+/))}>使用这些入口</button>}
      {prepared && <details><summary>旧版邀请码兼容</summary>
        <p className="small muted">对方节点需要更新至 0.4.0 或更新版本才能用地址加入。连接旧版节点可使用完整邀请码。</p>
        <label className="field">完整邀请码<textarea readOnly value={prepared.token} onFocus={e => e.target.select()} /></label>
        <button className="text-button" disabled={!ready} onClick={() => download("NornV2-聊天室邀请.norninvite", prepared.file)}><Download size={15} />保存旧版邀请文件</button>
      </details>}
    </details>
  </>;
}

export function InviteInput({ text, setText, status, onPreview }: { text: string; setText: (value: string) => void; status: Row; onPreview: (preview: Row | null) => void }) {
  const [preview, setPreview] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const request = useRef(0);
  const id = useRef("");
  const start = async () => {
    const current = ++request.current; id.current = ""; setPreview(null); onPreview(null); setError("");
    if (!text.trim()) return;
    try {
      const result = await api("invite_preview", { invite: text });
      if (current !== request.current) return;
      id.current = result.id; setPreview(result); onPreview(result);
    } catch (failure) { if (current === request.current) setError((failure as Error).message); }
  };
  useEffect(() => {
    request.current++; id.current = ""; setPreview(null); onPreview(null); setError("");
    const timer = setTimeout(() => void start(), 450);
    return () => { clearTimeout(timer); request.current++; };
  }, [text]);
  useEffect(() => {
    const value = status.invite_preview;
    if (id.current && value?.id === id.current) { setPreview(value); onPreview(value); }
  }, [status.invite_preview]);
  return <>
    <div hidden={preview?.phase === "ready"}>
    <label className="field">节点地址
      <textarea aria-label="节点地址或旧邀请码" value={text} maxLength={524288} onChange={e => setText(e.target.value)} placeholder="IPv4:端口 或 [IPv6]:端口" />
    </label>
    <details><summary>使用旧版邀请码或邀请文件</summary><p className="small muted">完整邀请码可直接粘贴到上方。</p><label className="file-link">打开邀请文件<input type="file" accept=".norninvite,.json" onChange={async e => {
      const file = e.target.files?.[0]; if (!file) return;
      if (file.size > 524288) { setError("邀请文件过大，请让朋友重新发送。"); return; }
      setText(await file.text());
    }} /></label></details>
    </div>
    {error && <div className="alert error" role="alert">{error}</div>}
    {preview && <div className="invite-summary" role="status"><strong>{preview.title}</strong>
      <span>{preview.phase === "checking" ? <><LoaderCircle size={15} className="spin" />正在连接节点并获取链配置…</> : preview.phase === "ready" ? preview.same_chain ? "已连接当前聊天室，将更新入口。" : status.network ? "已连接，加入后自动同步。原有聊天室记录会保留。" : "已连接，加入后自动同步聊天记录。" : preview.error}</span>
      {preview.phase !== "checking" && <button className="text-button" onClick={() => void start()}>重新检查</button>}
      {preview.phase === "error" && <details><summary>连接详情</summary>{(preview.endpoints || []).map((entry: Row) => <p className="small mono" key={entry.endpoint}>{entry.endpoint} · {entry.family} · {InviteEndpointText(entry)}</p>)}</details>}
      {preview.phase === "ready" && <button className="text-button" onClick={() => { id.current = ""; setText(""); setPreview(null); onPreview(null); }}>更换地址</button>}
    </div>}
  </>;
}
