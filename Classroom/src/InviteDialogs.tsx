import { useEffect, useRef, useState } from "react";
import { Copy, Download, LoaderCircle, RefreshCw, ShieldCheck, Users } from "lucide-react";
import { api, download, type Row } from "./api";
import { FirewallSummary } from "./network";

export function InviteShare({ status, action }: { status: Row; action: (name: string, values?: Row, label?: string) => Promise<any> }) {
  const [prepared, setPrepared] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [manual, setManual] = useState("");
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
  const hasGlobalV6 = connectivity.addresses?.some((a: Row) => a.selected && a.family === "IPv6" && a.scope === "internet_candidate" && a.default_route);
  const firewall = status.network_tools?.firewall;
  const allowed = !!firewall?.rules?.some((rule: Row) => rule.enabled === "True" && rule.action === "Allow" && rule.direction === "Inbound" && rule.profile === "Any" && rule.remote?.includes("Any"));
  const ready = !!prepared && !!status.running && !!status.contract_ready && !loading;
  return <>
    <span className="modal-icon"><Users size={26} /></span>
    <h2>邀请朋友加入</h2>
    <p>将邀请直接发给朋友。对方打开 NornV2，点击“加入聊天室”并粘贴即可。</p>
    <div className="invite-summary">
      <strong>{status.network.title}</strong>
      <span>{loading ? "正在检查连接…" : prepared?.invite.scope === "custom" ? "使用自定义入口，等待对方连接验证" : hasGlobalV6 ? "已找到 IPv6 入口，等待对方连接验证" : connectivity.scope === "internet_candidate" ? "已找到公网 IPv4 候选，等待对方连接验证" : connectivity.scope === "none" ? "未找到可分享的网络入口" : "当前适用于同一局域网或可互通的专用网络"}</span>
      {connectivity.scope === "local" && <small>未检测到可用于跨网的本机 IPv6 入口。跨网络加入需要可用公网节点或可互通的专用网络。</small>}
    </div>
    {error && <div className="alert error" role="alert">{error}</div>}
    {notice && <p className="text-teal" role="status">{notice}</p>}
    <button className="button primary full" disabled={!ready} onClick={async () => {
      try {
        const value = Date.now() - Number(prepared?.connectivity.checked_ms) > 30000
          ? await prepare(manual.trim() ? manual.trim().split(/\s+/) : undefined) : prepared;
        if (!value) return;
        await navigator.clipboard.writeText(value.share_text);
        setNotice("邀请已复制，直接发给朋友即可。");
      } catch { setError("无法自动复制，请展开连接详情，手动复制完整邀请。"); }
    }}><Copy size={17} />复制邀请</button>
    <div className="invite-secondary">
      <button className="text-button" disabled={!ready} onClick={() => download("NornV2-聊天室邀请.norninvite", prepared!.file)}><Download size={15} />保存邀请文件</button>
      <button className="text-button" disabled={loading} onClick={() => void prepare(manual.trim() ? manual.trim().split(/\s+/) : undefined)}><RefreshCw size={15} />重新检查</button>
    </div>
    {!allowed && <div className="invite-permission">
      <p>允许朋友主动连接本机节点。接下来 Windows 可能请求配置本应用的权限。</p>
      <button className="button secondary" disabled={status.network_tools?.busy} onClick={() => void action("network_configure", { profile: "Any", remote: "Any" }, "请留意 Windows 权限提示").catch(() => {})}><ShieldCheck size={16} />允许朋友连接</button>
      <small>{FirewallSummary(firewall)}</small>
    </div>}
    <details className="invite-details"><summary>连接详情与高级设置</summary>
      <p className="small muted">有 IPv6 不等于外部已可达。接收者会自动检查入口和聊天室指纹。</p>
      {(connectivity.addresses || []).map((a: Row, i: number) => <p className="small mono" key={i}>{a.interface} · {a.address} · {a.selected ? "已选用" : "未自动选用"} · {a.scope}</p>)}
      <label className="field">已有节点入口（可选）<textarea value={manual} maxLength={2400} onChange={e => { manualValue.current = e.target.value; setManual(e.target.value); setPrepared(null); if (!e.target.value.trim()) void prepare(); }} placeholder="通常留空。公网映射、域名或专用网络入口，每行一个。" /></label>
      {manual.trim() && <button className="button secondary" disabled={loading} onClick={() => void prepare(manual.trim().split(/\s+/))}>使用这些入口</button>}
      {prepared && <label className="field">完整邀请<textarea readOnly value={prepared.share_text} onFocus={e => e.target.select()} /></label>}
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
    <label className="field">把朋友发来的邀请粘贴到这里
      <textarea aria-label="聊天室邀请" value={text} maxLength={524288} onChange={e => setText(e.target.value)} placeholder="可以粘贴整段邀请文字，也支持原来的 JSON 邀请。" />
    </label>
    <label className="file-link">也可以打开邀请文件<input type="file" accept=".norninvite,.json" onChange={async e => {
      const file = e.target.files?.[0]; if (!file) return;
      if (file.size > 524288) { setError("邀请文件过大，请让朋友重新发送。"); return; }
      setText(await file.text());
    }} /></label>
    </div>
    {error && <div className="alert error" role="alert">{error}</div>}
    {preview && <div className="invite-summary" role="status"><strong>{preview.title}</strong>
      <span>{preview.phase === "checking" ? <><LoaderCircle size={15} className="spin" />正在检查连接和聊天室…</> : preview.phase === "ready" ? preview.same_chain ? "这是当前聊天室，将更新连接入口。" : status.network ? "检查通过，将切换到这个聊天室。原有记录会保留。" : "连接检查通过，点击下方加入即可。" : preview.error}</span>
      {preview.phase !== "checking" && <button className="text-button" onClick={() => void start()}>重新检查</button>}
      {preview.phase === "ready" && <button className="text-button" onClick={() => { id.current = ""; setText(""); setPreview(null); onPreview(null); }}>更换邀请</button>}
    </div>}
  </>;
}
