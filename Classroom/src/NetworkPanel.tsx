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
        <b>{status.running ? "本机 1 个节点运行中" : "未启动"}</b>
      </div>
      <div className="diagnostic">
        <span>防火墙</span>
        <b>
          {pending
            ? tools.action === "status"
              ? "正在检查…"
              : "处理中，请留意 Windows 权限提示…"
            : FirewallSummary(firewall)}
        </b>
      </div>
      <div className="diagnostic">
        <span>外部电脑能否连入本机</span>
        <b>需要从另一台电脑检测</b>
      </div>
      <div className="diagnostic"><span>本机 IPv6</span><b>{IPv6Summary(status.network_addresses)}</b></div>
      {firewall?.operation === "cancelled" && (
        <p className="network-warning" role="status">
          已取消管理员授权。节点仍可使用，可以稍后再配置。
        </p>
      )}
      {firewall?.operation === "unchanged" && (
        <p className="small text-teal">规则已符合本次请求，无需再次授权。</p>
      )}
      {firewall?.operation === "completed" && (
        <p className="small text-teal">
          操作已完成，已重新读取 Windows 防火墙配置。
        </p>
      )}
      {firewall?.error && (
        <p className="network-warning" role="alert">
          {firewall.error}。受组织管理的电脑请联系管理员。
        </p>
      )}
      {NetworkWarnings(firewall).map((w) => (
        <p className="network-warning" key={w}>
          {w}
        </p>
      ))}
      {(firewall?.profiles || []).map((p: Row, i: number) => (
        <p className="small muted" key={i}>
          当前连接：{p.interface} · {p.category} · IPv4 {p.ipv4} / IPv6 {p.ipv6}
        </p>
      ))}
      <>
        <p className="small muted">
          点击下方按钮并确认 Windows 授权，即可允许所有地址连接本机的 NornV2
          节点。适用于专用、公用和域网络，支持 IPv4 / IPv6，无需逐个添加成员。
        </p>
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
        <p className="small muted">
          放行仅针对本应用节点的 TCP
          入站连接。校园网或路由器是否允许外部连接，可用下方检测工具核实。
        </p>
      </>
      <details>
        <summary>查看规则范围和本机地址</summary>
        <p className="small mono">节点程序：{firewall?.node || "尚未读取"}</p>
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
        <p className="small muted">
          请选用成员可达的网卡地址。链路本地 IPv6 地址（fe80
          开头）需要接口标识，不能直接作为跨网段入口。
        </p>
      </details>
      <div className="network-probe">
        <h4>
          <Globe2 size={16} />
          检测另一台电脑的节点
        </h4>
        <label className="field">
          对方链入口
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
              ? `节点查询成功，区块高度 ${probe.height}，往返 ${probe.elapsed_ms} ms`
              : `未能查询节点：${probe.error}`}
          </p>
        )}
        <p className="small muted">
          此检测方向是“本机 →
          填写的节点”，成功不代表属于同一个链。请让成员在他们的电脑上填写你的入口，验证能否连入你。使用本机地址测试不能证明外部可达。
        </p>
      </div>
      <details>
        <summary>系统管理员与故障排查</summary>
        <p className="small muted">
          校园网、路由器或网络策略仍可能阻止入站。可把安装目录 runtime 中的
          Configure-Network.ps1 交给管理员，使用 status 检查、configure
          配置、remove
          撤销；脚本与界面使用同一套规则。无需为本机管理网页开放端口。
        </p>
      </details>
    </section>
  );
}
