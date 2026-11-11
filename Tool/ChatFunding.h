#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <nlohmann/json.hpp>
#include <sodium.h>

inline constexpr uint64_t ChatGenesisSupply = 9000000000000000ULL;
inline constexpr uint64_t ChatTreasuryAmount = 8000000000000000ULL;
inline constexpr uint64_t ChatGrantAmount = 1000000000000ULL;
inline constexpr uint64_t ChatBootstrapAmount = 2000000ULL;
inline constexpr uint64_t ChatClaimGas = 250000ULL;
inline constexpr uint64_t ChatFundingMaxAgeMs = 300000;
inline constexpr uint64_t ChatFundingFutureSkewMs = 30000;
inline constexpr size_t ChatFundingMaxRequestBytes = 4096;

namespace ChatFundingDetail {
inline std::string Hex(const nlohmann::json &value, size_t bytes) {
    if (!value.is_string()) throw std::invalid_argument("Funding field must be hexadecimal text");
    auto text = value.get<std::string>();
    if (text.size() != bytes * 2) throw std::invalid_argument("Invalid funding hexadecimal length");
    for (auto &c : text) {
        if (c >= 'A' && c <= 'F') c = char(c - 'A' + 'a');
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
            throw std::invalid_argument("Invalid funding hexadecimal text");
    }
    return text;
}
inline uint64_t Timestamp(const nlohmann::json &value) {
    if (value.is_number_unsigned()) return value.get<uint64_t>();
    if (value.is_number_integer() && value.get<int64_t>() >= 0) return uint64_t(value.get<int64_t>());
    throw std::invalid_argument("Funding timestamp must be a nonnegative integer");
}
template <size_t Size> inline std::array<unsigned char, Size> Bytes(const std::string &hex) {
    std::array<unsigned char, Size> bytes{};
    size_t written = 0;
    if (sodium_hex2bin(bytes.data(), bytes.size(), hex.data(), hex.size(), nullptr, &written, nullptr) != 0 || written != Size)
        throw std::invalid_argument("Invalid funding bytes");
    return bytes;
}
}

// Canonical lowercase hex, decimal milliseconds, and a final newline. This is
// an intent to request a fixed bootstrap grant, never a signed transaction.
inline std::string FundingRequestMessage(const nlohmann::json &request) {
    if (!request.is_object()) throw std::invalid_argument("Funding request must be an object");
    return "NORN-CHAT-BOOTSTRAP-V1\n" +
        ChatFundingDetail::Hex(request.at("genesis_root"), 32) + "\n" +
        ChatFundingDetail::Hex(request.at("contract"), 20) + "\n" +
        ChatFundingDetail::Hex(request.at("public_key"), 32) + "\n" +
        ChatFundingDetail::Hex(request.at("sponsor"), 32) + "\n" +
        ChatFundingDetail::Hex(request.at("nonce"), 16) + "\n" +
        std::to_string(ChatFundingDetail::Timestamp(request.at("timestamp_ms"))) + "\n";
}

inline bool ValidateFundingRequest(const nlohmann::json &request, const std::string &expectedGenesis,
                                   const std::string &expectedContract, const std::string &expectedSponsor,
                                   uint64_t nowMs) {
    try {
        if (!request.is_object() || request.dump().size() > ChatFundingMaxRequestBytes) return false;
        auto genesis = ChatFundingDetail::Hex(request.at("genesis_root"), 32);
        auto contract = ChatFundingDetail::Hex(request.at("contract"), 20);
        auto key = ChatFundingDetail::Hex(request.at("public_key"), 32);
        auto sponsor = ChatFundingDetail::Hex(request.at("sponsor"), 32);
        if (genesis != ChatFundingDetail::Hex(nlohmann::json(expectedGenesis), 32) ||
            contract != ChatFundingDetail::Hex(nlohmann::json(expectedContract), 20) ||
            sponsor != ChatFundingDetail::Hex(nlohmann::json(expectedSponsor), 32) || key == sponsor)
            return false;
        auto timestamp = ChatFundingDetail::Timestamp(request.at("timestamp_ms"));
        if (timestamp > nowMs ? timestamp - nowMs > ChatFundingFutureSkewMs : nowMs - timestamp > ChatFundingMaxAgeMs)
            return false;
        auto publicKey = ChatFundingDetail::Bytes<crypto_sign_PUBLICKEYBYTES>(key);
        auto signature = ChatFundingDetail::Bytes<crypto_sign_BYTES>(ChatFundingDetail::Hex(request.at("signature"), crypto_sign_BYTES));
        auto message = FundingRequestMessage(request);
        return crypto_sign_verify_detached(signature.data(), reinterpret_cast<const unsigned char *>(message.data()),
                                          message.size(), publicKey.data()) == 0;
    } catch (const std::exception &) { return false; }
}
