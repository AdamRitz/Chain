#pragma once

#include "VDF.h"

#include <sodium.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <initializer_list>
#include <span>
#include <stdexcept>
#include <string_view>
#include <vector>

namespace chain::vdf_election {

inline constexpr std::uint16_t kVersion = 1;
inline constexpr std::size_t kDigestBytes = 32;
inline constexpr std::size_t kMaxProducers = 5;
// Small iteration counts can share the same early output across discriminants.
inline constexpr std::uint64_t kMinIterations = 4096;
inline constexpr std::size_t kContextBytes = 2 + 32 + 8 + 32 + 32 + 8;
inline constexpr std::size_t kUnsignedBytes = 4 + kContextBytes + vdf::kProofBytes;
inline constexpr std::size_t kEncodedBytes = kUnsignedBytes + crypto_sign_BYTES;

using Digest = std::array<std::uint8_t, kDigestBytes>;
using PublicKey = std::array<std::uint8_t, crypto_sign_PUBLICKEYBYTES>;
using SecretKey = std::array<std::uint8_t, crypto_sign_SECRETKEYBYTES>;
using Signature = std::array<std::uint8_t, crypto_sign_BYTES>;
using EncodedCandidate = std::array<std::uint8_t, kEncodedBytes>;

static_assert(crypto_sign_PUBLICKEYBYTES == 32);
static_assert(crypto_sign_SECRETKEYBYTES == 64);
static_assert(crypto_sign_BYTES == 64);
static_assert(kEncodedBytes == 382);

struct Context {
    Digest chainId{};
    std::uint64_t targetEpoch = 0;
    Digest anchor{};
    std::uint64_t iterations = 0;

    bool operator==(const Context&) const = default;
};

struct Candidate {
    Context context{};
    PublicKey publicKey{};
    vdf::Proof proof{};
    Signature signature{};

    bool operator==(const Candidate&) const = default;
};

struct RankedCandidate {
    Candidate candidate{};
    Digest score{};

