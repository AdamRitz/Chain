import React, { useEffect, useRef, useState } from "react";
import { version } from "../package.json";
import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  Box,
  CheckCircle2,
  ChevronRight,
  Copy,
  Download,
  Globe2,
  Hash,
  Layers3,
  LoaderCircle,
  MessageCircle,
  Network,
  Play,
  Plus,
  Send,
  Square,
  Users,
  Wallet,
  X,
} from "lucide-react";
import { api, bytes, download, number, short, type Row } from "./api";
import { CanSend, MergeMessages, SelectedMessage } from "./model";
import { NetworkPanel } from "./NetworkPanel";
import { InviteInput, InviteShare } from "./InviteDialogs";
import { CanJoinPreview, ConnectionText, ReceiptText } from "./invite";
import { BlockTable, Metric, Outbox, PanelTitle, Topology } from "./components";
import "./style.css";

type View = "chat" | "wallet" | "blocks" | "network" | "home";
const views = [
  {
    id: "chat",
    title: "聊天室",
    icon: MessageCircle,
    caption: "与当前链上的成员交流。",
  },
  {
    id: "wallet",
    title: "我的账户",
    icon: Wallet,
    caption: "查看余额、收款地址和转账记录。",
  },
  {
    id: "blocks",
    title: "链上记录",
    icon: Layers3,
    caption: "查看当前链的区块和交易。",
  },
  {
    id: "network",
    title: "节点与连接",
    icon: Network,
    caption: "本机运行一个节点，与其他成员共同维护链。",
  },
  {
    id: "home",
    title: "链状态",
    icon: Activity,
    caption: "当前节点观测到的账本状态。",
  },
] as const;
function TxText(tx: Row) {
  if ((tx.receipt?.status ?? tx.execution_status ?? 0) !== 0)
    return "合约执行失败";
  return tx.state === "included"
    ? "本机已上链"
    : tx.state === "reorganized"
      ? "退出主链，等待重新入块"
      : "等待入块";
}

