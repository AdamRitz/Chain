import type { Row } from "./api";

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
    if (rules.length && !rules.some((r) => r.profile === categories[i]))
      warnings.push(
        `${p.interface || "当前连接"}（${categories[i]}）不在规则的网络类型内。若课堂使用此连接，请选择对应类型后重新配置。`,
      );
  });
  if (rules.length && firewall.active_rule_count === 0)
    warnings.push("规则尚未出现在有效策略中，请联系学校管理员检查组策略。");
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
