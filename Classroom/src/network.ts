import type { Row } from "./api";

export function IPv6Summary(addresses: Row | undefined): string {
  const v6 = addresses?.ipv6;
  if (!v6) return "尚未检查";
  if (addresses?.ipv6_listening === false) return "节点尚未监听 IPv6，请恢复节点后重试";
  if (!v6.global_addresses) return v6.link_local_addresses ? "仅链路本地 IPv6，不能用于跨网络连接" : "未检测到可用的全局 IPv6 地址";
  if (!v6.route_checked) return "已获取 IPv6 地址，默认路由状态待核实";
  return v6.default_route ? "已获取全局 IPv6 和默认路由，外部可达性待验证" : "已获取 IPv6 地址，但没有 IPv6 默认路由";
}
export function InviteEndpointText(entry: Row): string {
  if (entry.ok) return "连接与聊天室检查通过";
  return ({no_route:"无可用路由",refused:"端口拒绝连接",timeout:"连接超时",dns:"域名无法解析",wrong_chain:"不属于这条链",wrong_contract:"聊天室合约不匹配",configuration:"链配置不可用",unreachable:"入口不可达"} as Record<string,string>)[entry.code] || "连接失败";
}

export function FirewallSummary(firewall: Row | undefined) {
  if (!firewall) return "尚未检查";
  if (firewall.error) return "检查或配置失败";
  if (firewall.supported === false) return "请使用系统管理员配置";
  const rules: Row[] = firewall.rules || [];
  if (!rules.length) return "尚未配置本应用规则";
  if (
    !rules.some(
      (r) =>
        r.enabled === "True" &&
        r.action === "Allow" &&
        r.direction === "Inbound" &&
        ["TCP", "6"].includes(r.protocol),
    )
  )
    return "现有规则需要重新配置";
  if (
    rules.some(
      (r) =>
        r.enabled === "True" &&
        r.action === "Allow" &&
        r.direction === "Inbound" &&
        ["TCP", "6"].includes(r.protocol) &&
        ["Any", "Domain, Private, Public"].includes(r.profile) &&
        r.remote?.includes("Any"),
    )
  )
    return "已允许所有地址连接，外部可达性待验证";
  return "已保存本应用规则，入站可达性待验证";
}

export function NetworkWarnings(firewall: Row | undefined): string[] {
  if (!firewall || firewall.error) return [];
  const warnings: string[] = [];
  const rules: Row[] = firewall.rules || [];
  const profiles: Row[] = firewall.profiles || [];
  const categories = profiles.map((p) =>
    p.category === "DomainAuthenticated" ? "Domain" : p.category,
  );
  profiles.forEach((p, i) => {
    if (
      rules.length &&
      !rules.some(
        (r) =>
          r.profile === "Any" ||
          String(r.profile)
            .split(",")
            .map((p) => p.trim())
            .includes(categories[i]),
      )
    )
      warnings.push(
        `${p.interface || "当前连接"}（${categories[i]}）不在旧规则的网络类型内，点击“允许所有人连接”即可更新。`,
      );
  });
  if (rules.length && firewall.active_rule_count === 0)
    warnings.push("规则尚未出现在有效策略中，请联系系统管理员检查组策略。");
  for (const p of firewall.policy || []) {
    if (!categories.includes(p.name)) continue;
    if (p.allow_local_rules === "False" || p.allow_inbound === "False")
      warnings.push(`${p.name} 网络策略限制本地规则或入站连接，请联系管理员。`);
    if (p.enabled === "False")
      warnings.push(
        `${p.name} 网络的 Windows 防火墙当前未启用；本应用未更改该设置。`,
      );
  }
  return warnings;
}