export function PeerApp() {
  const [s, setS] = useState<Row>({});
  const [view, setView] = useState<View>("chat");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [modal, setModal] = useState("");
  const [password, setPassword] = useState("");
  const [title, setTitle] = useState("学术交流");
  const [nickname, setNickname] = useState("");
  const [invite, setInvite] = useState("");
  const [joinPreview, setJoinPreview] = useState<Row | null>(null);
  const incomingSeen = useRef("");
  const [room, setRoom] = useState(1);
  const [body, setBody] = useState("");
  const [roomName, setRoomName] = useState("");
  const [receiver, setReceiver] = useState("");
  const [amount, setAmount] = useState("10000000");
  const [selected, setSelected] = useState<Row | null>(null);
  const [block, setBlock] = useState<Row | null>(null);
  const [height, setHeight] = useState("");
  const [history, setHistory] = useState<Row[]>([]);
  const [offset, setOffset] = useState(50);
  const polling = useRef(false);
  const refreshPending = useRef(false);
  const chatEnd = useRef<HTMLDivElement>(null);
  const refresh = async () => {
    if (polling.current) { refreshPending.current = true; return; }
    polling.current = true;
    try {
      setS(await api("status"));
    } catch (failure) {
      setError((failure as Error).message);
      setS((old) => ({ ...old, running: false, connection_error: true }));
    } finally {
      polling.current = false;
      if (refreshPending.current) { refreshPending.current = false; void refresh(); }
    }
  };
  useEffect(() => {
    void refresh();
    const timer = setInterval(refresh, 1200);
    window.addEventListener("norn-invite-ready", refresh);
    return () => { clearInterval(timer); window.removeEventListener("norn-invite-ready", refresh); };
  }, []);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(""), 4500);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    setHistory([]);
    setOffset(50);
    setRoom(1);
    setSelected(null);
    setBlock(null);
  }, [s.network?.id]);
  useEffect(() => {
    setHistory([]);
    setOffset(50);
  }, [s.reorg_count]);
  const action = async (name: string, values: Row = {}, label = "正在处理") => {
    setBusy(label);
    setError("");
    try {
      const result = await api(name, values);
      if (result.has_wallet !== undefined) setS(result);
      else await refresh();
      return result;
    } catch (failure) {
      setError((failure as Error).message);
      throw failure;
    } finally {
      setBusy("");
    }
  };
  const run = (name: string, values: Row = {}, label?: string) =>
    void action(name, values, label).catch(() => {});
  const copy = (text: string) =>
    void navigator.clipboard
      .writeText(text)
      .then(() => setNotice("已复制"))
      .catch(() => setError("复制失败，请手动选择文字复制。"));
  const network = s.network;
  const wallet = (s.wallets || []).find(
    (item: Row) => item.id === s.active_wallet,
  );
  const balance = wallet?.balance || "0";
  const funded =
    BigInt(balance) >= 500000n + BigInt(network?.genesis?.base_fee || 0);
  const nodes: Row[] = s.nodes || [];
  const peers = nodes.reduce(
    (total, node) => total + (node.connections || []).length,
    0,
  );
  const rooms: Row[] = s.rooms || [];
  const currentRoom = rooms.find((item) => item.id === room);
  const messages = MergeMessages(history, s.messages || []).filter(
    (item) => item.room === room,
  );
  const pending = (s.outbox || []).filter(
    (item: Row) =>
      item.body &&
      item.room === room &&
      !messages.some((message) => message.tx_hash === item.hash),
  );
  const inspected = SelectedMessage(selected, messages);
  const canSend = CanSend(body, {
    unlocked: !!s.unlocked,
    running: !!s.running && (!s.connection_state || s.connection_state.phase === "ready"),
    contractReady: !!s.contract_ready,
    roomOpen: !!currentRoom?.open,
    busy: !!busy,
    funded,
  });
  const enter = async (joining: boolean) => {
    try {
      if (joining && !CanJoinPreview(joinPreview, joinPreview?.id || "")) throw Error("正在连接节点并获取链配置，请稍候。");
      if (!s.unlocked)
        await action(
          s.has_wallet ? "unlock" : "wallet_create",
          { password },
          "正在打开本机账户",
        );
      if (nickname.trim())
        await action("nickname", { nickname: nickname.trim() });
      await action(
        joining ? "join" : "create",
        joining ? { preview_id: joinPreview!.id } : { title },
        joining ? "正在连接链与聊天室" : "正在创建链并部署聊天室",
      );
      setModal("");
      setPassword("");
      setBody("");
      setView("chat");
      setNotice(
        joining
          ? "正在连接聊天室，请查看同步进度。"
          : "链和聊天室已就绪，可以分享邀请。",
      );
    } catch (failure) {
      setError((failure as Error).message);
    }
  };
  const send = async (text = body, target = room) => {
    try {
      await action("message", { room: target, body: text }, "正在签名发送");
      setBody("");
      setNotice("消息已提交，正在等待入块。");
    } catch {}
  };
  const openBlock = async (at: number, start = 0) => {
    try {
      setBlock(
        await action(
          "block",
          { height: at, offset: start, limit: 10 },
          "正在读取区块",
        ),
      );
      setView("blocks");
    } catch {}
  };
  useEffect(() => {
    chatEnd.current?.scrollIntoView({ block: "nearest" });
  }, [view, messages.at(-1)?.id, pending.length]);
  useEffect(() => {
    const value = s.incoming_invitation;
    if (!value?.id || value.id === incomingSeen.current || busy || (modal && modal !== "join")) return;
    incomingSeen.current = value.id;
    setInvite(value.text || ""); setJoinPreview(null); setModal("join");
    if (value.error) setError(value.error);
    void api("invite_received", { id: value.id }).catch(() => {});
  }, [s.incoming_invitation?.id, busy, modal]);
  const currentView = views.find((item) => item.id === view)!;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <Layers3 size={25} />
          </span>
          <div>
            NornV2<span>学术聊天室</span>
          </div>
        </div>
        <nav>
          {views.map((item) => (
            <button
              key={item.id}
              className={view === item.id ? "nav active" : "nav"}
              aria-label={item.title}
              title={item.title}
              onClick={() => setView(item.id)}
            >
              <item.icon size={19} />
              <span>{item.title}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className="tiny-dot" />
          NornV2 {version}
          <span>本地签名 · 链上聊天</span>
        </div>
      </aside>
      <main>
        <header>
          <div className="breadcrumb">
            NornV2 <ChevronRight size={14} />
            <span>{currentView.title}</span>
          </div>
          <div className="header-right">
            <button className="button secondary compact" onClick={() => setModal("join")}><Plus size={15} />加入聊天室</button>
            {network && <button className="button primary compact" onClick={() => setModal("share")}><Users size={15} />邀请朋友</button>}
            <span className={"connection " + (s.running ? "connected" : "")}>
              <i />
              {s.connection_error
                ? "本机服务连接中断"
                : s.running
                  ? ConnectionText(s)
                  : "节点未运行"}
            </span>
            <button
              className="avatar"
              title="账户与昵称"
              onClick={() => setModal(s.unlocked ? "account" : "wallet")}
            >
              <Wallet size={18} />
            </button>
          </div>
        </header>
        <div className="content">
          <div className="page-title">
            <div>
              <h1>{currentView.title}</h1>
              <p>{currentView.caption}</p>
            </div>
            {network && view === "network" && (
              <div className="page-actions">
                <button
                  className="button secondary"
                  disabled={!!busy}
                  onClick={() => run(s.running ? "stop" : "start")}
                >
                  {s.running ? <Square size={15} /> : <Play size={15} />}
                  {s.running ? "停止节点" : "恢复节点"}
                </button>
                <button
                  className="button secondary"
                  disabled={!!busy}
                  onClick={() => setModal("join")}
                >
                  <Globe2 size={16} />
                  加入聊天室
                </button>
              </div>
            )}
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
            <div className="alert warning">连接提示：{s.last_error}</div>
          )}
          {!network && (
            <section className="welcome">
              <div className="welcome-illustration">
                <div className="orb a">
                  <MessageCircle />
                </div>
              </div>
              <span className="pill teal">NornV2</span>
              <h2>创建聊天室，开始讨论。</h2>
              <p>
                创建链并分享邀请，或加入其他成员的链和聊天室。每台电脑运行一个节点。
              </p>
              <div className="welcome-options peer-options">
                <button onClick={() => setModal("create")}>
                  <span className="option-icon">
                    <Globe2 />
                  </span>
                  <b>创建聊天室</b>
                  <p>初始化链和公共讨论，随后邀请其他成员加入。</p>
                  <span>
                    创建聊天室 <ArrowRight size={17} />
                  </span>
                </button>
                <button onClick={() => setModal("join")}>
                  <span className="option-icon blue">
                    <Users />
                  </span>
                  <b>加入聊天室</b>
                  <p>粘贴成员的 IP 和端口，自动获取链配置并同步。</p>
                  <span>
                    加入聊天室 <ArrowRight size={17} />
                  </span>
                </button>
              </div>
            </section>
          )}
          {network && view === "chat" && (
            <div className="chat-layout peer-chat-layout">
              <aside className="room-panel">
                <div className="room-heading">
                  <h3>讨论分组</h3>
                  <span className="pill">{rooms.length}</span>
                </div>
                {rooms.map((item) => (
                  <button
                    className={room === item.id ? "room active" : "room"}
                    key={item.id}
                    onClick={() => {
                      setRoom(item.id);
                      setSelected(null);
                    }}
                  >
                    <Hash size={16} />
                    <span>{item.name}</span>
                  </button>
                ))}
                {network.chat_version === 2 && (
                  <div className="peer-room-create">
                    <input
                      aria-label="讨论分组名称"
                      value={roomName}
                      onChange={(event) => setRoomName(event.target.value)}
                      placeholder="讨论分组名称"
                    />
                    <button
                      className="button secondary full"
                      disabled={
                        !s.unlocked ||
                        !s.running ||
                        !funded ||
                        !!busy ||
                        !roomName.trim() ||
                        bytes(roomName) > 96
                      }
                      onClick={async () => {
                        try {
                          await action("room", { name: roomName.trim() });
                          setRoomName("");
                        } catch {}
                      }}
                    >
                      <Plus size={15} />
                      创建分组
                    </button>
                  </div>
                )}
                <p className="small muted peer-room-note">
                  昵称用于显示，身份以钱包地址为准。消息公开记录在当前链上。
                </p>
              </aside>
              <section className="chat-main">
                <div className="chat-top">
                  <div>
                    <h3>
                      <Hash size={19} />
                      {currentRoom?.name || "正在同步讨论分组"}
                    </h3>
                    <span>
                      {network.title} ·{" "}
                      {ConnectionText(s)}
                    </span>
                  </div>
                  <button
                    className="text-button"
                    onClick={() => setModal("share")}
                  >
                    <Users size={16} />
                    邀请朋友
                  </button>
                </div>
                <div className="chat-scroll">
                  <button
                    className="history-button"
                    disabled={!!busy}
                    onClick={async () => {
                      try {
                        const rows = await action("history", {
                          offset,
                          limit: 50,
                        });
                        setHistory((old) => [...rows, ...old]);
                        setOffset((old) => old + rows.length);
                        if (!rows.length) setNotice("没有更早的消息。");
                      } catch {}
                    }}
                  >
                    加载更早消息
                  </button>
                  {messages.map((message) => (
                    <div
                      className={
                        "chat-message " +
                        (message.sender === wallet?.evm_address ? "own" : "")
                      }
                      key={message.id}
                    >
                      <span className="message-avatar">
                        {(message.nickname || "成员").slice(0, 1)}
                      </span>
                      <div>
                        <div className="message-author">
                          <b>{message.nickname}</b>
                          <code>{short(message.sender, 6)}</code>
                        </div>
                        <button
                          className="bubble"
                          onClick={() => {
                            setSelected(message);
                            setModal("message");
                          }}
                        >
                          {message.body}
                        </button>
                        <div className="message-status">
                          <CheckCircle2 size={12} />
                          本机已上链 · 区块 #{message.height}
                          <button
                            onClick={() => {
                              setSelected(message);
                              setModal("message");
                            }}
                          >
                            查看记录
                          </button>
                        </div>
                      </div>
                    </div>
                  ))}
                  {pending.map((item: Row) => (
                    <div className="chat-message own" key={item.hash}>
                      <span className="message-avatar">我</span>
                      <div>
                        <button
                          className="bubble pending"
                          onClick={() => {
                            setSelected(item);
                            setModal("transaction");
                          }}
                        >
                          {item.body}
                        </button>
                        <div className="message-status">
                          {TxText(item)}
                          {item.state !== "included" ? (
                            <button
                              onClick={() => run("retry", { hash: item.hash })}
                            >
                              重试原交易
                            </button>
                          ) : item.execution_status &&
                            item.sender === wallet?.public_key ? (
                            <button
                              onClick={() => void send(item.body, item.room)}
                            >
                              重新发送
                            </button>
                          ) : null}
                        </div>
                      </div>
                    </div>
                  ))}
                  <div ref={chatEnd} />
                </div>
                <div className="composer">
                  {!s.unlocked ? (
                    <button
                      className="button secondary"
                      onClick={() => setModal("wallet")}
                    >
                      登录本机账户后发送消息
                    </button>
                  ) : s.contract_ready && !funded ? (
                    <div className="peer-fee-notice" role="status">
                      <span>
                        账户需要链上余额支付消息手续费，可由任意有余额的成员普通转账补充。
                      </span>
                      <button
                        className="text-button"
                        onClick={() => copy(ReceiptText(s, wallet.public_key))}
                      >
                        <Copy size={14} />
                        复制收款信息
                      </button>
                    </div>
                  ) : null}
                  <textarea
                    aria-label="聊天消息"
                    value={body}
                    onChange={(event) => setBody(event.target.value)}
                    placeholder={
                      !s.contract_ready ? "正在同步聊天室…" : "输入消息…"
                    }
                    onKeyDown={(event) => {
                      if (
                        event.key === "Enter" &&
                        !event.shiftKey &&
                        !event.nativeEvent.isComposing
                      ) {
                        event.preventDefault();
                        if (canSend) void send();
                      }
                    }}
                  />
                  <div className="composer-actions">
                    <span className="composer-hint">
                      {bytes(body)} / 512 字节 · Enter 发送
                    </span>
                    <button
                      className="button primary"
                      disabled={!canSend}
                      onClick={() => void send()}
                    >
                      <Send size={15} />
                      发送消息
                    </button>
                  </div>
                </div>
              </section>
            </div>
          )}
          {network && view === "wallet" && (
            <>
              <div className="two-columns">
                <section className="wallet-card">
                  <span>我的本机账户</span>
                  <div className="wallet-card-top">
                    {(s.wallets || []).length > 1 ? (
                      <select
                        className="wallet-select"
                        aria-label="当前账户"
                        value={s.active_wallet}
                        disabled={!!busy}
                        onChange={(event) =>
                          run("select_wallet", { wallet: event.target.value })
                        }
                      >
                        {s.wallets.map((item: Row) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <h2>{s.nickname || "我的账户"}</h2>
                    )}
                    <Wallet size={30} />
                  </div>
                  <div className="balance">
                    {s.unlocked ? number(balance) : "待登录"}
                    <span>链上余额</span>
                  </div>
                  <label>收款地址 · Ed25519 公钥</label>
                  <button
                    className="key-copy mono"
                    disabled={!wallet}
                    aria-label="复制收款地址"
                    onClick={() => copy(wallet.public_key)}
                  >
                    <span>{wallet?.public_key || "登录后显示"}</span>
                    <Copy size={15} />
                  </button>
                  <label>EVM 地址</label>
                  <button
                    className="key-copy mono"
                    disabled={!wallet}
                    aria-label="复制 EVM 地址"
                    onClick={() => copy(wallet.evm_address)}
                  >
                    <span>{wallet?.evm_address || "登录后显示"}</span>
                    <Copy size={15} />
                  </button>
                  <div className="wallet-bottom">
                    <span>已确认序号 {wallet?.nonce || "0"}</span>
                    <span>当前链的账户余额</span>
                  </div>
                </section>
                <section className="panel padded">
                  <PanelTitle icon={Send} title="普通转账" />
                  <p className="small muted">
                    任何有余额的成员都可以转账，帮助其他成员支付链上消息费用。
                  </p>
                  <label className="field">
                    收款人公钥
                    <input
                      aria-label="收款人公钥"
                      value={receiver}
                      onChange={(event) =>
                        setReceiver(event.target.value.trim())
                      }
                      placeholder="64 位十六进制公钥"
                    />
                  </label>
                  <label className="field">
                    金额（最小单位）
                    <input
                      aria-label="转账金额"
                      value={amount}
                      onChange={(event) => setAmount(event.target.value)}
                      inputMode="numeric"
                    />
                  </label>
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
                      run("transfer", { receiver, amount }, "正在签名转账")
                    }
                  >
                    <Send size={16} />
                    签名并转账
                  </button>
                </section>
              </div>
              <section className="panel">
                <PanelTitle icon={ArrowUpRight} title="本机发送记录" />
                <Outbox
                  rows={s.outbox || []}
                  onSelect={(item) => {
                    setSelected(item);
                    setModal("transaction");
                  }}
                  onRetry={(hash) => run("retry", { hash })}
                />
              </section>
            </>
          )}
          {network && view === "blocks" && (
            <>
              <div className="search-row">
                <input
                  aria-label="区块高度"
                  placeholder="输入区块高度"
                  value={height}
                  onChange={(event) => setHeight(event.target.value)}
                />
                <button
                  className="button secondary"
                  disabled={!/^\d+$/.test(height) || !!busy}
                  onClick={() => void openBlock(Number(height))}
                >
                  查询区块
                </button>
              </div>
              <section className="panel">
                <PanelTitle icon={Box} title="最近区块" />
                <BlockTable blocks={s.blocks || []} onSelect={openBlock} />
              </section>
              {block && (
                <section className="panel padded">
                  <PanelTitle icon={Layers3} title={`区块 #${block.height}`} />
                  <p className="mono code-line">{block.hash}</p>
                  {(block.transactions || []).map((item: Row) => (
                    <button
                      className="transaction-row"
                      key={item.hash}
                      onClick={() => {
                        setSelected(item);
                        setModal("transaction");
                      }}
                    >
                      <code>{short(item.hash, 20)}</code>
                      <span>
                        {item.kind === "transfer" ? "转账" : "合约交易"}
                      </span>
                    </button>
                  ))}
                  <div className="pagination">
                    <button
                      disabled={!block.offset}
                      onClick={() =>
                        void openBlock(
                          block.height,
                          Math.max(0, block.offset - 10),
                        )
                      }
                    >
                      上一页
                    </button>
                    <span>
                      {block.offset + 1}–{block.next} / {block.count}
                    </span>
                    <button
                      disabled={block.next >= block.count}
                      onClick={() => void openBlock(block.height, block.next)}
                    >
                      下一页
                    </button>
                  </div>
                </section>
              )}
            </>
          )}
          {network && view === "home" && (
            <>
              <section className="panel padded">
                <PanelTitle icon={Globe2} title={network.title} />
                <p className="mono code-line">
                  创世指纹：{network.genesis_root}
                </p>
                <p className="mono code-line">
                  聊天室合约：{network.contract || "正在部署"}
                </p>
              </section>
              <div className="metrics">
                <Metric
                  icon={Layers3}
                  title="当前区块高度"
                  value={number(s.stats?.height || 0)}
                  foot="本机节点的规范链"
                />
                <Metric
                  icon={Activity}
                  title="累计链上交易"
                  value={number(s.stats?.chain_transactions || 0)}
                  foot="按当前主链计数"
                />
                <Metric
                  icon={Network}
                  title="对等连接"
                  value={String(peers)}
                  foot="连接条数，不代表成员人数"
                />
                <Metric
                  icon={Box}
                  title="本机节点"
                  value={s.running ? "1 / 1" : "0 / 1"}
                  foot="每台电脑运行一个节点"
                />
              </div>
              <section className="panel">
                <PanelTitle icon={Box} title="最近区块" />
                <BlockTable
                  blocks={(s.blocks || []).slice(0, 8)}
                  onSelect={openBlock}
                />
              </section>
            </>
          )}
          {network && view === "network" && (
            <>
              <NetworkPanel key={network.id} status={s} action={action} />
              <section className="panel">
                <PanelTitle icon={Network} title="本机节点" />
                <Topology nodes={nodes} />
              </section>
              {nodes.map((node) => (
                <section className="panel padded" key={node.id}>
                  <PanelTitle
                    icon={Box}
                    title="本机节点"
                    right={
                      <span
                        className={"pill " + (node.online ? "teal" : "amber")}
                      >
                        {node.online ? "在线" : "未连接"}
                      </span>
                    }
                  />
                  <div className="node-meta">
                    <span>
                      入口端口 <b>{node.port}</b>
                    </span>
                    <span>
                      区块高度 <b>{node.height || "—"}</b>
                    </span>
                    <code>链头 {short(node.head, 18)}</code>
                  </div>
                  {(node.connections || []).map((peer: Row) => (
                    <div className="peer-row" key={peer.id}>
                      <Globe2 size={16} />
                      <code>
                        {peer.ipv6
                          ? `[${peer.host}]:${peer.port}`
                          : `${peer.host}:${peer.port}`}
                      </code>
                      <span className="pill">
                        {peer.ipv6 ? "IPv6" : "IPv4"}
                      </span>
                      <span>观测高度 {peer.height}</span>
                    </div>
                  ))}
                </section>
              ))}
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
                <Download size={16} />
                导出诊断
              </button>
            </>
          )}
        </div>
        <footer>
          <span>NornV2 学术聊天室</span>
          <span>消息公开写入当前链</span>
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
          <LoaderCircle size={18} className="spin" />
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
            className="modal"
            onClick={(event) => event.stopPropagation()}
          >
            <button
              className="modal-close icon-button"
              aria-label="关闭对话框"
              disabled={!!busy}
              onClick={() => setModal("")}
            >
              <X size={19} />
            </button>
            {error && (
              <div className="alert error" role="alert">
                {error}
              </div>
            )}
            {modal === "share" && network && <InviteShare key={network.id} status={s} action={action} />}
            {(modal === "create" || modal === "join") && (
              <>
                <span className="modal-icon">
                  <Globe2 size={26} />
                </span>
                <h2>
                  {modal === "join" ? "加入聊天室" : "创建聊天室"}
                </h2>
                <p>
                  {modal === "join"
                    ? "粘贴对方节点的 IP 和端口，软件自动获取链配置并同步聊天记录。"
                    : "本机启动一个节点并部署公共聊天室。任何加入的成员都可以发言、创建分组和分享邀请。"}
                </p>
                {modal === "join" ? (
                  <InviteInput text={invite} setText={value => { setInvite(value); setJoinPreview(null); }} status={s} onPreview={setJoinPreview} />
                ) : (
                  <label className="field">
                    聊天室名称
                    <input
                      aria-label="链名称"
                      value={title}
                      maxLength={80}
                      onChange={(event) => setTitle(event.target.value)}
                    />
                  </label>
                )}
                <label className="field">
                  显示昵称
                  <input
                    aria-label="显示昵称"
                    value={nickname}
                    placeholder={s.nickname || "成员"}
                    onChange={(event) => setNickname(event.target.value)}
                  />
                </label>
                {!s.unlocked && (
                  <label className="field">
                    {s.has_wallet ? "本机账户密码" : "设置本机账户密码"}
                    <input
                      aria-label="账户密码"
                      type="password"
                      autoComplete={
                        s.has_wallet ? "current-password" : "new-password"
                      }
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      placeholder={
                        s.has_wallet ? "输入账户密码" : "至少 8 个字节"
                      }
                    />
                  </label>
                )}
                <button
                  className="button primary full"
                  disabled={
                    !!busy ||
                    (modal === "join" ? !CanJoinPreview(joinPreview, joinPreview?.id || "") : !title.trim()) ||
                    bytes(nickname) > 48 ||
                    (!s.unlocked &&
                      (!password || (!s.has_wallet && bytes(password) < 8)))
                  }
                  onClick={() => void enter(modal === "join")}
                >
                  {modal === "join" ? "连接并加入" : "创建聊天室"}
                  <ArrowRight size={16} />
                </button>
                {modal === "join" && !invite.trim() && <button className="text-button" onClick={() => setModal("create")}>创建自己的聊天室</button>}
              </>
            )}
            {modal === "wallet" && (
              <>
                <h2>{s.has_wallet ? "登录本机账户" : "创建本机账户"}</h2>
                <p>账户身份和私钥保存在本机。</p>
                <form
                  onSubmit={async (event) => {
                    event.preventDefault();
                    try {
                      await action(s.has_wallet ? "unlock" : "wallet_create", {
                        password,
                      });
                      setPassword("");
                      setModal("");
                    } catch {}
                  }}
                >
                  <label className="field">
                    账户密码
                    <input
                      autoFocus
                      aria-label="账户密码"
                      type="password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      autoComplete={
                        s.has_wallet ? "current-password" : "new-password"
                      }
                    />
                  </label>
                  <button
                    className="button primary full"
                    disabled={
                      !!busy ||
                      !password ||
                      (!s.has_wallet && bytes(password) < 8)
                    }
                  >
                    {s.has_wallet ? "登录账户" : "创建账户"}
                  </button>
                </form>
              </>
            )}
            {modal === "account" && (
              <>
                <h2>显示昵称</h2>
                <label className="field">
                  昵称
                  <input
                    value={nickname || s.nickname || ""}
                    onChange={(event) => setNickname(event.target.value)}
                  />
                </label>
                <button
                  className="button primary full"
                  disabled={
                    !!busy ||
                    !String(nickname || s.nickname || "").trim() ||
                    bytes(nickname || s.nickname || "") > 48
                  }
                  onClick={async () => {
                    try {
                      await action("nickname", {
                        nickname: nickname || s.nickname,
                      });
                      setModal("");
                    } catch {}
                  }}
                >
                  保存昵称
                </button>
              </>
            )}
            {modal === "message" && inspected && (
              <>
                <h2>消息的链上记录</h2>
                <p>{inspected.body}</p>
                <dl className="details">
                  <dt>发送者</dt>
                  <dd className="mono">{inspected.sender}</dd>
                  <dt>交易哈希</dt>
                  <dd className="mono">{inspected.tx_hash}</dd>
                  <dt>区块高度</dt>
                  <dd>#{inspected.height}</dd>
                  <dt>状态</dt>
                  <dd>本机已上链，仍可能随链重组变化</dd>
                </dl>
                <button
                  className="button primary full"
                  onClick={() => {
                    setModal("");
                    void openBlock(inspected.height);
                  }}
                >
                  查看所在区块
                </button>
              </>
            )}
            {modal === "transaction" && selected && (
              <>
                <h2>交易记录</h2>
                <dl className="details">
                  <dt>交易哈希</dt>
                  <dd className="mono">{selected.hash}</dd>
                  <dt>状态</dt>
                  <dd>{TxText(selected)}</dd>
                  <dt>区块高度</dt>
                  <dd>{selected.height || "等待入块"}</dd>
                  <dt>账户序号</dt>
                  <dd>{selected.nonce || "—"}</dd>
                </dl>
                <details>
                  <summary>查看原始记录</summary>
                  <pre className="source-code">
                    {JSON.stringify(selected, null, 2)}
                  </pre>
                </details>
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
