import React, { useEffect, useRef, useState } from "react";
import { version } from "../package.json";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowDownLeft,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Box,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleHelp,
  Copy,
  Download,
  ExternalLink,
  Fingerprint,
  Globe2,
  GraduationCap,
  Hash,
  KeyRound,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  MessageCircle,
  Network,
  Play,
  Plus,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  Square,
  Terminal,
  Users,
  Wallet,
  X,
} from "lucide-react";
import { api, bytes, download, number, Row, short } from "./api";
import chatSource from "../../Contracts/ClassroomChat.sol?raw";
import artifact from "../../Contracts/ClassroomChat.compiled.json";
import "./style.css";
import {
  MergeMessages,
  CanSend,
  SelectedMessage,
  RestartMatches,
} from "./model";

type View =
  | "home"
  | "wallet"
  | "blocks"
  | "network"
  | "chat"
  | "learn"
  | "teacher";
const views: { id: View; title: string; icon: typeof Box; caption: string }[] =
  [
    {
      id: "home",
      title: "实验概览",
      icon: Activity,
      caption: "从一条交易开始，认识你的区块链。",
    },
    {
      id: "wallet",
      title: "我的钱包",
      icon: Wallet,
      caption: "自己保管密钥，亲手签名每一笔交易。",
    },
    {
      id: "blocks",
      title: "区块浏览器",
      icon: Layers3,
      caption: "打开区块，看看交易如何连接成一条链。",
    },
    {
      id: "network",
      title: "节点与网络",
      icon: Network,
      caption: "观察每个节点，理解同步与共同的账本。",
    },
    {
      id: "chat",
      title: "链上聊天室",
      icon: MessageCircle,
      caption: "说一句话，让它成为一笔真实的合约交易。",
    },
    {
      id: "learn",
      title: "实验引导",
      icon: BookOpen,
      caption: "完成六个小实验，把概念变成亲身体验。",
    },
    {
      id: "teacher",
      title: "课堂管理",
      icon: GraduationCap,
      caption: "分享课堂、发放测试币、管理聊天室。",
    },
  ];
const stateText: Row = {
  submitted: "等待入块",
  included: "本机已上链",
  reorganized: "退出主链，待重新入块",
};
const selectors = artifact.contracts.ClassroomChat.evm.methodIdentifiers as Row;

