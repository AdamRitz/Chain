import test from "node:test";
import assert from "node:assert/strict";
import { FirewallSummary, NetworkWarnings } from "../src/network.ts";
test("firewall status distinguishes configured, missing and invalid rules", () => {
  assert.match(
    FirewallSummary({
      rules: [
        {
          enabled: "True",
          action: "Allow",
          direction: "Inbound",
          protocol: "TCP",
        },
      ],
    }),
    /已配置入站规则/,
  );
  assert.match(FirewallSummary({ rules: [] }), /尚未配置/);
  assert.match(FirewallSummary({ error: "denied", rules: [] }), /失败/);
  assert.match(FirewallSummary({ rules: [{ enabled: "False" }] }), /重新配置/);
});
test("current network and managed-policy limitations are surfaced", () => {
  assert.equal(
    NetworkWarnings({
      rules: [{ profile: "Private" }],
      profiles: [{ category: "Public" }],
      active_rule_count: 0,
      policy: [{ name: "Public", allow_local_rules: "False" }],
    }).length,
    3,
  );
  assert.deepEqual(
    NetworkWarnings({
      rules: [{ profile: "Domain" }],
      profiles: [{ category: "DomainAuthenticated" }],
      active_rule_count: 1,
      policy: [],
    }),
    [],
  );
  assert.deepEqual(NetworkWarnings({ error: "unavailable" }), []);
});

test("all-network rules cover Public and Domain", () => {
  for (const profile of ["Any", "Domain, Private, Public"]) {
    const firewall = {
      rules: [
        {
          profile,
          enabled: "True",
          action: "Allow",
          direction: "Inbound",
          protocol: "TCP",
          remote: ["Any"],
        },
      ],
      profiles: [{ category: "Public" }, { category: "DomainAuthenticated" }],
      active_rule_count: 1,
      policy: [],
    };
    assert.deepEqual(NetworkWarnings(firewall), []);
    assert.match(FirewallSummary(firewall), /已允许所有地址连接/);
  }
});
