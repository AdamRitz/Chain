import { useEffect, useState } from "react";
import { Globe2, LoaderCircle, ShieldCheck } from "lucide-react";
import type { Row } from "./api";
import { FirewallSummary, NetworkWarnings, IPv6Summary } from "./network";

export function NetworkPanel({
  status,
  action,
}: {
  status: Row;
  action: (name: string, values?: Row, label?: string) => Promise<any>;
}) {
  const network = status.network;
  const tools = status.network_tools || {};
  const firewall = tools.firewall;
  const [endpoint, setEndpoint] = useState(network?.seeds?.[0] || "");
  const [requesting, setRequesting] = useState(false);
  const pending = requesting || tools.busy;
  const invoke = async (name: string, values: Row = {}) => {
    setRequesting(true);
    try {
      await action(name, values, "正在请求网络工具");
    } catch {
    } finally {
      setRequesting(false);
    }
  };
  useEffect(() => {
    void action("network_addresses").catch(() => {});
    if (!tools.checked && !tools.busy) void invoke("network_status");
  }, []);
  const probe =
    tools.probe?.network_id === network?.id ? tools.probe : undefined;
  return (
    <section className="panel padded network-setup">
      <div className="panel-title">
        <h3>
          <ShieldCheck size={18} />
          连接设置
        </h3>
        <span className="pill">Windows · IPv4 / IPv6</span>
      </div>
      <div className="diagnostic">
        <span>本机节点</span>
        <b>{status.running ? "运行中" : "未启动"}</b>
      </div>
      <div className="diagnostic">
        <span>防火墙</span>
        <b>
          {pending
            ? tools.action === "status"
              ? "正在检查…"
              : "处理中…"
            : FirewallSummary(firewall)}
        </b>
      </div>
      <div className="diagnostic"><span>本机 IPv6</span><b>{IPv6Summary(status.network_addresses)}</b></div>
      {firewall?.operation === "cancelled" && (
        <p className="network-warning" role="status">
          已取消授权
        </p>
      )}
      {firewall?.error && (
        <p className="network-warning" role="alert">
          {firewall.error}
        </p>
      )}
      {NetworkWarnings(firewall).map((w) => (
        <p className="network-warning" key={w}>
          {w}
        </p>
      ))}
      <>
        <div className="network-actions">
          <button
            className="button primary"
            disabled={pending}
            onClick={() =>
              void invoke("network_configure", {
                profile: "Any",
                remote: "Any",
              })
            }
          >
            <ShieldCheck size={16} />
            允许所有人连接
          </button>
          <button
            className="button secondary"
            disabled={pending}
            onClick={() => void invoke("network_status")}
          >
            重新检查
          </button>
          <button
            className="button secondary"
            disabled={pending || !firewall?.rules?.length}
            onClick={() => void invoke("network_remove")}
          >
            撤销本应用规则
          </button>
        </div>
      </>
      <details>
        <summary>防火墙规则与本机地址</summary>
        <p className="small mono">节点程序：{firewall?.node || "尚未读取"}</p>
        {(firewall?.profiles || []).map((p: Row, i: number) => (
          <p className="small" key={i}>{p.interface} · {p.category} · IPv4 {p.ipv4} / IPv6 {p.ipv6}</p>
        ))}
        {(firewall?.rules || []).map((r: Row) => (
          <p className="small" key={r.name}>
            {r.profile} · {r.direction} · {r.protocol} ·{" "}
            {r.enabled === "True" ? "启用" : "禁用"} · 成员地址：
            {r.remote.join(", ")}
          </p>
        ))}
        {(status.network_addresses?.addresses || firewall?.addresses || []).map((a: Row, i: number) => (
          <p className="small mono" key={i}>
            {a.interface} · {a.address}/{a.prefix} · {a.family}
          </p>
        ))}
      </details>
      <div className="network-probe">
        <h4>
          <Globe2 size={16} />
          检测连接
        </h4>
        <label className="field">
          节点地址
          <input
            value={endpoint}
            maxLength={300}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder="192.168.1.20:端口 或 [IPv6地址]:端口"
          />
        </label>
        <button
          className="button secondary"
          disabled={requesting || tools.probing || !endpoint.trim()}
          onClick={() =>
            void invoke("network_probe", { endpoint: endpoint.trim() })
          }
        >
          {tools.probing ? (
            <LoaderCircle size={16} className="spin" />
          ) : (
            <Globe2 size={16} />
          )}
          检测连接
        </button>
        {probe && (
          <p
            role="status"
            className={probe.ok ? "small text-teal" : "network-warning"}
          >
            {probe.endpoint}：
            {probe.ok
              ? `已连接 · 区块 ${probe.height} · ${probe.elapsed_ms} ms`
              : `连接失败：${probe.error}`}
          </p>
        )}
      </div>
    </section>
  );
}
