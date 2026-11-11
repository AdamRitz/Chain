#include "../Tool/ChatFunding.h"
#include <algorithm>
#include <iostream>
#include <limits>
#include <vector>

using Json = nlohmann::json;
size_t checks = 0;
void Require(bool value, const char *message) {
    if (!value) throw std::runtime_error(message);
    ++checks;
}
std::string Hex(const unsigned char *bytes, size_t size) {
    std::vector<char> output(size * 2 + 1);
    sodium_bin2hex(output.data(), output.size(), bytes, size);
    return {output.data(), size * 2};
}
void Sign(Json &request, const std::array<unsigned char, crypto_sign_SECRETKEYBYTES> &secret) {
    auto message = FundingRequestMessage(request);
    std::array<unsigned char, crypto_sign_BYTES> signature{};
    crypto_sign_detached(signature.data(), nullptr, reinterpret_cast<const unsigned char *>(message.data()), message.size(), secret.data());
    request["signature"] = Hex(signature.data(), signature.size());
}
int main() {
    try {
        Require(sodium_init() >= 0, "sodium initialization");
        std::array<unsigned char, crypto_sign_PUBLICKEYBYTES> publicKey{}, sponsorKey{};
        std::array<unsigned char, crypto_sign_SECRETKEYBYTES> secret{}, sponsorSecret{};
        crypto_sign_keypair(publicKey.data(), secret.data());
        crypto_sign_keypair(sponsorKey.data(), sponsorSecret.data());
        const uint64_t now = 1700000000000ULL;
        const std::string genesis(64, '1'), contract(40, '2'), sponsor = Hex(sponsorKey.data(), sponsorKey.size());
        Json valid = {{"method", "funding_request"}, {"genesis_root", genesis}, {"contract", contract},
                      {"public_key", Hex(publicKey.data(), publicKey.size())}, {"sponsor", sponsor},
                      {"nonce", std::string(32, '4')}, {"timestamp_ms", now}};
        auto message = FundingRequestMessage(valid);
        Require(message == "NORN-CHAT-BOOTSTRAP-V1\n" + genesis + "\n" + contract + "\n" +
                valid["public_key"].get<std::string>() + "\n" + sponsor + "\n" + std::string(32, '4') + "\n1700000000000\n",
                "canonical field order, decimal milliseconds and final newline");
        Sign(valid, secret);
        auto validate = [&](const Json &request, uint64_t at = 1700000000000ULL) {
            return ValidateFundingRequest(request, genesis, contract, sponsor, at);
        };
        Require(validate(valid), "valid ownership proof");
        Json uppercase = valid;
        for (const auto *field : {"genesis_root", "contract", "public_key", "sponsor", "nonce", "signature"}) {
            auto text = uppercase[field].get<std::string>();
            for (auto &c : text) if (c >= 'a' && c <= 'f') c = char(c - 'a' + 'A');
            uppercase[field] = text;
        }
        Require(validate(uppercase) && FundingRequestMessage(uppercase) == message, "hex case normalizes without changing signed message");
        for (const auto *field : {"genesis_root", "contract", "public_key", "sponsor", "nonce", "signature"}) {
            Json changed = valid;
            auto text = changed[field].get<std::string>();
            text[0] = text[0] == '0' ? '1' : '0'; changed[field] = text;
            Require(!validate(changed), "tampered signed field rejected");
        }
        Json changed = valid; changed["timestamp_ms"] = now + 1;
        Require(!validate(changed), "timestamp is signed");
        Require(!ValidateFundingRequest(valid, std::string(64, '3'), contract, sponsor, now), "cross-chain proof rejected");
        Require(!ValidateFundingRequest(valid, genesis, std::string(40, '3'), sponsor, now), "cross-contract proof rejected");
        Require(!ValidateFundingRequest(valid, genesis, contract, Hex(publicKey.data(), publicKey.size()), now), "wrong provider rejected");
        changed = valid; Sign(changed, sponsorSecret);
        Require(!validate(changed), "another identity cannot sign the requester proof");
        changed = valid; changed["sponsor"] = changed["public_key"]; Sign(changed, secret);
        Require(!ValidateFundingRequest(changed, genesis, contract, changed["sponsor"].get<std::string>(), now), "self-sponsored request rejected");
        changed = valid; changed["timestamp_ms"] = now - ChatFundingMaxAgeMs; Sign(changed, secret);
        Require(validate(changed), "maximum allowed age accepted");
        Require(!validate(changed, now + 1), "expired request rejected");
        changed = valid; changed["timestamp_ms"] = now + ChatFundingFutureSkewMs; Sign(changed, secret);
        Require(validate(changed), "maximum allowed future clock skew accepted");
        Require(!validate(changed, now - 1), "excess future timestamp rejected");
        changed = valid; changed["timestamp_ms"] = std::numeric_limits<uint64_t>::max(); Sign(changed, secret);
        Require(!validate(changed, 1), "future timestamp comparison cannot overflow");
        changed = valid; changed["timestamp_ms"] = uint64_t(0); Sign(changed, secret);
        Require(!validate(changed, std::numeric_limits<uint64_t>::max()), "old timestamp comparison cannot overflow");
        for (const auto &bad : std::vector<Json>{-1, 1.25, "1700000000000", nullptr, Json::array()}) {
            changed = valid; changed["timestamp_ms"] = bad;
            Require(!validate(changed), "invalid timestamp type rejected");
        }
        for (const auto *field : {"genesis_root", "contract", "public_key", "sponsor", "nonce", "signature"}) {
            for (const auto &bad : std::vector<Json>{std::string(""), std::string("gg"), 1, nullptr}) {
                changed = valid; changed[field] = bad;
                Require(!validate(changed), "malformed hexadecimal field rejected");
            }
            changed = valid; changed.erase(field);
            Require(!validate(changed), "missing required field rejected");
        }
        changed = valid; changed["padding"] = std::string(ChatFundingMaxRequestBytes, 'x');
        Require(!validate(changed), "oversized request rejected");
        Require(!validate(Json::array()), "non-object request rejected");
        Require(ChatGenesisSupply < (uint64_t(1) << 53) && ChatTreasuryAmount < ChatGenesisSupply &&
                ChatGrantAmount < ChatTreasuryAmount && ChatBootstrapAmount > ChatClaimGas && ChatClaimGas >= 21000,
                "funding constants fit exact browser integers and reserve claim gas");
        sodium_memzero(secret.data(), secret.size()); sodium_memzero(sponsorSecret.data(), sponsorSecret.size());
        std::cout << "PASS " << checks << " chat funding checks\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
