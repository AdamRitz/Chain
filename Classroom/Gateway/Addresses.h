#pragma once
#include "Support.h"
#include <set>
#ifdef _WIN32
#include <iphlpapi.h>
#else
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>
#endif

// Address scope is determined from parsed bytes, never text prefixes. An
// Internet candidate is NOT proof of inbound reachability from another host.
string InviteScope(const ip::address &input) {
    auto a = CanonicalAddress(input);
    if (a.is_loopback() || a.is_unspecified() || a.is_multicast()) return "excluded";
    if (a.is_v6()) {
        auto v = a.to_v6(); auto b = v.to_bytes();
        if (v.is_link_local() || v.scope_id()) return "excluded";
        if ((b[0] & 0xfe) == 0xfc) return "private";
        if ((b[0] & 0xe0) != 0x20 ||
            (b[0] == 0x20 && b[1] == 1 && (b[2] < 2 || (b[2] == 0x0d && b[3] == 0xb8))) ||
            (b[0] == 0x3f && b[1] == 0xff && (b[2] & 0xf0) == 0)) return "excluded";
        return "internet_candidate";
    }
    auto b = a.to_v4().to_bytes();
    if (!b[0] || b[0] == 127 || b[0] >= 224 || (b[0] == 169 && b[1] == 254) ||
        (b[0] == 192 && b[1] == 0 && (b[2] == 0 || b[2] == 2)) ||
        (b[0] == 198 && (b[1] == 18 || b[1] == 19 || (b[1] == 51 && b[2] == 100))) ||
        (b[0] == 203 && b[1] == 0 && b[2] == 113)) return "excluded";
    if (b[0] == 10 || (b[0] == 172 && b[1] >= 16 && b[1] <= 31) ||
        (b[0] == 192 && b[1] == 168) || (b[0] == 100 && b[1] >= 64 && b[1] <= 127)) return "lan";
    return "internet_candidate";
}
bool HostOnlyAdapter(string name) {
    transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return char(tolower(c)); });
    return name.find("vmware") != string::npos || name.find("virtualbox") != string::npos ||
           name.find("host-only") != string::npos || name.find("vethernet") != string::npos;
}
Json SelectInviteAddresses(Json addresses, uint16_t port) {
    vector<Json> selected; set<string> seen;
    for (auto &a : addresses) {
        boost::system::error_code ec;
        auto address = ip::make_address(a.at("address").get<string>(), ec);
        auto scope = ec ? string("excluded") : InviteScope(address);
        a["scope"] = scope;
        a["family"] = !ec && address.is_v6() ? "IPv6" : "IPv4";
        a["endpoint"] = ec ? string{} : FormatEndpoint(CanonicalAddress(address).to_string(), port);
        a["selected"] = false;
        if (scope == "excluded" || !a.value("preferred", false) || !a.value("up", false) ||
            a.value("skip_as_source", false) || HostOnlyAdapter(a.value("interface", string{}))) continue;
        // Default-route preference does not discard usable same-site addresses.
        int score = scope == "internet_candidate" && a.value("default_route", false) ? 0 :
                    scope == "lan" ? 30 : scope == "private" ? 50 : 40;
        if (a["family"] == "IPv4") score += 5;
        if (a.value("preferred_seconds", uint64_t(3600)) < 300) score += 20;
        a["score"] = score;
        selected.push_back(a);
    }
    stable_sort(selected.begin(), selected.end(), [](const Json &a, const Json &b) {
        if (a["score"] != b["score"]) return a["score"] < b["score"];
        return a.value("metric", 0u) < b.value("metric", 0u);
    });
    Json endpoints = Json::array(); bool internet = false, picked4 = false, picked6 = false;
    bool has4 = false, has6 = false;
    for (const auto &a : selected) { has4 |= a["family"] == "IPv4"; has6 |= a["family"] == "IPv6"; }
    for (const auto &a : selected) {
        // Reserve the last slot for the other family, even on adapters with
        // many temporary IPv6 addresses. Keep an actual dual-stack fallback.
        if (endpoints.size() == 7 && ((a["family"] == "IPv6" && has4 && !picked4) ||
                                     (a["family"] == "IPv4" && has6 && !picked6))) continue;
        auto endpoint = a.at("endpoint").get<string>();
        if (endpoints.size() < 8 && seen.insert(endpoint).second) {
            endpoints.push_back(endpoint);
            picked4 |= a["family"] == "IPv4"; picked6 |= a["family"] == "IPv6";
            if (a["scope"] == "internet_candidate" && a.value("default_route", false)) internet = true;
        }
    }
    for (auto &a : addresses) a["selected"] = seen.count(a["endpoint"].get<string>()) != 0;
    return {{"addresses", addresses}, {"seeds", endpoints}, {"checked_ms", NowMs()},
            {"scope", internet ? "internet_candidate" : endpoints.empty() ? "none" : "local"},
            {"external_verified", false}};
}
Json CollectInviteAddresses(uint16_t port) {
    Json addresses = Json::array();
#ifdef _WIN32
    ULONG size = 16384; vector<unsigned char> buffer(size);
    ULONG result = ERROR_BUFFER_OVERFLOW;
    for (unsigned attempt = 0; attempt < 3 && result == ERROR_BUFFER_OVERFLOW; attempt++) {
        buffer.resize(size);
        result = GetAdaptersAddresses(AF_UNSPEC, GAA_FLAG_INCLUDE_GATEWAYS, nullptr,
                                     reinterpret_cast<IP_ADAPTER_ADDRESSES *>(buffer.data()), &size);
    }
    if (result != NO_ERROR) throw runtime_error("无法读取本机网卡地址，请重试");
    for (auto *a = reinterpret_cast<IP_ADAPTER_ADDRESSES *>(buffer.data()); a; a = a->Next) {
        bool route4 = false, route6 = false;
        for (auto *g = a->FirstGatewayAddress; g; g = g->Next) {
            if (g->Address.lpSockaddr->sa_family == AF_INET) route4 = true;
            if (g->Address.lpSockaddr->sa_family == AF_INET6) route6 = true;
        }
        auto name = a->FriendlyName ? PathUtf8(fs::path(a->FriendlyName)) : string{};
        for (auto *u = a->FirstUnicastAddress; u; u = u->Next) {
            auto *s = u->Address.lpSockaddr; string text; bool v6 = s->sa_family == AF_INET6;
            MIB_UNICASTIPADDRESS_ROW row{}; row.InterfaceLuid = a->Luid;
            if (v6) {
                auto *v = reinterpret_cast<sockaddr_in6 *>(s); ip::address_v6::bytes_type b;
                memcpy(b.data(), &v->sin6_addr, 16); text = ip::address_v6(b, v->sin6_scope_id).to_string();
                memcpy(&row.Address.Ipv6, v, sizeof(*v));
            } else if (s->sa_family == AF_INET) {
                auto *v = reinterpret_cast<sockaddr_in *>(s); ip::address_v4::bytes_type b;
                memcpy(b.data(), &v->sin_addr, 4); text = ip::address_v4(b).to_string();
                memcpy(&row.Address.Ipv4, v, sizeof(*v));
            } else continue;
            bool skip = GetUnicastIpAddressEntry(&row) == NO_ERROR && row.SkipAsSource;
            addresses.push_back({{"interface", name}, {"address", text}, {"prefix", u->OnLinkPrefixLength},
                {"up", a->OperStatus == IfOperStatusUp}, {"preferred", u->DadState == IpDadStatePreferred},
                {"preferred_seconds", u->PreferredLifetime}, {"metric", v6 ? a->Ipv6Metric : a->Ipv4Metric},
                {"default_route", v6 ? route6 : route4}, {"skip_as_source", skip}});
        }
    }
#else
    ifaddrs *list = nullptr;
    if (getifaddrs(&list)) throw runtime_error("无法读取本机网卡地址");
    for (auto *a = list; a; a = a->ifa_next) {
        if (!a->ifa_addr) continue;
        char text[INET6_ADDRSTRLEN]{}; int family = a->ifa_addr->sa_family;
        if (family == AF_INET) inet_ntop(family, &reinterpret_cast<sockaddr_in *>(a->ifa_addr)->sin_addr, text, sizeof(text));
        else if (family == AF_INET6) inet_ntop(family, &reinterpret_cast<sockaddr_in6 *>(a->ifa_addr)->sin6_addr, text, sizeof(text));
        else continue;
        addresses.push_back({{"interface", a->ifa_name}, {"address", text}, {"up", bool(a->ifa_flags & IFF_UP)},
                             {"preferred", true}, {"default_route", false}});
    }
    freeifaddrs(list);
#endif
    return SelectInviteAddresses(addresses, port);
}
