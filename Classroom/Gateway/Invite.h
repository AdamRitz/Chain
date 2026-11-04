#pragma once
#include "Addresses.h"

constexpr size_t MaxInviteBytes = 256 * 1024;
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
    start += 6; auto end = text.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_", start);
    auto encoded = text.substr(start, end == string::npos ? end : end - start);
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
            Json result = {{"endpoint", endpoint}, {"ok", false}};
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
