import React, { useEffect, useRef, useState } from "react";
import { Download, LogOut, Plus, Upload, Wallet } from "lucide-react";
import { bytes, download, short, type Row } from "./api";
import { ReadWalletBackup } from "./wallet";

type Props = { status: Row; busy: boolean; action: (name: string, values?: Row, label?: string) => Promise<any>; onNickname: () => void };
export function WalletPanel({ status: s, busy, action, onNickname }: Props) {
  const [mode, setMode] = useState("login");
  const [title, setTitle] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [backup, setBackup] = useState<Row | null>(null);
  const [fileName, setFileName] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const readTicket = useRef(0);
  const changeMode = (next: string) => { readTicket.current++; setMode(next); };
  const wallets: Row[] = (s.wallet_profiles || []).filter((item: Row) => item.has_wallet);
  const selected = wallets.find(item => item.id === s.selected_wallet_id);
  const account = (s.wallets || []).find((item: Row) => item.id === s.active_wallet);
  useEffect(() => {
    readTicket.current++;
    setPassword(""); setConfirm(""); setBackup(null); setFileName(""); setError(""); setNotice("");
    return () => { readTicket.current++; };
  }, [mode, s.selected_wallet_id, s.unlocked]);
  const passwordValid = bytes(password) >= 8 && bytes(password) <= 256;
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setError(""); setNotice("");
    try {
      if (mode === "login") await action("unlock", { password }, "正在登录钱包");
      if (mode === "create") await action("wallet_add", { title: title.trim(), password }, "正在创建钱包");
      if (mode === "import") await action("wallet_import", { title: title.trim(), password, backup }, "正在导入钱包");
      if (mode === "backup") {
        const encrypted = await action("backup", { password }, "正在导出加密备份");
        download(`NornV2-wallet-${s.selected_wallet_id || "legacy"}.json`, encrypted);
        setPassword(""); setConfirm(""); setNotice("加密钱包备份已导出。");
      }
    } catch (failure) { setError((failure as Error).message); }
  };
  return <div className="wallet-manager">
    <span className="modal-icon"><Wallet size={26} /></span>
    <h2>钱包与登录</h2>
    <p>钱包可以在不同链上使用，余额按链分别计算。</p>
    <label className="field">已保存钱包
      <select aria-label="已保存钱包" value={s.has_wallet ? s.selected_wallet_id : ""} disabled={busy || wallets.length === 0}
        onChange={event => void action("wallet_select", { id: event.target.value }, "正在切换钱包").catch(() => {})}>
        {!s.has_wallet && <option value="">选择钱包</option>}
        {wallets.map(item => <option key={item.id} value={item.id}>{item.title} · {item.id.slice(0, 8)}</option>)}
      </select>
    </label>
    <div className="wallet-session" role="status" aria-label="钱包登录状态">
      <strong>{selected?.title || "尚未添加钱包"}</strong>
      <span>{s.unlocked ? "已登录" : "已退出登录"}</span>
      {account && <code title={account.public_key}>{short(account.public_key, 12)}</code>}
    </div>
    <div className="wallet-manager-actions">
      <button className="button secondary compact" aria-label="新建钱包" disabled={busy} onClick={() => { setTitle(""); changeMode("create"); }}><Plus size={15} />新建钱包</button>
      <button className="button secondary compact" aria-label="导入钱包" disabled={busy} onClick={() => { setTitle(""); changeMode("import"); }}><Upload size={15} />导入钱包</button>
      {s.unlocked && <button className="button secondary compact" aria-label="导出钱包备份" disabled={busy} onClick={() => changeMode("backup")}><Download size={15} />导出备份</button>}
    </div>
    {s.unlocked && mode === "login" && <>
      <button className="button secondary full" disabled={busy} onClick={onNickname}>修改显示昵称</button>
      <button className="button full wallet-logout" aria-label="退出登录" disabled={busy} onClick={() => void action("logout", {}, "正在退出钱包").catch(() => {})}><LogOut size={16} />退出登录</button>
      <p className="small muted">退出后停止签名，当前节点继续同步。</p>
    </>}
    {(!s.unlocked || mode !== "login") && (mode !== "login" || s.has_wallet) && <form onSubmit={submit}>
      {mode !== "login" && <h3>{mode === "create" ? "新建钱包" : mode === "import" ? "导入加密钱包" : "导出加密备份"}</h3>}
      {["create", "import"].includes(mode) && <label className="field">钱包名称
        <input aria-label="钱包名称" value={title} maxLength={40} onChange={event => setTitle(event.target.value)} placeholder="例如：日常钱包、测试钱包" />
      </label>}
      {mode === "import" && <label className="field">加密钱包备份
        <input aria-label="加密钱包备份文件" type="file" accept=".json,application/json" disabled={busy} onChange={async event => {
          const input = event.currentTarget;
          const ticket = ++readTicket.current;
          const current = () => ticket === readTicket.current && input.isConnected;
          const file = input.files?.[0]; setBackup(null); setFileName(""); setError("");
          if (!file) return;
          try {
            if (file.size > 65536) throw Error("钱包备份文件过大。");
            const text = await file.text();
            if (!current()) return;
            setBackup(ReadWalletBackup(text)); setFileName(file.name);
          } catch (failure) { if (current()) { input.value = ""; setError((failure as Error).message); } }
        }} />
        {fileName && <small>{fileName}</small>}
      </label>}
      <label className="field">{mode === "import" ? "备份密码" : mode === "backup" ? "设置备份密码" : mode === "create" ? "设置钱包密码" : "钱包密码"}
        <input aria-label="钱包密码" type="password" value={password} autoComplete={["create", "backup"].includes(mode) ? "new-password" : "current-password"} onChange={event => setPassword(event.target.value)} />
      </label>
      {["create", "backup"].includes(mode) && <label className="field">确认密码
        <input aria-label="确认钱包密码" type="password" autoComplete="new-password" value={confirm} onChange={event => setConfirm(event.target.value)} />
      </label>}
      {error && <div className="alert error" role="alert">{error}</div>}
      {notice && <div className="alert" role="status">{notice}</div>}
      <button className="button primary full" disabled={busy || !password || bytes(password) > 256 ||
        (["create", "backup"].includes(mode) && (!passwordValid || password !== confirm)) ||
        (["create", "import"].includes(mode) && (!title.trim() || bytes(title.trim()) > 160)) || (mode === "import" && !backup)}>
        {mode === "create" ? "创建并登录" : mode === "import" ? "导入并登录" : mode === "backup" ? "下载加密备份" : "登录钱包"}
      </button>
      {mode !== "login" && <button type="button" className="text-button" disabled={busy} onClick={() => changeMode("login")}>返回钱包登录</button>}
    </form>}
    {!s.unlocked && !s.has_wallet && mode === "login" && <p>新建钱包，或导入已有的加密备份。</p>}
  </div>;
}