    bool operator==(const RankedCandidate&) const = default;
};

namespace detail {

inline constexpr std::array<std::uint8_t, 4> kMagic{'V', 'F', 'E', 'L'};

inline bool ValidIterations(std::uint64_t iterations) noexcept {
    return iterations >= kMinIterations && iterations <= vdf::kMaxIterations;
}

inline void Write64(std::span<std::uint8_t, 8> out, std::uint64_t value) noexcept {
    for (std::size_t i = 0; i < out.size(); ++i) {
        out[i] = static_cast<std::uint8_t>(value >> (8 * i));
    }
}

inline std::uint64_t Read64(std::span<const std::uint8_t, 8> in) noexcept {
    std::uint64_t value = 0;
    for (std::size_t i = 0; i < in.size(); ++i) {
        value |= static_cast<std::uint64_t>(in[i]) << (8 * i);
    }
    return value;
}

// Every field has a fixed width. Integers use little-endian encoding.
inline std::array<std::uint8_t, kContextBytes> EncodeContext(
    const Context& context, const PublicKey& publicKey) noexcept {
    std::array<std::uint8_t, kContextBytes> bytes{};
    bytes[0] = static_cast<std::uint8_t>(kVersion);
    bytes[1] = static_cast<std::uint8_t>(kVersion >> 8);
    std::copy(context.chainId.begin(), context.chainId.end(), bytes.begin() + 2);
    Write64(std::span(bytes).subspan<34, 8>(), context.targetEpoch);
    std::copy(context.anchor.begin(), context.anchor.end(), bytes.begin() + 42);
    std::copy(publicKey.begin(), publicKey.end(), bytes.begin() + 74);
    Write64(std::span(bytes).subspan<106, 8>(), context.iterations);
    return bytes;
}

inline std::array<std::uint8_t, kUnsignedBytes> EncodeUnsigned(const Candidate& candidate) noexcept {
    std::array<std::uint8_t, kUnsignedBytes> bytes{};
    const auto context = EncodeContext(candidate.context, candidate.publicKey);
    std::copy(kMagic.begin(), kMagic.end(), bytes.begin());
    std::copy(context.begin(), context.end(), bytes.begin() + kMagic.size());
    std::copy(candidate.proof.begin(), candidate.proof.end(), bytes.begin() + kMagic.size() + context.size());
    return bytes;
}

inline Digest Hash(std::string_view domain,
                   std::initializer_list<std::span<const std::uint8_t>> fields) noexcept {
    Digest digest{};
    crypto_generichash_state state;
    crypto_generichash_init(&state, nullptr, 0, digest.size());
    crypto_generichash_update(&state, reinterpret_cast<const unsigned char*>(domain.data()), domain.size());
    for (const auto field : fields) crypto_generichash_update(&state, field.data(), field.size());
    crypto_generichash_final(&state, digest.data(), digest.size());
    return digest;
}

} // namespace detail

// Each context and public key maps to its own challenge and class-group
// discriminant; the iteration count is part of that challenge.
[[nodiscard]] inline vdf::Challenge DeriveChallenge(
    const Context& context, const PublicKey& publicKey) noexcept {
    const auto encoded = detail::EncodeContext(context, publicKey);
    return detail::Hash("Chain/VdfElection/Challenge/v1", {encoded});
}

// Call only after VerifyCandidate succeeds for received records. Neither the
// proof witness nor the signature provides an extra input to the ranking hash.
[[nodiscard]] inline Digest DeriveScore(
    const vdf::Challenge& challenge, const vdf::Proof& proof) noexcept {
    return detail::Hash("Chain/VdfElection/Score/v1", {challenge, vdf::Output(proof)});
}

[[nodiscard]] inline Digest SigningDigest(const Candidate& candidate) noexcept {
    const auto encoded = detail::EncodeUnsigned(candidate);
    return detail::Hash("Chain/VdfElection/Signature/v1", {encoded});
}

inline void SignCandidate(Candidate& candidate, const SecretKey& secretKey) {
    PublicKey publicKey{};
    crypto_sign_ed25519_sk_to_pk(publicKey.data(), secretKey.data());
    if (candidate.publicKey != publicKey) {
        throw std::invalid_argument("VDF candidate public key does not match signing key");
    }
    const auto digest = SigningDigest(candidate);
    if (crypto_sign_detached(candidate.signature.data(), nullptr, digest.data(), digest.size(), secretKey.data()) != 0) {
        throw std::runtime_error("VDF candidate signing failed");
    }
}

// The application initializes libsodium before using this module.
[[nodiscard]] inline Candidate CreateCandidate(const Context& context, const SecretKey& secretKey) {
    if (!detail::ValidIterations(context.iterations)) {
        throw std::invalid_argument("VDF election iterations outside supported range");
    }
    Candidate candidate;
    candidate.context = context;
    crypto_sign_ed25519_sk_to_pk(candidate.publicKey.data(), secretKey.data());
    candidate.proof = vdf::Prove(DeriveChallenge(context, candidate.publicKey), context.iterations);
    SignCandidate(candidate, secretKey);
    return candidate;
}

// expectedContext comes from the caller's chain state, never from the received
// record. A valid result says nothing about membership or a network deadline.
[[nodiscard]] inline bool VerifyCandidate(const Candidate& candidate, const Context& expectedContext,
                                          Digest* verifiedScore = nullptr) noexcept {
    if (candidate.context != expectedContext || !detail::ValidIterations(expectedContext.iterations)) return false;
    const auto digest = SigningDigest(candidate);
    if (crypto_sign_verify_detached(candidate.signature.data(), digest.data(), digest.size(),
                                    candidate.publicKey.data()) != 0) return false;
    const auto challenge = DeriveChallenge(expectedContext, candidate.publicKey);
    if (!vdf::Verify(challenge, expectedContext.iterations, candidate.proof)) return false;
    if (verifiedScore) *verifiedScore = DeriveScore(challenge, candidate.proof);
    return true;
}

[[nodiscard]] inline EncodedCandidate EncodeCandidate(const Candidate& candidate) noexcept {
    EncodedCandidate bytes{};
    const auto message = detail::EncodeUnsigned(candidate);
    std::copy(message.begin(), message.end(), bytes.begin());
    std::copy(candidate.signature.begin(), candidate.signature.end(), bytes.begin() + kUnsignedBytes);
    return bytes;
}

// Decoding checks the fixed format and parameter bounds. The caller then uses
// VerifyCandidate with its expected context before accepting the record.
[[nodiscard]] inline bool DecodeCandidate(std::span<const std::uint8_t> bytes, Candidate& candidate) noexcept {
    if (bytes.size() != kEncodedBytes || !std::equal(detail::kMagic.begin(), detail::kMagic.end(), bytes.begin()) ||
        bytes[4] != static_cast<std::uint8_t>(kVersion) || bytes[5] != static_cast<std::uint8_t>(kVersion >> 8)) {
        return false;
    }
    Candidate decoded;
    std::copy_n(bytes.begin() + 6, decoded.context.chainId.size(), decoded.context.chainId.begin());
    decoded.context.targetEpoch = detail::Read64(bytes.subspan<38, 8>());
    std::copy_n(bytes.begin() + 46, decoded.context.anchor.size(), decoded.context.anchor.begin());
    std::copy_n(bytes.begin() + 78, decoded.publicKey.size(), decoded.publicKey.begin());
    decoded.context.iterations = detail::Read64(bytes.subspan<110, 8>());
    if (!detail::ValidIterations(decoded.context.iterations)) return false;
    std::copy_n(bytes.begin() + 118, decoded.proof.size(), decoded.proof.begin());
    std::copy_n(bytes.begin() + kUnsignedBytes, decoded.signature.size(), decoded.signature.begin());
    candidate = decoded;
    return true;
}

[[nodiscard]] inline bool RankLess(const RankedCandidate& left, const RankedCandidate& right) noexcept {
    if (left.score != right.score) return left.score < right.score;
    return left.candidate.publicKey < right.candidate.publicKey;
}

// Deterministic selection from a caller-supplied set. At most five records are
// retained; all received records still require signature and VDF verification.
[[nodiscard]] inline std::vector<RankedCandidate> SelectTopCandidates(
    std::span<const Candidate> candidates, const Context& expectedContext, std::size_t limit = kMaxProducers) {
    if (limit > kMaxProducers) throw std::invalid_argument("VDF election supports at most five producers");
    std::vector<RankedCandidate> selected;
    if (limit == 0) return selected;
    selected.reserve(limit + 1);
    for (const auto& candidate : candidates) {
        Digest score{};
        if (!VerifyCandidate(candidate, expectedContext, &score)) continue;
        const RankedCandidate ranked{candidate, score};
        const auto existing = std::find_if(selected.begin(), selected.end(), [&](const RankedCandidate& item) {
            return item.candidate.publicKey == candidate.publicKey;
        });
        if (existing != selected.end()) {
            // The output is unique for a challenge. If two valid encodings of
            // the same record exist, choose deterministically between them.
            if (RankLess(ranked, *existing) ||
                (ranked.score == existing->score && EncodeCandidate(candidate) < EncodeCandidate(existing->candidate))) {
                *existing = ranked;
            }
        } else {
            selected.push_back(ranked);
        }
        std::sort(selected.begin(), selected.end(), RankLess);
        if (selected.size() > limit) selected.pop_back();
    }
    return selected;
}

} // namespace chain::vdf_election
