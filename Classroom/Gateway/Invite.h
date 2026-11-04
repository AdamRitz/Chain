#pragma once
#include "Addresses.h"
#include <optional>

constexpr size_t MaxInviteBytes = 256 * 1024;
optional<string> JoinAddress(const Json &input) {
    if (!input.is_string()) return nullopt;
    auto text = input.get<string>();
    auto first = text.find_first_not_of(" \r\n\t");
    if (first == string::npos) throw invalid_argument("请输入节点 IP 地址和端口");
    text = text.substr(first, text.find_last_not_of(" \r\n\t") - first + 1);
    if (text.find("NORN1:") != string::npos || text.front() == '{') return nullopt;
    try {
        auto [host, port] = ParseEndpoint(text);
        boost::system::error_code ec; auto parsed = ip::make_address(host, ec);
        if (!ec) {
            auto address = CanonicalAddress(parsed);
            if (address.is_unspecified() || address.is_multicast()) throw invalid_argument("Unusable address");
            host = address.to_string();
        } else if (host.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-") != string::npos)
            throw invalid_argument("Invalid hostname");
        return FormatEndpoint(host, port);
    } catch (...) { throw invalid_argument("请输入 IPv4:端口 或 [IPv6]:端口，例如 192.168.1.10:8787"); }
}
Json FetchNetworkInvite(const string &endpoint) {
    auto [host, port] = ParseEndpoint(endpoint);
    auto request = Json({{"method", "network"}}).dump();
    auto bytes = Rpc(host, port, 46, {request.begin(), request.end()}, true, 3, MaxInviteBytes);
    auto response = Json::parse(bytes);
    if (!response.contains("network")) throw invalid_argument("对方节点暂不支持地址加入或聊天室尚未就绪，请让对方更新客户端并保持聊天室运行；旧版可使用完整邀请码。");
    // Never follow endpoints supplied by an untrusted descriptor: the address
    // entered by the user remains the bootstrap and synchronization seed.
    Json invite;
    for (const auto *key : {"version", "title", "genesis", "genesis_root", "contract", "contract_code_sha256"})
        invite[key] = response.at("network").at(key);
    invite["seeds"] = Json::array({endpoint});
    return invite;
}
Json InviteEndpoint(const string &endpoint) {
    auto [host, port] = ParseEndpoint(endpoint); boost::system::error_code ec;
    auto address = ip::make_address(host, ec);
    return {{"endpoint", endpoint}, {"ok", false}, {"family", ec ? "DNS" : address.is_v6() ? "IPv6" : "IPv4"},
            {"scope", ec ? "hostname" : InviteScope(address)}};
}
void InviteNetworkError(Json &result, const boost::system::system_error &e) {
    auto code = e.code(); result["system_code"] = code.value();
    result["code"] = code == error::network_unreachable || code == error::host_unreachable ? "no_route" :
        code == error::connection_refused ? "refused" : code == error::timed_out ? "timeout" :
        code == error::host_not_found || code == error::host_not_found_try_again ? "dns" : "unreachable";
}
Json InviteEnvelope(const Json &invite) {
    auto body = invite.dump(); array<unsigned char, 32> hash;
    crypto_hash_sha256(hash.data(), reinterpret_cast<const unsigned char *>(body.data()), body.size());
    return {{"format", "nornv2-invite"}, {"format_version", 1}, {"invite", invite}, {"checksum", EvmHex(hash)}};
}
Json DecodeInvite(const Json &input) {
    if (input.is_object()) {
        if (input.dump().size() > MaxInviteBytes) throw invalid_argument("邀请文件过大");
        if (input.contains("format")) {
            if (input.value("format", string{}) != "nornv2-invite" || input.value("format_version", 0) != 1)
                throw invalid_argument("这个邀请需要更新 NornV2 后才能使用");
            if (!input.contains("invite") || !input["invite"].is_object() || input["invite"].contains("format"))
                throw invalid_argument("邀请内容不完整，请重新复制");
            auto envelope = InviteEnvelope(input["invite"]);
            if (!input.contains("checksum") || input["checksum"] != envelope["checksum"])
                throw invalid_argument("邀请内容已损坏，请重新复制");
            return input["invite"];
        }
        return input;
    }
    if (!input.is_string()) throw invalid_argument("请粘贴朋友发来的完整邀请");
    auto text = input.get<string>();
    if (text.size() > MaxInviteBytes * 2) throw invalid_argument("邀请内容过大");
    auto start = text.find("NORN1:");
    if (start == string::npos) {
        try { return DecodeInvite(Json::parse(text)); }
        catch (...) { throw invalid_argument("邀请没有复制完整，请让朋友重新复制发送"); }
    }
    start += 6; string encoded;
    for (size_t i = start; i < text.size(); ++i) {
        auto c = static_cast<unsigned char>(text[i]);
        if (c == ' ' || c == '\r' || c == '\n' || c == '\t') continue;
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_') encoded.push_back(char(c));
        else break;
    }
    vector<unsigned char> decoded(encoded.size()); size_t size = 0;
    if (encoded.empty() || sodium_base642bin(decoded.data(), decoded.size(), encoded.data(), encoded.size(),
        nullptr, &size, nullptr, sodium_base64_VARIANT_URLSAFE_NO_PADDING) || size > MaxInviteBytes)
        throw invalid_argument("邀请没有复制完整，请让朋友重新复制发送");
    try {
        return DecodeInvite(Json::parse(decoded.begin(), decoded.begin() + size));
    } catch (const invalid_argument &) { throw; }
    catch (...) { throw invalid_argument("邀请没有复制完整，请让朋友重新复制发送"); }
}
string EncodeInvite(const Json &invite) {
    auto text = InviteEnvelope(invite).dump();
    vector<char> encoded(sodium_base64_ENCODED_LEN(text.size(), sodium_base64_VARIANT_URLSAFE_NO_PADDING));
    sodium_bin2base64(encoded.data(), encoded.size(), reinterpret_cast<const unsigned char *>(text.data()), text.size(),
                      sodium_base64_VARIANT_URLSAFE_NO_PADDING);
    return "NORN1:" + string(encoded.data());
}
Json ProbeInvite(const Json &invite, const string &runtime) {
    vector<future<Json>> tasks; unsigned index = 0;
    for (const auto &seed : invite.at("seeds")) {
        auto endpoint = seed.get<string>(); auto [host, port] = ParseEndpoint(endpoint);
        auto delay = index++ * 100;
        tasks.push_back(async(launch::async, [invite, runtime, endpoint, host, port, delay] {
            this_thread::sleep_for(std::chrono::milliseconds(delay));
            Json result = InviteEndpoint(endpoint);
            try {
                auto head = Rpc(host, port, 19, {}, true, 2, 80);
                if (head.size() != 80 || EvmHex(span<const uint8_t>(head).first(32)) != invite.at("genesis_root").get<string>()) {
                    result["code"] = "wrong_chain"; return result;
                }
                auto request = Json({{"method", "code"}, {"to", invite.at("contract")}}).dump();
                auto bytes = Rpc(host, port, 46, {request.begin(), request.end()}, true, 2, runtime.size() + 4096);
                if (Json::parse(bytes).value("code", string{}) != runtime) {
                    result["code"] = "wrong_contract"; return result;
                }
                result["ok"] = true; result["height"] = ReadU64(head.data() + 64);
            } catch (const boost::system::system_error &e) {
                InviteNetworkError(result, e);
            } catch (...) { result["code"] = "unreachable"; }
            return result;
        }));
    }
    Json results = Json::array(), seeds = Json::array(); uint64_t height = 0;
    for (auto &task : tasks) {
        auto result = task.get(); results.push_back(result);
        if (result["ok"] == true) { seeds.push_back(result["endpoint"]); height = max(height, result["height"].get<uint64_t>()); }
    }
    return {{"endpoints", results}, {"seeds", seeds}, {"height", height}, {"ok", !seeds.empty()}};
}
Json CheckJoinSource(const Json &source, const string &runtime1, const string &runtime2) {
    Json endpoint;
    try {
        if (source.is_string()) endpoint = InviteEndpoint(source.get<string>());
        auto invite = source.is_string() ? FetchNetworkInvite(source.get<string>()) : source;
        auto version = invite.at("version").get<int>();
        if (version != 1 && version != 2) throw invalid_argument("对方的聊天室版本需要更新客户端后才能加入");
        auto result = ProbeInvite(invite, version == 1 ? runtime1 : runtime2);
        result["invite"] = invite;
        return result;
    } catch (const boost::system::system_error &e) {
        if (endpoint.is_object()) InviteNetworkError(endpoint, e);
        return {{"ok", false}, {"endpoints", endpoint.is_object() ? Json::array({endpoint}) : Json::array()}};
    } catch (const invalid_argument &e) {
        if (endpoint.is_object()) endpoint["code"] = "configuration";
        return {{"ok", false}, {"error", e.what()}, {"endpoints", endpoint.is_object() ? Json::array({endpoint}) : Json::array()}};
    } catch (...) {
        if (endpoint.is_object()) endpoint["code"] = "configuration";
        return {{"ok", false}, {"error", "对方返回的链配置无效或不完整，请让对方更新客户端后重新连接。"},
                {"endpoints", endpoint.is_object() ? Json::array({endpoint}) : Json::array()}};
    }
}
string InviteFailureMessage(const Json &result, const Json &local) {
    bool global6 = false, refused = false, timeout = false, noRoute = false, dns = false;
    for (const auto &entry : result.at("endpoints")) {
        auto code = entry.value("code", string{});
        if (code == "wrong_chain" || code == "wrong_contract") return "入口对应的聊天室与邀请码不一致，请重新复制正确的邀请码。";
        global6 |= entry.value("family", string{}) == "IPv6" && entry.value("scope", string{}) == "internet_candidate";
        refused |= code == "refused"; timeout |= code == "timeout"; noRoute |= code == "no_route"; dns |= code == "dns";
    }
    if (global6 && local.value("route_checked", false) && !local.value("default_route", false) && !local.value("global_addresses", 0u))
        return "邀请码包含公网 IPv6 入口，但本机只有局域网 IPv6 或未获得 IPv6 地址，且没有 IPv6 默认路由。请先让路由器或网络提供 IPv6，或使用双方可达的 IPv4 入口。";
    if (refused) return "对方节点端口拒绝连接，请确认对方的 NornV2 正在运行，并重新复制当前邀请码。";
    if (noRoute) return "本机没有到邀请码入口的可用网络路由，请检查双方是否处于可互通网络。";
    if (dns) return "邀请码中的节点域名无法解析，请检查网络或重新获取邀请码。";
    if (timeout) return "连接对方节点超时，请检查对方的入站规则、路由器或校园网隔离，以及邀请码中的地址是否仍有效。";
    return "暂时联系不上聊天室。局域网入口需要处于同一可互通网络；请让对方保持程序运行并发送当前邀请码。";
}