function App() {
  const [s, setS] = useState<Row>({});
  const [view, setView] = useState<View>("home");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [modal, setModal] = useState("");
  const [password, setPassword] = useState("");
  const [title, setTitle] = useState("我的区块链课堂");
  const [invite, setInvite] = useState("");
  const [restore, setRestore] = useState<Row | null>(null);
  const [body, setBody] = useState("");
  const [room, setRoom] = useState(1);
  const [selected, setSelected] = useState<Row | null>(null);
  const [block, setBlock] = useState<Row | null>(null);
  const [height, setHeight] = useState("");
  const [receiver, setReceiver] = useState("");
  const [amount, setAmount] = useState("100");
  const [fundAmount, setFundAmount] = useState("10000000");
  const [nick, setNick] = useState("");
  const [endpoints, setEndpoints] = useState("");
  const [exported, setExported] = useState<Row | null>(null);
  const [roomName, setRoomName] = useState("");
  const [member, setMember] = useState("");
  const [historyRows, setHistoryRows] = useState<Row[]>([]);
  const [historyOffset, setHistoryOffset] = useState(50);
  const [restartCheckpoint, setRestartCheckpoint] = useState<Row | null>(null);
  const [restartVerified, setRestartVerified] = useState("");
  const [samples, setSamples] = useState<{ t: number; v: number }[]>([]);
  const polling = useRef(false);
  const lastReorg = useRef<number | undefined>(undefined);
  const chatEnd = useRef<HTMLDivElement>(null);
  const refresh = async () => {
    if (polling.current) return;
    polling.current = true;
    try {
      const value = await api("status");
      setS(value);
      setSamples((old) =>
        [...old, { t: Date.now(), v: value.recent_tps || 0 }].slice(-30),
      );
      if (
        lastReorg.current !== undefined &&
        value.reorg_count !== lastReorg.current
      ) {
        setHistoryRows([]);
        setHistoryOffset(50);
        setNotice("检测到链重组，聊天历史已按当前主链刷新。");
      }
      lastReorg.current = value.reorg_count;
    } catch (e) {
      setS((old) => ({ ...old, running: false, connection_error: true }));
      setError((e as Error).message);
    } finally {
      polling.current = false;
    }
  };
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 1200);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (notice) {
      const t = setTimeout(() => setNotice(""), 5000);
      return () => clearTimeout(t);
    }
  }, [notice]);
  const action = async (name: string, values: Row = {}, label = "正在处理") => {
    setBusy(label);
    setError("");
    try {
      const result = await api(name, values);
      if (name === "stop" && !result.running && result.network?.id) {
        setRestartCheckpoint({
          id: result.network.id,
          head: result.stats?.head,
        });
      }
      if (result.has_wallet !== undefined) setS(result);
      else await refresh();
      return result;
    } catch (e) {
      setError((e as Error).message);
      throw e;
    } finally {
      setBusy("");
    }
  };
  const run = (name: string, values: Row = {}, label?: string) =>
    void action(name, values, label).catch(() => {});
  const copy = (value: string) => {
    navigator.clipboard
      .writeText(value)
      .then(() => setNotice("已复制"))
      .catch(() => setError("复制失败，请手动选择文字复制。"));
  };
  const wallet = (s.wallets || []).find((w: Row) => w.id === s.active_wallet);
  const network = s.network;
  const rooms: Row[] = s.rooms || [];
  const currentRoom = rooms.find((r) => r.id === room);
  const allMessages = MergeMessages(historyRows, s.messages || []);
  const messages = allMessages.filter((m) => m.room === room);
  const selectedMessage = SelectedMessage(selected, messages);
  const pending = (s.outbox || []).filter(
    (x: Row) =>
      x.body &&
      x.room === room &&
      !allMessages.some((m) => m.tx_hash === x.hash),
  );
  const nodes: Row[] = s.nodes || [];
  const online = nodes.filter((n) => n.online);
  const same = online.filter((n) => n.head === s.stats?.head).length;
  const selectedView = views.find((v) => v.id === view)!;
  const openBlock = async (h: number, offset = 0) => {
    try {
      const result = await action(
        "block",
        { height: h, offset, limit: 10 },
        "读取区块",
      );
      setBlock(result);
      setView("blocks");
    } catch {}
  };
  const needWallet = (next: string) => {
    if (!s.unlocked) {
      setModal(s.has_wallet ? "unlock" : "wallet");
      return;
    }
    setModal(next);
  };
  const enter = async (joining = false, mode = "demo") => {
    try {
      await action(
        joining ? "join" : "create",
        joining ? { invite: JSON.parse(invite) } : { mode, title },
        joining ? "正在连接课堂并同步区块" : "正在启动节点并部署聊天室",
      );
      setModal("");
      setHistoryRows([]);
      setHistoryOffset(50);
      setView("home");
      setBody("");
      setSelected(null);
      setBlock(null);
      setRoom(1);
      setNotice(
        joining
          ? "课堂节点已启动，合约同步完成后即可聊天。"
          : "教学网络已就绪，聊天室已通过真实交易部署。",
      );
    } catch (e) {
      if (e instanceof SyntaxError) setError("课堂配置不是有效的 JSON 文件。");
    }
  };
  const send = async () => {
    try {
      await action("message", { room, body }, "签名并提交消息");
      setBody("");
      setNotice("消息已提交，正在等待入块。");
    } catch {}
  };
  const receipt = (tx: Row) => {
    setSelected(tx);
    setModal("transaction");
  };
  const balance = wallet?.balance || "0";
  const canSend = CanSend(body, {
    unlocked: !!s.unlocked,
    running: !!s.running,
    contractReady: !!s.contract_ready,
    roomOpen: !!currentRoom?.open,
    busy: !!busy,
  });
  useEffect(() => {
    if (RestartMatches(restartCheckpoint, s)) {
      setRestartVerified(s.network.id);
      setRestartCheckpoint(null);
    }
  }, [s, restartCheckpoint]);
  useEffect(() => {
    if (view === "chat" && chatEnd.current?.parentElement) {
      const scroll = chatEnd.current.parentElement;
      scroll.scrollTop = scroll.scrollHeight;
    }
  }, [view, messages.at(-1)?.id, pending.length]);
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <Layers3 size={25} />
          </span>
          <div>
            NornV2<span>区块链实验室</span>
          </div>
        </div>
        <nav>
          {views
            .filter((v) => v.id !== "teacher" || network?.teacher)
            .map((v) => (
              <button
                key={v.id}
                aria-label={v.title}
                title={v.title}
                className={view === v.id ? "nav active" : "nav"}
                onClick={() => setView(v.id)}
              >
                <v.icon size={19} />
                <span>{v.title}</span>
                {v.id === "chat" && <span className="nav-dot" />}
              </button>
            ))}
        </nav>
        <div className="sidebar-note">
          <GraduationCap size={23} />
          <b>你的第一条区块链</b>
          <p>这里的转账和聊天都发生在真实的教学链上。</p>
          <button onClick={() => setView("learn")}>
            开始探索 <ArrowRight size={14} />
          </button>
        </div>
        <div className="sidebar-foot">
          <span className="tiny-dot" /> 教学版 {version}
          <span>本地签名 · 测试币</span>
        </div>
      </aside>
      <main>
        <header>
          <div className="breadcrumb">
            NornV2 实验室 <ChevronRight size={14} />
            <span>{selectedView.title}</span>
          </div>
          <div className="header-right">
            <span className={"connection " + (s.running ? "connected" : "")}>
              <i />
              {s.connection_error
                ? "本地服务连接中断"
                : s.running
                  ? "本地节点运行中"
                  : "节点未启动"}
            </span>
            <button
              className="icon-button"
              aria-label="帮助"
              onClick={() => setView("learn")}
            >
              <CircleHelp size={19} />
            </button>
            <button
              className="avatar"
              title="钱包与密码"
              onClick={() =>
                setModal(
                  s.unlocked ? "account" : s.has_wallet ? "unlock" : "wallet",
                )
              }
            >
              {s.unlocked ? <Wallet size={18} /> : <LockKeyhole size={18} />}
            </button>
          </div>
        </header>
        <div className="content">
          <div className="page-title">
            <div>
              <div className="eyebrow">LEARN BY BUILDING</div>
              <h1>{selectedView.title}</h1>
              <p>{selectedView.caption}</p>
            </div>
            <div className="page-actions">
              {network && (
                <>
                  <button
                    className="button secondary"
                    disabled={!!busy}
                    onClick={() =>
                      run(
                        s.running ? "stop" : "start",
                        {},
                        s.running ? "正在安全停止节点" : "正在恢复节点",
                      )
                    }
                  >
                    {s.running ? <Square size={15} /> : <Play size={15} />}{" "}
                    {s.running ? "停止节点" : "恢复节点"}
                  </button>
                  <button
                    className="button secondary"
                    onClick={() => needWallet("choose")}
                  >
                    <Plus size={16} /> 新实验
                  </button>
                </>
              )}
            </div>
          </div>
          {error && !modal && (
            <div className="alert error" role="alert">
              <span>{error}</span>
              <button aria-label="关闭错误" onClick={() => setError("")}>
                <X size={16} />
              </button>
            </div>
          )}
          {s.last_error && (
            <div className="alert warning">节点观测提示：{s.last_error}</div>
          )}
          {!network && view !== "learn" && (
            <section className="welcome">
              <div className="welcome-illustration">
                <div className="orb a">
                  <Box />
                </div>
                <div className="orb b">
                  <Box />
                </div>
                <div className="orb c">
                  <Box />
                </div>
                <svg viewBox="0 0 280 150">
                  <path d="M60 100 L140 42 L220 100 Z" />
                </svg>
              </div>
              <span className="pill teal">不需要写代码，从这里开始</span>
              <h2>把区块链，变成看得见的实验。</h2>
              <p>创建一个三节点的小世界，或者和同学加入同一间课堂。</p>
              <div className="welcome-options">
                <button onClick={() => needWallet("demo")}>
                  <span className="option-icon">
                    <Box />
                  </span>
                  <b>开始本地实验</b>
                  <p>自动创建 3 个节点，钱包和聊天室一步就绪。</p>
                  <span>
                    适合第一次体验 <ArrowRight size={17} />
                  </span>
                </button>
                <button onClick={() => needWallet("join")}>
                  <span className="option-icon blue">
                    <Users />
                  </span>
                  <b>加入课堂</b>
                  <p>导入教师发来的课堂配置，和同学一起聊天。</p>
                  <span>
                    连接真实的课堂网络 <ArrowRight size={17} />
                  </span>
                </button>
                <button onClick={() => needWallet("teacher")}>
                  <span className="option-icon amber">
                    <GraduationCap />
                  </span>
                  <b>创建教学课堂</b>
                  <p>启动可供其他电脑连接的网络，分享课堂入口。</p>
                  <span>
                    教师与小组组织者 <ArrowRight size={17} />
                  </span>
                </button>
              </div>
              <div className="welcome-hint">
                <ShieldCheck size={16} />{" "}
                私钥只保存在你的电脑上，聊天记录随链同步。
              </div>
            </section>
          )}
          {view === "home" && network && (
            <>
              <section className="network-banner">
                <div className="network-badge">
                  <Globe2 size={24} />
                </div>
                <div>
                  <span className="small muted">当前实验网络</span>
                  <h2>{network.title}</h2>
                  <span className="mono">
                    创世指纹 {short(network.genesis_root, 12)}
                  </span>
                </div>
                <span className="pill">
                  {network.mode === "demo"
                    ? "单机 · 三节点"
                    : network.mode === "student"
                      ? "课堂 · 学生节点"
                      : "课堂 · 教师节点"}
                </span>
                <div className="banner-tip">
                  <ShieldCheck size={18} />
                  <span>
                    每一次操作
                    <br />
                    <b>都能在链上找到依据</b>
                  </span>
                </div>
              </section>
              <div className="metrics">
                <Metric
                  icon={Layers3}
                  title="当前区块高度"
                  value={number(s.stats?.height || 0)}
                  foot="当前节点的规范链"
                />
                <Metric
                  icon={ArrowUpRight}
                  title="累计链上交易"
                  value={number(s.stats?.chain_transactions || 0)}
                  foot="按当前主链计数"
                />
                <Metric
                  icon={Activity}
                  title="最近窗口交易速率"
                  value={(s.recent_tps || 0).toFixed(1)}
                  unit="TPS"
                  foot="教学观测值，非性能基准"
                />
                <Metric
                  icon={Network}
                  title="本机在线节点"
                  value={`${online.length} / ${network.ports.length}`}
                  foot={
                    same > 1
                      ? `${same} 个节点最近观测链头相同`
                      : "可前往网络页查看连接"
                  }
                />
              </div>
              <div className="two-columns">
                <section className="panel">
                  <PanelTitle
                    icon={Network}
                    title="账本正在怎样同步？"
                    right={
                      <button
                        className="text-button"
                        onClick={() => setView("network")}
                      >
                        查看网络 <ArrowRight size={14} />
                      </button>
                    }
                  />
                  <Topology nodes={nodes} />
                  <div className="panel-caption">
                    <span className="tiny-dot" />{" "}
                    显示真实本地进程与连接，链头相同表示最近观测账本一致。
                  </div>
                </section>
                <section className="panel">
                  <PanelTitle
                    icon={Activity}
                    title="最近的链上活动"
                    right={<span className="small muted">约每 1.2 秒更新</span>}
                  />
                  <Sparkline samples={samples} />
                  <div className="chart-caption">
                    <span>近 30 次观测</span>
                    <span>Transactions Per Second</span>
                  </div>
                  <div className="insight">
                    <BookOpen size={18} />
                    <span>
                      没有新交易时可能没有新区块；这不等于节点停止。出块间隔与交易确认延迟也不同。
                    </span>
                  </div>
                </section>
              </div>
              <section className="panel">
                <PanelTitle
                  icon={Box}
                  title="最近区块"
                  right={
                    <button
                      className="text-button"
                      onClick={() => setView("blocks")}
                    >
                      打开浏览器 <ArrowRight size={14} />
                    </button>
                  }
                />
                <BlockTable
                  blocks={(s.blocks || []).slice(0, 5)}
                  onSelect={(h) => openBlock(h)}
                />
              </section>
              <div className="lesson-strip">
                <div>
                  <span className="pill teal">试试下一步</span>
                  <h3>发一句话，追踪它怎样进入区块。</h3>
                  <p>从聊天气泡打开交易详情，观察合约执行、Gas 与区块位置。</p>
                </div>
                <button
                  className="button primary"
                  onClick={() => setView("chat")}
                >
                  <MessageCircle size={17} /> 打开聊天室{" "}
                  <ArrowRight size={17} />
                </button>
              </div>
            </>
          )}
          {view === "wallet" && network && (
            <>
              <div className="two-columns">
                <section className="wallet-card">
                  <span>我的教学钱包</span>
                  <div className="wallet-card-top">
                    <h2>
                      {s.active_wallet === "teacher"
                        ? "教师"
                        : s.active_wallet === "bob"
                          ? "Bob"
                          : "Alice"}
                    </h2>
                    <Wallet size={30} />
                  </div>
                  <div className="balance">
                    {s.unlocked ? number(balance) : "已锁定"}
                    <span>测试币</span>
                  </div>
                  <label>原生账户公钥 · Ed25519</label>
                  <button
                    className="key-copy mono"
                    onClick={() => copy(wallet?.public_key || "")}
                  >
                    {short(wallet?.public_key, 17)}
                    <Copy size={15} />
                  </button>
                  <div className="wallet-bottom">
                    <span>已确认序号 {wallet?.nonce || "0"}</span>
                    <span>仅用于本教学网络</span>
                  </div>
                </section>
                <section className="panel padded">
                  <PanelTitle icon={Send} title="发送一笔转账" />
                  <label className="field">
                    收款人公钥
                    <input
                      aria-label="收款人公钥"
                      value={receiver}
                      onChange={(e) => setReceiver(e.target.value.trim())}
                      placeholder="64 位十六进制公钥"
                    />
                  </label>
                  <label className="field">
                    金额（测试币最小单位）
                    <input
                      aria-label="转账金额"
                      inputMode="numeric"
                      value={amount}
                      onChange={(e) => setAmount(e.target.value)}
                    />
                  </label>
                  <div className="quick-wallets">
                    {(s.wallets || [])
                      .filter((w: Row) => w.id !== s.active_wallet)
                      .map((w: Row) => (
                        <button
                          key={w.id}
                          onClick={() => setReceiver(w.public_key)}
                        >
                          转给 {w.id} <ArrowUpRight size={12} />
                        </button>
                      ))}
                  </div>
                  <button
                    className="button primary full"
                    disabled={
                      !s.unlocked ||
                      !s.running ||
                      !!busy ||
                      !/^[0-9a-fA-F]{64}$/.test(receiver) ||
                      !/^\d+$/.test(amount) ||
                      BigInt(amount || "0") === 0n
                    }
                    onClick={() =>
                      run("transfer", { receiver, amount }, "签名并提交转账")
                    }
                  >
                    <Send size={16} /> 本地签名并发送
                  </button>
                </section>
              </div>
              <section className="panel padded">
                <PanelTitle icon={Fingerprint} title="认识两个地址" />
                <p>
                  上面的 32 字节公钥用于原生转账。下面的 20 字节 EVM
                  地址用于合约身份，它们属于同一个钱包。
                </p>
                <code className="code-line">
                  EVM 地址：{wallet?.evm_address || "解锁钱包后显示"}
                </code>
                <div className="wallet-tools">
                  <label>
                    切换演示身份
                    <select
                      value={s.active_wallet || "alice"}
                      disabled={!s.unlocked}
                      onChange={(e) =>
                        run("select_wallet", { wallet: e.target.value })
                      }
                    >
                      {(s.wallets || []).map((w: Row) => (
                        <option key={w.id} value={w.id}>
                          {w.id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="button secondary"
                    onClick={() => setModal("backup")}
                  >
                    <Download size={16} /> 加密备份
                  </button>
                  <button
                    className="button secondary"
                    onClick={() => run("lock")}
                  >
                    <LockKeyhole size={16} /> 锁定钱包
                  </button>
                </div>
              </section>
              <section className="panel">
                <PanelTitle icon={ArrowUpRight} title="本机发送记录" />
                <Outbox
                  rows={s.outbox || []}
                  onSelect={receipt}
                  onRetry={(hash) => run("retry", { hash }, "重发相同签名交易")}
                />
              </section>
            </>
          )}
          {view === "blocks" && network && (
            <>
              <div className="search-row">
                <div className="input-icon">
                  <Search size={18} />
                  <input
                    aria-label="区块高度"
                    value={height}
                    onChange={(e) => setHeight(e.target.value)}
                    placeholder="输入区块高度，例如 2"
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && /^\d+$/.test(height))
                        openBlock(Number(height));
                    }}
                  />
                </div>
                <button
                  className="button primary"
                  disabled={!/^\d+$/.test(height) || !s.running}
                  onClick={() => openBlock(Number(height))}
                >
                  查询区块
                </button>
              </div>
              <section className="panel">
                <PanelTitle
                  icon={Layers3}
                  title="当前规范链"
                  right={
                    <span className="small muted">
                      索引进度 {s.indexed_height || 0} / {s.stats?.height || 0}
                    </span>
                  }
                />
                <BlockTable
                  blocks={s.blocks || []}
                  onSelect={(h) => openBlock(h)}
                />
              </section>
              {block && (
                <section className="panel padded">
                  <PanelTitle
                    icon={Box}
                    title={`区块 #${block.height}`}
                    right={
                      <button
                        className="icon-button"
                        onClick={() => setBlock(null)}
                        aria-label="关闭区块详情"
                      >
                        <X size={18} />
                      </button>
                    }
                  />
                  <dl className="details">
                    <dt>区块哈希</dt>
                    <dd className="mono">{block.hash}</dd>
                    <dt>父区块哈希</dt>
                    <dd className="mono">{block.parent}</dd>
                    <dt>Merkle 根</dt>
                    <dd className="mono">{block.merkle_root}</dd>
                    <dt>交易数量</dt>
                    <dd>{block.count}</dd>
                  </dl>
                  {block.transactions.map((tx: Row) => (
                    <button
                      className="transaction-row"
                      key={tx.hash}
                      onClick={() =>
                        receipt({
                          ...tx,
                          height: block.height,
                          block_hash: block.hash,
                        })
                      }
                    >
                      <span className="tx-icon">
                        <ArrowUpRight size={16} />
                      </span>
                      <div>
                        <b>
                          {tx.kind === "transfer"
                            ? "原生转账"
                            : tx.kind === "deploy"
                              ? "部署合约"
                              : "调用合约"}
                        </b>
                        <span className="mono">{short(tx.hash, 16)}</span>
                      </div>
                      <span className="small muted">序号 {tx.nonce}</span>
                      <ChevronRight size={16} />
                    </button>
                  ))}
                  <div className="pagination">
                    <button
                      disabled={block.offset === 0}
                      onClick={() =>
                        openBlock(block.height, Math.max(0, block.offset - 10))
                      }
                    >
                      上一页
                    </button>
                    <span>
                      {block.count ? block.offset + 1 : 0}–{block.next} /{" "}
                      {block.count}
                    </span>
                    <button
                      disabled={block.next >= block.count}
                      onClick={() => openBlock(block.height, block.next)}
                    >
                      下一页
                    </button>
                  </div>
                </section>
              )}
            </>
          )}
          {view === "network" && network && (
            <>
              <div className="two-columns">
                <section className="panel">
                  <PanelTitle icon={Network} title="本机节点连接" />
                  <Topology nodes={nodes} />
                  <div className="panel-caption">
                    这里只显示本机启动的节点；课堂其他节点见下方连接列表。
                  </div>
                </section>
                <section className="panel padded">
                  <PanelTitle icon={Globe2} title="网络诊断" />
                  <div className="diagnostic">
                    <span>本机 IPv6 回环</span>
                    <b className={s.ipv6_available ? "text-teal" : ""}>
                      {s.ipv6_available ? "可用" : "不可用，使用 IPv4"}
                    </b>
                  </div>
                  <div className="diagnostic">
                    <span>课堂节点协议</span>
                    <b>IPv4 / IPv6 · v2</b>
                  </div>
                  <div className="diagnostic">
                    <span>本机入口端口</span>
                    <code>{network.ports.join(" / ")}</code>
                  </div>
                  <div className="diagnostic">
                    <span>本机观测链头一致</span>
                    <b>
                      {same} / {nodes.length}
                    </b>
                  </div>
                  <p className="small muted">
                    IPv6
                    回环可用不代表校园网允许入站。跨机器连接需使用可达地址，并检查
                    Windows 防火墙。
                  </p>
                  <button
                    className="button secondary"
                    onClick={async () => {
                      try {
                        download(
                          "nornv2-diagnostics.json",
                          await action("diagnostics"),
                        );
                      } catch {}
                    }}
                  >
                    <Download size={16} /> 导出诊断包
                  </button>
                </section>
              </div>
              {nodes.map((n) => (
                <section className="panel padded" key={n.id}>
                  <PanelTitle
                    icon={Box}
                    title={`本机节点 ${n.id}`}
                    right={
                      <span className={"pill " + (n.online ? "teal" : "amber")}>
                        {n.online ? "在线" : "未连接"}
                      </span>
                    }
                  />
                  <div className="node-meta">
                    <span>
                      端口 <b>{n.port}</b>
                    </span>
                    <span>
                      高度 <b>{n.height || "—"}</b>
                    </span>
                    <span className="mono">链头 {short(n.head, 15)}</span>
                  </div>
                  {(n.connections || []).map((p: Row) => (
                    <div className="peer-row" key={p.id}>
                      <Globe2 size={16} />
                      <code>
                        {p.ipv6
                          ? `[${p.host}]:${p.port}`
                          : `${p.host}:${p.port}`}
                      </code>
                      <span className="pill">{p.ipv6 ? "IPv6" : "IPv4"}</span>
                      <span>观测高度 {p.height}</span>
                    </div>
                  ))}
                </section>
              ))}
            </>
          )}
          {view === "chat" && network && (
            <div className="chat-layout">
              <aside className="room-panel">
                <div className="room-heading">
                  <MessageCircle size={19} />
                  <b>课堂房间</b>
                </div>
                {rooms.length ? (
                  rooms.map((r) => (
                    <button
                      key={r.id}
                      className={room === r.id ? "room active" : "room"}
                      onClick={() => {
                        setRoom(r.id);
                        setSelected(null);
                      }}
                    >
                      <Hash size={17} />
                      <span>{r.name}</span>
                      {!r.open && <LockKeyhole size={13} />}
                    </button>
                  ))
                ) : (
                  <p className="small muted pad">正在同步房间事件…</p>
                )}
                <div className="room-bottom">
                  <span className="pill teal">真实合约</span>
                  <p>每一条消息都是一次合约调用，正文公开保存在链上。</p>
                  <button
                    className="text-button"
                    onClick={() => {
                      setModal("source");
                    }}
                  >
                    查看合约源码 <ExternalLink size={13} />
                  </button>
                </div>
              </aside>
              <section className="chat-main">
                <div className="chat-top">
                  <div>
                    <h3>
                      <Hash size={19} />
                      {currentRoom?.name || "聊天室同步中"}
                    </h3>
                    <span>
                      {s.contract_ready ? "已校验聊天室字节码" : "等待合约同步"}{" "}
                      ·{" "}
                      {same > 1
                        ? `${same} 个本机节点最近观测链头相同`
                        : "本机节点观察"}
                    </span>
                  </div>
                  <ShieldCheck size={21} className="text-teal" />
                </div>
                <div className="chat-scroll">
                  <div className="chat-intro">
                    <span className="chat-intro-icon">
                      <MessageCircle size={23} />
                    </span>
                    <h3>聊天，也是一个区块链实验。</h3>
                    <p>发送一条消息，再点开它看看背后的交易。</p>
                  </div>
                  <button
                    className="history-button"
                    disabled={!!busy}
                    onClick={async () => {
                      try {
                        const rows = await action("history", {
                          offset: historyOffset,
                          limit: 50,
                        });
                        setHistoryRows((old) => [...rows, ...old]);
                        setHistoryOffset((x) => x + rows.length);
                        if (!rows.length)
                          setNotice("已经到达可索引历史的起点。");
                      } catch {}
                    }}
                  >
                    加载更早消息
                  </button>
                  {messages.map((m) => {
                    const own = m.sender === wallet?.evm_address;
                    return (
                      <div
                        key={m.id}
                        className={"chat-message " + (own ? "own" : "")}
                      >
                        <span className="message-avatar">
                          {(m.nickname || "同").slice(0, 1)}
                        </span>
                        <div>
                          <div className="message-author">
                            <b>{m.nickname}</b>
                            <code>{short(m.sender, 6)}</code>
                          </div>
                          <button
                            className={
                              "bubble " +
                              (selected?.id === m.id ? "selected" : "")
                            }
                            onClick={() => {
                              setSelected(m);
                              if (window.innerWidth <= 1160)
                                setModal("message");
                            }}
                          >
                            {m.body}
                          </button>
                          <div className="message-status">
                            <Check size={12} /> 本机已上链 · 区块 #{m.height}
                            <button
                              onClick={() => {
                                setSelected(m);
                                if (window.innerWidth <= 1160)
                                  setModal("message");
                              }}
                            >
                              查看交易 <ArrowUpRight size={11} />
                            </button>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                  {pending.map((p: Row) => (
                    <div className="chat-message own" key={p.hash}>
                      <span className="message-avatar">
                        {(p.nickname || "我").slice(0, 1)}
                      </span>
                      <div>
                        <div className="message-author">
                          <b>{p.nickname}</b>
                        </div>
                        <button
                          className="bubble pending"
                          onClick={() => receipt(p)}
                        >
                          {p.body}
                        </button>
                        <div className="message-status">
                          <Activity size={12} />
                          {p.execution_status !== undefined &&
                          p.execution_status !== 0
                            ? "合约执行回退"
                            : stateText[p.state] || "等待核对"}
                          <button
                            onClick={() => run("retry", { hash: p.hash })}
                          >
                            重试原交易
                          </button>
                        </div>
                      </div>
                    </div>
                  ))}
                  <div ref={chatEnd} />
                </div>
                <div className="composer">
                  <textarea
                    aria-label="聊天消息"
                    placeholder={
                      !s.unlocked
                        ? "先解锁钱包，再发送消息"
                        : !currentRoom?.open
                          ? "当前房间已关闭"
                          : "写一句话，记录在链上…"
                    }
                    value={body}
                    onChange={(e) => setBody(e.target.value)}
                    onKeyDown={(e) => {
                      if (
                        e.key === "Enter" &&
                        !e.shiftKey &&
                        !e.nativeEvent.isComposing
                      ) {
                        e.preventDefault();
                        if (canSend) send();
                      }
                    }}
                  />
                  <div>
                    <span className={bytes(body) > 512 ? "text-red" : ""}>
                      {bytes(body)} / 512 字节{" "}
                      <i>Enter 发送 · Shift + Enter 换行</i>
                    </span>
                    <button
                      className="button primary"
                      disabled={!canSend}
                      onClick={send}
                    >
                      <Send size={15} /> 发送消息
                    </button>
                  </div>
                </div>
              </section>
              <aside className="message-inspector">
                <span className="eyebrow">BEHIND THE MESSAGE</span>
                <h3>
                  {selectedMessage ? "这条消息怎样上链？" : "一条消息的旅程"}
                </h3>
                <ol className="journey">
                  {[
                    "钱包签名",
                    "提交交易，等待入块",
                    "合约执行并进入区块",
                    "其他节点同步账本",
                  ].map((label, i) => (
                    <li
                      key={label}
                      className={
                        selectedMessage?.height && (i < 3 || same > 1)
                          ? "done"
                          : ""
                      }
                    >
                      <span>
                        {selectedMessage?.height && (i < 3 || same > 1) ? (
                          <Check size={12} />
                        ) : (
                          i + 1
                        )}
                      </span>
                      <div>
                        <b>{label}</b>
                        <p>
                          {
                            [
                              "在本机用 Ed25519 密钥签名",
                              "网络发送成功不等于已入块",
                              "执行成功的回执包含消息事件",
                              "以最近观测结果显示一致情况",
                            ][i]
                          }
                        </p>
                      </div>
                    </li>
                  ))}
                </ol>
                {selectedMessage ? (
                  <>
                    <dl className="compact-details">
                      <dt>交易哈希</dt>
                      <dd>
                        <button
                          className="text-button mono"
                          onClick={() => copy(selectedMessage.tx_hash)}
                        >
                          {short(selectedMessage.tx_hash, 9)} <Copy size={12} />
                        </button>
                      </dd>
                      <dt>区块高度</dt>
                      <dd>#{selectedMessage.height}</dd>
                      <dt>后续区块</dt>
                      <dd>
                        {Math.max(
                          0,
                          (s.stats?.height || 0) - selectedMessage.height,
                        )}
                      </dd>
                      <dt>本机首次观察</dt>
                      <dd>
                        {new Date(
                          selectedMessage.observed_ms,
                        ).toLocaleTimeString()}
                      </dd>
                    </dl>
                    <button
                      className="button secondary full"
                      onClick={() => openBlock(selectedMessage.height)}
                    >
                      打开所在区块 <ArrowUpRight size={14} />
                    </button>
                  </>
                ) : (
                  <div className="inspector-tip">
                    <CircleHelp size={18} />
                    <p>点击一条消息，查看它的真实交易位置。</p>
                  </div>
                )}
                <p className="small muted">
                  已上链及后续区块数均不代表不可逆最终确认，发生重组时记录可能变化。
                </p>
              </aside>
            </div>
          )}
          {view === "learn" && (
            <>
              <div className="learning-hero">
                <span className="pill teal">一堂可以亲手完成的课</span>
                <h2>从“听说过”，到“我试过”。</h2>
                <p>
                  完成下面的实验。每一步都使用真实节点、真实签名和真实合约。
                </p>
              </div>
              <div className="lesson-grid">
                {[
                  {
                    title: "创建自己的身份",
                    text: "创建加密钱包，区分公钥、私钥与 EVM 地址。",
                    done: s.has_wallet,
                    go: () => needWallet("account"),
                    icon: KeyRound,
                  },
                  {
                    title: "启动三个节点",
                    text: "一键创建本地网络，观察它们怎样保存相同账本。",
                    done: s.running && nodes.length === 3,
                    go: () =>
                      network ? setView("network") : needWallet("demo"),
                    icon: Network,
                  },
                  {
                    title: "完成第一笔转账",
                    text: "给 Bob 转一点测试币，比较余额、费用与账户序号。",
                    done: (s.outbox || []).some(
                      (x: Row) =>
                        x.kind === "transfer" && x.state === "included",
                    ),
                    go: () => setView("wallet"),
                    icon: ArrowUpRight,
                  },
                  {
                    title: "用一句话调用合约",
                    text: "在聊天室发消息，找到执行回执和 MessageSent 事件。",
                    done: (s.outbox || []).some(
                      (x: Row) => x.body && x.state === "included",
                    ),
                    go: () => setView("chat"),
                    icon: MessageCircle,
                  },
                  {
                    title: "打开区块看一看",
                    text: "查看父哈希、Merkle 根和其中的交易。",
                    done: !!block,
                    go: () => setView("blocks"),
                    icon: Layers3,
                  },
                  {
                    title: "停止、重启，再观察",
                    text: "安全停止节点再恢复，检查余额与聊天历史是否保留。",
                    done: !!network?.id && restartVerified === network.id,
                    go: () => setView("network"),
                    icon: RefreshCw,
                  },
                ].map((l, i) => (
                  <button key={l.title} className="lesson-card" onClick={l.go}>
                    <span className="lesson-number">
                      实验 {String(i + 1).padStart(2, "0")}
                    </span>
                    <l.icon size={25} />
                    <h3>{l.title}</h3>
                    <p>{l.text}</p>
                    <span className={l.done ? "text-teal" : ""}>
                      {l.done ? (
                        <>
                          <CheckCircle2 size={15} /> 已完成
                        </>
                      ) : (
                        <>
                          开始操作 <ArrowRight size={15} />
                        </>
                      )}
                    </span>
                  </button>
                ))}
              </div>
              <div className="insight large">
                <BookOpen size={22} />
                <div>
                  <b>观察真实状态，也认识它的边界。</b>
                  <p>
                    界面上的 TPS
                    是短窗口教学观测值；动画不是共识机制。出块时间、交易确认时间和最终性是不同概念。
                  </p>
                </div>
              </div>
            </>
          )}
          {view === "teacher" && network?.teacher && (
            <>
              <div className="two-columns">
                <section className="panel padded">
                  <PanelTitle icon={Users} title="邀请学生加入" />
                  <p className="small muted">
                    填写学生电脑能够连接的地址，每行一个。端口为{" "}
                    {network.ports[0]}，IPv6 使用
                    [地址]:端口。回环地址只适用于同机实验。
                  </p>
                  <label className="field">
                    课堂入口
                    <textarea
                      aria-label="课堂入口"
                      value={endpoints}
                      onChange={(e) => setEndpoints(e.target.value)}
                      placeholder={`192.168.1.10:${network.ports[0]}\n[你的IPv6地址]:${network.ports[0]}`}
                    />
                  </label>
                  <button
                    className="button primary"
                    disabled={!endpoints.trim() || !!busy}
                    onClick={async () => {
                      try {
                        const result = await action("invite", {
                          seeds: endpoints.split(/\s+/).filter(Boolean),
                        });
                        setExported(result);
                      } catch {}
                    }}
                  >
                    生成课堂配置 <ArrowRight size={15} />
                  </button>
                  {exported && (
                    <div className="export-actions">
                      <button
                        className="button secondary"
                        onClick={() =>
                          download("nornv2-classroom-invite.json", exported)
                        }
                      >
                        <Download size={15} /> 下载配置
                      </button>
                      <button
                        className="button secondary"
                        onClick={() => copy(JSON.stringify(exported, null, 2))}
                      >
                        <Copy size={15} /> 复制配置
                      </button>
                    </div>
                  )}
                </section>
                <section className="panel padded">
                  <PanelTitle icon={Wallet} title="发放测试币" />
                  <p className="small muted">
                    学生加入后复制原生公钥给教师。发放使用教师钱包的正常转账，不增加链的总供应量。
                  </p>
                  <label className="field">
                    学生原生公钥
                    <input
                      aria-label="学生公钥"
                      value={receiver}
                      onChange={(e) => setReceiver(e.target.value.trim())}
                      placeholder="64 位十六进制公钥"
                    />
                  </label>
                  <label className="field">
                    测试币金额
                    <input
                      value={fundAmount}
                      onChange={(e) => setFundAmount(e.target.value)}
                      inputMode="numeric"
                    />
                  </label>
                  <button
                    className="button primary"
                    disabled={
                      !s.unlocked ||
                      !!busy ||
                      !/^[a-fA-F0-9]{64}$/.test(receiver) ||
                      !/^\d+$/.test(fundAmount)
                    }
                    onClick={() =>
                      run(
                        "fund",
                        { receiver, amount: fundAmount },
                        "教师签名发放测试币",
                      )
                    }
                  >
                    <Send size={15} /> 发放测试币
                  </button>
                </section>
              </div>
              <div className="two-columns">
                <section className="panel padded">
                  <PanelTitle icon={MessageCircle} title="房间管理" />
                  <label className="field">
                    新房间名称
                    <input
                      value={roomName}
                      onChange={(e) => setRoomName(e.target.value)}
                      placeholder="例如：第三实验小组"
                    />
                  </label>
                  <button
                    className="button secondary"
                    disabled={!roomName || !!busy}
                    onClick={() => run("room", { name: roomName })}
                  >
                    <Plus size={15} /> 创建房间
                  </button>
                  {rooms.map((r) => (
                    <div className="diagnostic" key={r.id}>
                      <span>
                        #{r.id} {r.name}
                      </span>
                      <button
                        className="text-button"
                        disabled={!!busy}
                        onClick={() =>
                          run("room_open", { room: r.id, open: !r.open })
                        }
                      >
                        {r.open ? "关闭发言" : "恢复发言"}
                      </button>
                    </div>
                  ))}
                </section>
                <section className="panel padded">
                  <PanelTitle icon={ShieldCheck} title="合约成员权限" />
                  <p className="small muted">
                    成员限制由合约检查，钱包仍需要测试币支付费用。
                  </p>
                  <div className="export-actions">
                    <button
                      className="button secondary"
                      disabled={!!busy}
                      onClick={() => run("members_only", { enabled: true })}
                    >
                      启用成员限制
                    </button>
                    <button
                      className="button secondary"
                      disabled={!!busy}
                      onClick={() => run("members_only", { enabled: false })}
                    >
                      允许公开发言
                    </button>
                  </div>
                  <label className="field">
                    成员 EVM 地址
                    <input
                      value={member}
                      onChange={(e) => setMember(e.target.value.trim())}
                      placeholder="40 位十六进制 EVM 地址"
                    />
                  </label>
                  <div className="export-actions">
                    <button
                      className="button secondary"
                      disabled={!/^[a-fA-F0-9]{40}$/.test(member) || !!busy}
                      onClick={() => run("member", { member, allowed: true })}
                    >
                      允许该成员
                    </button>
                    <button
                      className="button secondary"
                      disabled={!/^[a-fA-F0-9]{40}$/.test(member) || !!busy}
                      onClick={() => run("member", { member, allowed: false })}
                    >
                      移除该成员权限
                    </button>
                  </div>
                </section>
              </div>
              <section className="panel padded">
                <PanelTitle icon={Terminal} title="合约与索引" />
                <code className="code-line">{network.contract}</code>
                <div className="export-actions">
                  <button
                    className="button secondary"
                    onClick={() => setModal("source")}
                  >
                    查看 Solidity 源码
                  </button>
                  <button
                    className="button secondary"
                    onClick={async () => {
                      try {
                        const result = await action("call", {
                          to: network.contract,
                          input: selectors["messageCount()"],
                        });
                        if (result.status !== 0) throw Error("只读调用失败");
                        setNotice(
                          `合约当前消息计数：${BigInt("0x" + result.output).toString()}（只读查询，不收费）`,
                        );
                      } catch {}
                    }}
                  >
                    只读查询消息计数
                  </button>
                  <button
                    className="button secondary"
                    disabled={!!busy}
                    onClick={() => run("rebuild_index", {}, "从链上重建索引")}
                  >
                    重建本地索引
                  </button>
                </div>
                <p className="small muted">
                  SQLite 只保存可重建的界面索引。停止节点不会删除区块链数据库。
                </p>
              </section>
            </>
          )}
        </div>
        <footer>
          <span>NornV2 区块链实验室</span>
          <span>让每一个概念，都有一次亲手实践。</span>
          <button onClick={() => setView("learn")}>
            使用帮助 <ArrowUpRight size={12} />
          </button>
        </footer>
      </main>
      {notice && (
        <div className="toast" role="status">
          <CheckCircle2 size={18} />
          {notice}
        </div>
      )}
      {busy && (
        <div className="busy-indicator" role="status">
          <LoaderCircle className="spin" size={18} />
          {busy}…
        </div>
      )}
      {modal && (
        <div
          className="modal-backdrop"
          onClick={() => {
            if (!busy) setModal("");
          }}
        >
          <section
            className={
              "modal " +
              (modal === "source" || modal === "transaction" ? "wide" : "")
            }
            onClick={(e) => e.stopPropagation()}
          >
            <button
              className="modal-close icon-button"
              aria-label="关闭对话框"
              onClick={() => setModal("")}
              disabled={!!busy}
            >
              <X size={19} />
            </button>
            {error && (
              <div className="alert error" role="alert">
                <span>{error}</span>
                <button aria-label="关闭错误" onClick={() => setError("")}>
                  <X size={16} />
                </button>
              </div>
            )}
            {(modal === "wallet" ||
              modal === "unlock" ||
              modal === "backup") && (
              <>
                <span className="modal-icon">
                  <KeyRound size={26} />
                </span>
                <h2>
                  {modal === "wallet"
                    ? "创建你的加密钱包"
                    : modal === "unlock"
                      ? "解锁本机钱包"
                      : "导出加密备份"}
                </h2>
                <p>
                  {modal === "wallet"
                    ? "设置密码。程序会在本机生成 Alice、Bob 和教师三个教学身份。"
                    : modal === "unlock"
                      ? "密码只用于解锁这台电脑上的钱包。"
                      : "为备份设置至少 8 字节的密码，妥善保存备份与密码。"}
                </p>
                <form
                  onSubmit={async (e) => {
                    e.preventDefault();
                    try {
                      if (modal === "backup") {
                        download(
                          "nornv2-wallet-backup.json",
                          await action("backup", { password }),
                        );
                        setNotice("加密备份已导出。");
                      } else
                        await action(
                          modal === "wallet" ? "wallet_create" : "unlock",
                          { password },
                        );
                      setPassword("");
                      setModal("");
                    } catch {}
                  }}
                >
                  <label className="field">
                    钱包密码
                    <input
                      autoFocus
                      aria-label="钱包密码"
                      type="password"
                      autoComplete={
                        modal === "unlock" ? "current-password" : "new-password"
                      }
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder={
                        modal === "unlock" ? "输入钱包密码" : "至少 8 个字节"
                      }
                    />
                  </label>
                  <button
                    className="button primary full"
                    disabled={
                      !!busy ||
                      !password ||
                      (modal !== "unlock" && bytes(password) < 8)
                    }
                  >
                    {modal === "backup"
                      ? "下载加密备份"
                      : modal === "unlock"
                        ? "解锁钱包"
                        : "创建钱包"}
                  </button>
                </form>
                {modal === "wallet" && (
                  <label className="file-link">
                    已有备份？导入恢复
                    <input
                      type="file"
                      accept=".json"
                      onChange={async (e) => {
                        const file = e.target.files?.[0];
                        if (file) {
                          try {
                            setRestore(JSON.parse(await file.text()));
                            setModal("restore");
                          } catch {
                            setError("备份文件格式不正确");
                          }
                        }
                      }}
                    />
                  </label>
                )}
              </>
            )}
            {modal === "restore" && (
              <>
                <h2>恢复加密备份</h2>
                <p>输入备份密码。只允许在尚未创建钱包的应用目录中恢复。</p>
                <label className="field">
                  备份密码
                  <input
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </label>
                <button
                  className="button primary full"
                  disabled={!restore || !password || !!busy}
                  onClick={async () => {
                    try {
                      await action("restore", { backup: restore, password });
                      setModal("");
                      setPassword("");
                      setRestore(null);
                    } catch {}
                  }}
                >
                  恢复钱包
                </button>
              </>
            )}
            {(modal === "choose" ||
              modal === "demo" ||
              modal === "teacher" ||
              modal === "join") && (
              <>
                <span className="modal-icon">
                  <GraduationCap size={26} />
                </span>
                <h2>
                  {modal === "join"
                    ? "加入课堂"
                    : modal === "teacher"
                      ? "创建教学课堂"
                      : "开始一个新实验"}
                </h2>
                {modal === "choose" ? (
                  <div className="choose-buttons">
                    <button
                      className="button secondary"
                      onClick={() => setModal("demo")}
                    >
                      <Box size={17} /> 单机三节点实验
                    </button>
                    <button
                      className="button secondary"
                      onClick={() => setModal("join")}
                    >
                      <Users size={17} /> 加入已有课堂
                    </button>
                    <button
                      className="button secondary"
                      onClick={() => setModal("teacher")}
                    >
                      <GraduationCap size={17} /> 创建教学课堂
                    </button>
                  </div>
                ) : modal === "join" ? (
                  <>
                    <p>
                      粘贴或导入教师发来的课堂配置。钱包私钥不会发送给教师。
                    </p>
                    <label className="field">
                      课堂配置
                      <textarea
                        aria-label="课堂配置"
                        className="invite-input"
                        value={invite}
                        onChange={(e) => setInvite(e.target.value)}
                        placeholder="粘贴课堂 JSON 配置"
                      />
                    </label>
                    <label className="file-link">
                      从文件导入
                      <input
                        type="file"
                        accept=".json"
                        onChange={async (e) => {
                          const f = e.target.files?.[0];
                          if (f) setInvite(await f.text());
                        }}
                      />
                    </label>
                    <button
                      className="button primary full"
                      disabled={!invite || !!busy}
                      onClick={() => enter(true)}
                    >
                      加入并启动本地节点 <ArrowRight size={16} />
                    </button>
                  </>
                ) : (
                  <>
                    <p>
                      {modal === "teacher"
                        ? "启动三个教学节点，随后在课堂管理中填写可达地址并分享给同学。"
                        : "自动创建独立数据库、三个节点和聊天室。已有实验数据会保留。"}
                    </p>
                    <label className="field">
                      实验名称
                      <input
                        aria-label="实验名称"
                        value={title}
                        onChange={(e) => setTitle(e.target.value)}
                      />
                    </label>
                    <div className="setup-summary">
                      <span>
                        <CheckCircle2 size={15} /> 新建独立教学链
                      </span>
                      <span>
                        <CheckCircle2 size={15} /> 加载测试币与演示钱包
                      </span>
                      <span>
                        <CheckCircle2 size={15} /> 正常交易部署聊天室合约
                      </span>
                    </div>
                    <button
                      className="button primary full"
                      disabled={!!busy || !title.trim()}
                      onClick={() =>
                        enter(false, modal === "teacher" ? "teacher" : "demo")
                      }
                    >
                      创建并启动 <Play size={15} />
                    </button>
                  </>
                )}
              </>
            )}
            {modal === "account" && (
              <>
                <h2>本机钱包</h2>
                <p>课堂网络中的账户身份由公钥决定，昵称只用于显示。</p>
                <label className="field">
                  聊天昵称
                  <input
                    value={nick || s.nickname || ""}
                    onChange={(e) => setNick(e.target.value)}
                  />
                </label>
                <button
                  className="button primary full"
                  disabled={!!busy || bytes(nick || s.nickname || "") > 48}
                  onClick={async () => {
                    try {
                      await action("nickname", {
                        nickname: nick || s.nickname,
                      });
                      setModal("");
                    } catch {}
                  }}
                >
                  保存昵称
                </button>
                <div className="export-actions">
                  <button
                    className="button secondary"
                    onClick={() => setModal("backup")}
                  >
                    加密备份
                  </button>
                  <button
                    className="button secondary"
                    onClick={() => {
                      run("lock");
                      setModal("");
                    }}
                  >
                    锁定钱包
                  </button>
                </div>
              </>
            )}
            {modal === "message" && selected && (
              <>
                <span className="modal-icon">
                  <MessageCircle size={26} />
                </span>
                <h2>这条消息的链上记录</h2>
                <p>{selected.body}</p>
                <dl className="details">
                  <dt>交易哈希</dt>
                  <dd className="mono">{selected.tx_hash}</dd>
                  <dt>区块高度</dt>
                  <dd>#{selected.height}</dd>
                  <dt>本机首次观察</dt>
                  <dd>{new Date(selected.observed_ms).toLocaleString()}</dd>
                  <dt>当前状态</dt>
                  <dd>本机已上链；仍可能随链重组变化</dd>
                </dl>
                <button
                  className="button primary full"
                  onClick={() => {
                    setModal("");
                    openBlock(selected.height);
                  }}
                >
                  打开所在区块 <ArrowUpRight size={15} />
                </button>
              </>
            )}
            {modal === "source" && (
              <>
                <h2>ClassroomChat.sol</h2>
                <p>
                  Solidity 0.8.28 · Shanghai ·
                  普通部署交易。消息正文随交易与事件上链，公开可读取。
                </p>
                <pre className="source-code">{chatSource}</pre>
              </>
            )}
            {modal === "transaction" && selected && (
              <>
                <h2>交易详情</h2>
                <p>从这里核对交易哈希、合约回执和执行结果。</p>
                <dl className="details">
                  <dt>交易哈希</dt>
                  <dd className="mono">{selected.hash || selected.tx_hash}</dd>
                  <dt>状态</dt>
                  <dd>
                    {selected.receipt
                      ? selected.receipt.status === 0
                        ? "合约执行成功"
                        : "合约执行回退（消耗实际费用）"
                      : stateText[selected.state] || "已在当前规范链中观察"}
                  </dd>
                  <dt>区块高度</dt>
                  <dd>{selected.height || "等待入块"}</dd>
                  <dt>账户序号</dt>
                  <dd>{selected.nonce || "—"}</dd>
                </dl>
                <pre className="source-code">
                  {JSON.stringify(selected, null, 2)}
                </pre>
                {selected.height && (
                  <button
                    className="button primary"
                    onClick={() => {
                      setModal("");
                      openBlock(selected.height);
                    }}
                  >
                    打开所在区块 <ArrowUpRight size={15} />
                  </button>
                )}
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
function Metric({
  icon: Icon,
  title,
  value,
  unit,
  foot,
}: {
  icon: typeof Box;
  title: string;
  value: string;
  unit?: string;
  foot: string;
}) {
  return (
    <section className="metric">
      <div>
        <span>{title}</span>
        <Icon size={18} />
      </div>
      <strong>
        {value}
        <small>{unit}</small>
      </strong>
      <p>{foot}</p>
    </section>
  );
}
function PanelTitle({
  icon: Icon,
  title,
  right,
}: {
  icon: typeof Box;
  title: string;
  right?: React.ReactNode;
}) {
  return (
    <div className="panel-title">
      <h3>
        <Icon size={18} />
        {title}
      </h3>
      {right}
    </div>
  );
}
function BlockTable({
  blocks,
  onSelect,
}: {
  blocks: Row[];
  onSelect: (height: number) => void;
}) {
  return blocks.length ? (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>区块</th>
            <th>区块哈希</th>
            <th>交易数量</th>
            <th>父区块</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {blocks.map((b) => (
            <tr key={b.hash} onClick={() => onSelect(b.height)}>
              <td>
                <span className="block-icon">
                  <Box size={14} />
                </span>
                <b>#{b.height}</b>
              </td>
              <td className="mono">{short(b.hash, 15)}</td>
              <td>{b.count} 笔</td>
              <td className="mono muted">{short(b.parent, 10)}</td>
              <td>
                <ArrowUpRight size={16} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : (
    <div className="empty">
      <Box size={28} />
      <p>节点启动后，区块记录会出现在这里。</p>
    </div>
  );
}
function Outbox({
  rows,
  onSelect,
  onRetry,
}: {
  rows: Row[];
  onSelect: (tx: Row) => void;
  onRetry: (hash: string) => void;
}) {
  return rows.length ? (
    <div>
      {rows.map((tx) => (
        <div className="transaction-row" key={tx.hash}>
          <span className="tx-icon">
            <ArrowUpRight size={17} />
          </span>
          <button className="transaction-main" onClick={() => onSelect(tx)}>
            <b>
              {tx.kind === "transfer"
                ? "转账"
                : tx.kind === "deploy"
                  ? "部署聊天室"
                  : tx.body
                    ? "发送聊天消息"
                    : "调用合约"}
            </b>
            <span className="mono">{short(tx.hash, 15)}</span>
          </button>
          <span
            className={"pill " + (tx.state === "included" ? "teal" : "amber")}
          >
            {stateText[tx.state] || tx.state}
          </span>
          {tx.state !== "included" && (
            <button className="text-button" onClick={() => onRetry(tx.hash)}>
              重试原交易
            </button>
          )}
        </div>
      ))}
    </div>
  ) : (
    <div className="empty">
      <Activity size={27} />
      <p>提交交易后，这里会显示它的状态。</p>
    </div>
  );
}
function Topology({ nodes }: { nodes: Row[] }) {
  const actual = nodes.length ? nodes : [];
  const points = actual.map((n, i) => ({
    n,
    x: actual.length === 1 ? 250 : 125 + i * 125,
    y: actual.length === 1 ? 100 : i === 1 ? 65 : 140,
  }));
  return (
    <div className="topology">
      <svg viewBox="0 0 500 210" role="img" aria-label="本机节点拓扑">
        {points.flatMap((p, i) =>
          points
            .slice(i + 1)
            .filter(
              (q) =>
                (p.n.connections || []).some((c: Row) => c.port === q.n.port) ||
                (q.n.connections || []).some((c: Row) => c.port === p.n.port),
            )
            .map((q) => (
              <g key={`${p.n.id}-${q.n.id}`}>
                <line
                  x1={p.x}
                  y1={p.y}
                  x2={q.x}
                  y2={q.y}
                  className="network-line"
                />
                <circle r="3" fill="#1f9c83">
                  <animateMotion
                    dur="3s"
                    repeatCount="indefinite"
                    path={`M ${p.x} ${p.y} L ${q.x} ${q.y}`}
                  />
                </circle>
              </g>
            )),
        )}
        {points.map((p) => (
          <g key={p.n.id}>
            <circle
              cx={p.x}
              cy={p.y}
              r="33"
              fill={p.n.online ? "#e8f6f1" : "#eef0f1"}
              stroke={p.n.online ? "#9ad7c4" : "#d9dfe2"}
            />
            <rect
              x={p.x - 10}
              y={p.y - 11}
              width="20"
              height="20"
              rx="5"
              fill="none"
              stroke={p.n.online ? "#20876f" : "#87969e"}
              strokeWidth="1.8"
            />
            <path
              d={`M ${p.x - 6} ${p.y - 3} L ${p.x + 6} ${p.y - 3} M ${p.x - 6} ${p.y + 3} L ${p.x + 6} ${p.y + 3}`}
              stroke="#20876f"
            />
            <text x={p.x} y={p.y + 51} textAnchor="middle">
              节点 {p.n.id} · #{p.n.height || "—"}
            </text>
          </g>
        ))}
        {!points.length && (
          <text x="250" y="105" textAnchor="middle" fill="#8c9ba4">
            启动网络后显示真实节点
          </text>
        )}
      </svg>
    </div>
  );
}
function Sparkline({ samples }: { samples: { t: number; v: number }[] }) {
  const max = Math.max(1, ...samples.map((s) => s.v));
  const points = samples
    .map(
      (s, i) =>
        `${20 + (i / Math.max(1, samples.length - 1)) * 460},${145 - (s.v / max) * 105}`,
    )
    .join(" ");
  return (
    <svg
      className="sparkline"
      viewBox="0 0 500 180"
      role="img"
      aria-label="近期观测交易速率"
    >
      <defs>
        <linearGradient id="area" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#79cbb6" stopOpacity=".28" />
          <stop offset="100%" stopColor="#79cbb6" stopOpacity="0" />
        </linearGradient>
      </defs>
      {[40, 92, 145].map((y) => (
        <line
          key={y}
          x1="20"
          y1={y}
          x2="480"
          y2={y}
          stroke="#e8edef"
          strokeDasharray="4 5"
        />
      ))}
      {samples.length > 1 && (
        <>
          <polygon points={`20,145 ${points} 480,145`} fill="url(#area)" />
          <polyline
            points={points}
            fill="none"
            stroke="#269c80"
            strokeWidth="2.5"
            strokeLinejoin="round"
          />
        </>
      )}
      <text x="20" y="23" fill="#7d909c" fontSize="11">
        {max.toFixed(1)} TPS
      </text>
    </svg>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
