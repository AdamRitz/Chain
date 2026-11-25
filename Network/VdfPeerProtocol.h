#pragma once

#include <sodium.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <string_view>
#include <utility>
#include <vector>

namespace chain::vdf_peer {

inline constexpr std::uint8_t kChallengeType = 56;
inline constexpr std::uint8_t kIdentityType = 57;
inline constexpr std::uint8_t kHintsType = 58;
inline constexpr std::size_t kChallengeBytes = 32;
inline constexpr std::size_t kIdentityBytes = 32 + 64;
inline constexpr std::size_t kHintBytes = 32 + 1 + 16 + 2;
inline constexpr std::size_t kMaxHints = 10;

using Digest = std::array<std::uint8_t, 32>;
using PublicKey = std::array<std::uint8_t, crypto_sign_PUBLICKEYBYTES>;
using SecretKey = std::array<std::uint8_t, crypto_sign_SECRETKEYBYTES>;
using Signature = std::array<std::uint8_t, crypto_sign_BYTES>;
using Challenge = std::array<std::uint8_t, kChallengeBytes>;
using EncodedIdentity = std::array<std::uint8_t, kIdentityBytes>;

static_assert(crypto_sign_PUBLICKEYBYTES == 32 && crypto_sign_BYTES == 64);

struct Identity {
    PublicKey publicKey{};
    Signature signature{};
    bool operator==(const Identity&) const = default;
};

struct EndpointHint {
    PublicKey publicKey{};
    std::array<std::uint8_t, 16> address{};
    std::uint16_t port = 0;
    std::uint8_t family = 0;
    bool operator==(const EndpointHint&) const = default;
};

namespace detail {

inline Digest SigningDigest(const Digest& chainId, const Challenge& challenge,
                            const PublicKey& publicKey, std::uint16_t listenPort) noexcept {
    constexpr std::string_view domain = "Chain/VdfPeer/Identity/v1";
    const std::array<std::uint8_t, 2> port{
        static_cast<std::uint8_t>(listenPort), static_cast<std::uint8_t>(listenPort >> 8)};
    Digest digest{};
    crypto_generichash_state state;
    crypto_generichash_init(&state, nullptr, 0, digest.size());
    crypto_generichash_update(&state, reinterpret_cast<const unsigned char*>(domain.data()), domain.size());
    crypto_generichash_update(&state, chainId.data(), chainId.size());
    crypto_generichash_update(&state, publicKey.data(), publicKey.size());
    crypto_generichash_update(&state, port.data(), port.size());
    crypto_generichash_update(&state, challenge.data(), challenge.size());
    crypto_generichash_final(&state, digest.data(), digest.size());
    return digest;
}

inline bool ValidHint(const EndpointHint& hint) noexcept {
    if (!hint.port || std::all_of(hint.publicKey.begin(), hint.publicKey.end(), [](auto byte) { return byte == 0; }))
        return false;
    if (hint.family == 4) {
        // IPv4 occupies the first four bytes; fixed padding has a single encoding.
        if (!std::all_of(hint.address.begin() + 4, hint.address.end(), [](auto byte) { return byte == 0; }))
            return false;
        if (!hint.address[0] || hint.address[0] >= 224) return false;
        return true;
    }
    if (hint.family != 6 || hint.address[0] == 0xff) return false;
    return !std::all_of(hint.address.begin(), hint.address.end(), [](auto byte) { return byte == 0; });
}

} // namespace detail

// The caller initializes libsodium once before network startup. Challenges are
// generated per connection and retained only until that connection authenticates.
[[nodiscard]] inline Challenge MakeChallenge() {
    Challenge challenge{};
    randombytes_buf(challenge.data(), challenge.size());
    return challenge;
}

[[nodiscard]] inline Identity CreateIdentity(const Digest& chainId, const Challenge& challenge,
                                            std::uint16_t listenPort, const SecretKey& secretKey) {
    if (!listenPort) throw std::invalid_argument("VDF peer listen port must be nonzero");
    Identity identity;
    crypto_sign_ed25519_sk_to_pk(identity.publicKey.data(), secretKey.data());
    const auto digest = detail::SigningDigest(chainId, challenge, identity.publicKey, listenPort);
    if (crypto_sign_detached(identity.signature.data(), nullptr, digest.data(), digest.size(), secretKey.data()) != 0)
        throw std::runtime_error("VDF peer identity signing failed");
    return identity;
}

[[nodiscard]] inline bool VerifyIdentity(const Identity& identity, const Digest& chainId,
                                        const Challenge& challenge, std::uint16_t expectedListenPort) noexcept {
    if (!expectedListenPort) return false;
    const auto digest = detail::SigningDigest(chainId, challenge, identity.publicKey, expectedListenPort);
    return crypto_sign_verify_detached(identity.signature.data(), digest.data(), digest.size(),
                                      identity.publicKey.data()) == 0;
}

[[nodiscard]] inline EncodedIdentity EncodeIdentity(const Identity& identity) noexcept {
    EncodedIdentity encoded{};
    std::copy(identity.publicKey.begin(), identity.publicKey.end(), encoded.begin());
    std::copy(identity.signature.begin(), identity.signature.end(), encoded.begin() + 32);
    return encoded;
}

[[nodiscard]] inline bool DecodeIdentity(std::span<const std::uint8_t> bytes, Identity& identity) noexcept {
    if (bytes.size() != kIdentityBytes) return false;
    Identity decoded;
    std::copy_n(bytes.begin(), decoded.publicKey.size(), decoded.publicKey.begin());
    std::copy_n(bytes.begin() + 32, decoded.signature.size(), decoded.signature.begin());
    identity = decoded;
    return true;
}

// Hints are address discovery records. Their public keys become connection
// identities only after VerifyIdentity succeeds for a fresh connection challenge.
[[nodiscard]] inline std::vector<std::uint8_t> EncodeHints(std::span<const EndpointHint> hints) {
    if (hints.empty() || hints.size() > kMaxHints) throw std::invalid_argument("VDF peer hint count outside supported range");
    std::vector<std::uint8_t> encoded(hints.size() * kHintBytes);
    for (std::size_t index = 0; index < hints.size(); ++index) {
        const auto& hint = hints[index];
        if (!detail::ValidHint(hint)) throw std::invalid_argument("Invalid VDF peer endpoint hint");
        auto start = encoded.begin() + index * kHintBytes;
        std::copy(hint.publicKey.begin(), hint.publicKey.end(), start);
        start[32] = hint.family;
        std::copy(hint.address.begin(), hint.address.end(), start + 33);
        start[49] = static_cast<std::uint8_t>(hint.port);
        start[50] = static_cast<std::uint8_t>(hint.port >> 8);
    }
    return encoded;
}

[[nodiscard]] inline bool DecodeHints(std::span<const std::uint8_t> bytes, std::vector<EndpointHint>& hints) {
    hints.clear();
    if (bytes.empty() || bytes.size() % kHintBytes || bytes.size() / kHintBytes > kMaxHints) return false;
    std::vector<EndpointHint> decoded;
    decoded.reserve(bytes.size() / kHintBytes);
    for (std::size_t offset = 0; offset < bytes.size(); offset += kHintBytes) {
        EndpointHint hint;
        std::copy_n(bytes.begin() + offset, hint.publicKey.size(), hint.publicKey.begin());
        hint.family = bytes[offset + 32];
        std::copy_n(bytes.begin() + offset + 33, hint.address.size(), hint.address.begin());
        hint.port = static_cast<std::uint16_t>(bytes[offset + 49]) |
                    (static_cast<std::uint16_t>(bytes[offset + 50]) << 8);
        if (!detail::ValidHint(hint)) return false;
        decoded.push_back(hint);
    }
    hints = std::move(decoded);
    return true;
}

} // namespace chain::vdf_peer
