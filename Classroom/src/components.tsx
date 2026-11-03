import React from "react";
import {
  Activity,
  ArrowUpRight,
  Box,
  Check,
  CheckCircle2,
  ChevronRight,
  Copy,
  Layers3,
} from "lucide-react";
import { number, short, type Row } from "./api";
const stateText: Row = {
  submitted: "等待入块",
  included: "本机已上链",
  reorganized: "退出主链，待重新入块",
};
export function PanelTitle({
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
export function BlockTable({
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
export function Outbox({
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
            className={
              "pill " +
              (tx.execution_status
                ? "amber"
                : tx.state === "included"
                  ? "teal"
                  : "amber")
            }
          >
            {tx.execution_status ? "执行失败" : stateText[tx.state] || tx.state}
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
export function Topology({ nodes }: { nodes: Row[] }) {
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
export function Sparkline({
  samples,
}: {
  samples: { t: number; v: number }[];
}) {
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

export function Metric({
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
