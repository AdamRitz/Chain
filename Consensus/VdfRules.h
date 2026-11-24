#pragma once

#include "../Crypto/VdfElection.h"

#include <atomic>
#include <chrono>
#include <deque>
#include <functional>
#include <limits>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_set>

namespace chain::vdf_consensus {

using vdf_election::Candidate;
using vdf_election::Context;
using vdf_election::Digest;
using vdf_election::PublicKey;
using vdf_election::SecretKey;
using vdf_election::Signature;

struct Config {
    bool enabled = false;
    std::uint64_t epochBlocks = 32;
    std::uint64_t iterations = 65536;
    std::uint64_t slotMs = 50;
    std::uint64_t startTimeMs = 0;
    std::uint64_t fallbackSlots = 20;
    std::uint64_t futureSlots = 2;

    bool operator==(const Config&) const = default;
};

inline Config config;
inline constexpr std::uint16_t kVersion = 1;
inline constexpr std::size_t kMaxCandidates = 5;
inline constexpr std::size_t kUnsignedFixedBytes = 2 + 8 + 32 + 1 + 1;
inline constexpr std::size_t kMinEncodedBytes = 8 + kUnsignedFixedBytes + 64;
inline constexpr std::size_t kMaxEncodedBytes = kMinEncodedBytes + 6 * vdf_election::kEncodedBytes;

struct Extension {
    std::uint64_t slot = 0;
    PublicKey producer{};
    std::vector<Candidate> candidates;
    std::optional<Candidate> producerProof;
    Signature signature{};

    bool operator==(const Extension&) const = default;
};

inline std::function<std::uint64_t()> currentTimeMs = [] {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
};
inline std::atomic<std::uint64_t> proofChecks{0}, invalidProofs{0}, proofCacheHits{0};

inline void ValidateConfig(const Config& value) {
    if (value.epochBlocks == 0 || value.epochBlocks > 4096 ||
        value.iterations < vdf_election::kMinIterations || value.iterations > vdf::kMaxIterations ||
        value.slotMs == 0 || value.slotMs > 5000 ||
        (value.enabled && (value.startTimeMs == 0 || value.startTimeMs % value.slotMs != 0)) ||
        value.futureSlots > 2 ||
        value.fallbackSlots <= value.futureSlots || value.fallbackSlots > 65536) {
        throw std::invalid_argument("Invalid VDF consensus configuration");
    }
}

namespace detail {

inline void Write64(std::uint8_t* bytes, std::uint64_t value) noexcept {
    for (std::size_t i = 0; i < 8; ++i) bytes[i] = static_cast<std::uint8_t>(value >> (8 * i));
}
inline std::uint64_t Read64(const std::uint8_t* bytes) noexcept {
    std::uint64_t value = 0;
    for (std::size_t i = 0; i < 8; ++i) value |= static_cast<std::uint64_t>(bytes[i]) << (8 * i);
    return value;
}
inline void Write32(std::uint8_t* bytes, std::uint32_t value) noexcept {
    for (std::size_t i = 0; i < 4; ++i) bytes[i] = static_cast<std::uint8_t>(value >> (8 * i));
}
inline std::uint32_t Read32(const std::uint8_t* bytes) noexcept {
    std::uint32_t value = 0;
    for (std::size_t i = 0; i < 4; ++i) value |= static_cast<std::uint32_t>(bytes[i]) << (8 * i);
    return value;
}
inline std::mutex proofMutex;
inline std::unordered_set<std::string> validProofs;
inline std::deque<std::string> proofOrder;
inline constexpr std::size_t kProofCacheLimit = 4096;

} // namespace detail

[[nodiscard]] inline std::array<std::uint8_t, 72> EncodeConfig(const Config& value) {
    ValidateConfig(value);
    std::array<std::uint8_t, 72> bytes{};
    const std::array<std::uint64_t, 9> fields{kVersion, value.enabled ? 1ULL : 0ULL,
        value.epochBlocks, value.iterations, value.slotMs, value.fallbackSlots, value.futureSlots,
        kMaxCandidates, value.startTimeMs};
    for (std::size_t i = 0; i < fields.size(); ++i) detail::Write64(bytes.data() + i * 8, fields[i]);
    return bytes;
}

[[nodiscard]] inline std::uint64_t CurrentSlot(const Config& value = config) {
    if (!value.slotMs) throw std::invalid_argument("VDF slot duration is zero");
    return currentTimeMs() / value.slotMs;
}

[[nodiscard]] inline std::uint64_t StartSlot(const Config& value = config) {
    ValidateConfig(value);
    if (!value.enabled) throw std::invalid_argument("VDF consensus is disabled");
    return value.startTimeMs / value.slotMs;
}

[[nodiscard]] inline std::uint64_t MinimumSlot(std::uint64_t height, const Config& value = config) {
    if (height < 2) throw std::invalid_argument("Invalid VDF block height");
    const auto start = StartSlot(value);
    if (height - 2 > std::numeric_limits<std::uint64_t>::max() - start)
        throw std::invalid_argument("VDF minimum slot overflow");
    return start + (height - 2);
}

[[nodiscard]] inline std::uint64_t Epoch(std::uint64_t height, const Config& value = config) {
    if (height < 2 || !value.epochBlocks) throw std::invalid_argument("Invalid VDF epoch height");
    return (height - 2) / value.epochBlocks;
}

[[nodiscard]] inline std::uint64_t EpochStart(std::uint64_t epoch, const Config& value = config) {
    if (!value.epochBlocks || epoch > (std::numeric_limits<std::uint64_t>::max() - 2) / value.epochBlocks) {
        throw std::invalid_argument("VDF epoch height overflow");
    }
    return 2 + epoch * value.epochBlocks;
}

using ReadHash = std::function<std::optional<Digest>(std::uint64_t)>;

[[nodiscard]] inline Context ContextForEpoch(const Config& value, const Digest& chainId,
    const Digest& genesisHash, std::uint64_t targetEpoch, const ReadHash& readHash) {
    ValidateConfig(value);
    Context context{chainId, targetEpoch, {}, value.iterations};
    if (targetEpoch < 2) {
        context.anchor = vdf_election::detail::Hash("Chain/VdfConsensus/Bootstrap/v1", {chainId, genesisHash});
        return context;
    }
    const auto sourceEpoch = targetEpoch - 2;
    const auto start = EpochStart(sourceEpoch, value);
    if (value.epochBlocks - 1 > std::numeric_limits<std::uint64_t>::max() - start) {
        throw std::invalid_argument("VDF anchor height overflow");
    }
    std::vector<std::uint8_t> bytes(16 + value.epochBlocks * 32);
    detail::Write64(bytes.data(), sourceEpoch);
    detail::Write64(bytes.data() + 8, value.epochBlocks);
    for (std::uint64_t i = 0; i < value.epochBlocks; ++i) {
        const auto hash = readHash(start + i);
        if (!hash) throw std::invalid_argument("Missing VDF anchor block");
        std::copy(hash->begin(), hash->end(), bytes.begin() + 16 + i * 32);
    }
    context.anchor = vdf_election::detail::Hash("Chain/VdfConsensus/Anchor/v1", {chainId, bytes});
    return context;
}

[[nodiscard]] inline std::vector<std::uint8_t> EncodeUnsigned(const Extension& extension) {
    if (extension.candidates.size() > kMaxCandidates) throw std::invalid_argument("Too many VDF block candidates");
    const auto records = extension.candidates.size() + (extension.producerProof ? 1 : 0);
    std::vector<std::uint8_t> bytes(kUnsignedFixedBytes + records * vdf_election::kEncodedBytes);
    bytes[0] = static_cast<std::uint8_t>(kVersion);
    bytes[1] = static_cast<std::uint8_t>(kVersion >> 8);
    detail::Write64(bytes.data() + 2, extension.slot);
    std::copy(extension.producer.begin(), extension.producer.end(), bytes.begin() + 10);
    bytes[42] = static_cast<std::uint8_t>(extension.candidates.size());
    bytes[43] = extension.producerProof ? 1 : 0;
    std::size_t offset = kUnsignedFixedBytes;
    const auto append = [&](const Candidate& record) {
        const auto encoded = vdf_election::EncodeCandidate(record);
        std::copy(encoded.begin(), encoded.end(), bytes.begin() + offset);
        offset += encoded.size();
    };
    for (const auto& record : extension.candidates) append(record);
    if (extension.producerProof) append(*extension.producerProof);
    return bytes;
}

[[nodiscard]] inline std::vector<std::uint8_t> Encode(const Extension& extension) {
    const auto message = EncodeUnsigned(extension);
    std::vector<std::uint8_t> bytes(8 + message.size() + extension.signature.size());
    std::copy_n("VDB1", 4, bytes.begin());
    detail::Write32(bytes.data() + 4, static_cast<std::uint32_t>(bytes.size() - 8));
    std::copy(message.begin(), message.end(), bytes.begin() + 8);
    std::copy(extension.signature.begin(), extension.signature.end(), bytes.end() - extension.signature.size());
    return bytes;
}

// Parse starts at the exact extension offset established by the block parser.
// Looking for VDB1 in a transaction signature or at the end is not a delimiter.
[[nodiscard]] inline bool Parse(std::span<const std::uint8_t> bytes, Extension& extension) noexcept {
    try {
        if (bytes.size() < kMinEncodedBytes || bytes.size() > kMaxEncodedBytes ||
            !std::equal(bytes.begin(), bytes.begin() + 4, "VDB1") || detail::Read32(bytes.data() + 4) != bytes.size() - 8 ||
            bytes[8] != static_cast<std::uint8_t>(kVersion) || bytes[9] != static_cast<std::uint8_t>(kVersion >> 8) ||
            bytes[50] > kMaxCandidates || bytes[51] > 1) return false;
        const auto records = static_cast<std::size_t>(bytes[50]) + bytes[51];
        if (bytes.size() != kMinEncodedBytes + records * vdf_election::kEncodedBytes) return false;
        Extension parsed;
        parsed.slot = detail::Read64(bytes.data() + 10);
        std::copy_n(bytes.begin() + 18, parsed.producer.size(), parsed.producer.begin());
        parsed.candidates.resize(bytes[50]);
        std::size_t offset = 8 + kUnsignedFixedBytes;
        for (auto& record : parsed.candidates) {
            if (!vdf_election::DecodeCandidate(bytes.subspan(offset, vdf_election::kEncodedBytes), record)) return false;
            offset += vdf_election::kEncodedBytes;
        }
        if (bytes[51]) {
            Candidate record;
            if (!vdf_election::DecodeCandidate(bytes.subspan(offset, vdf_election::kEncodedBytes), record)) return false;
            parsed.producerProof = record;
            offset += vdf_election::kEncodedBytes;
        }
        std::copy_n(bytes.begin() + offset, parsed.signature.size(), parsed.signature.begin());
        extension = std::move(parsed);
        return true;
    } catch (...) {
        return false;
    }
}

[[nodiscard]] inline Digest MerkleRoot(const Digest& transactionRoot, const Extension& extension) {
    const auto message = EncodeUnsigned(extension);
    return vdf_election::detail::Hash("Chain/VdfConsensus/BlockRoot/v1", {transactionRoot, message});
}

[[nodiscard]] inline Signature SignHash(const Digest& headerHash, const SecretKey& secretKey) {
    Signature signature{};
    if (crypto_sign_detached(signature.data(), nullptr, headerHash.data(), headerHash.size(), secretKey.data()) != 0) {
        throw std::runtime_error("VDF block signing failed");
    }
    return signature;
}

inline void Sign(Extension& extension, const Digest& headerHash, const SecretKey& secretKey) {
    PublicKey publicKey{};
    crypto_sign_ed25519_sk_to_pk(publicKey.data(), secretKey.data());
    if (extension.producer != publicKey) throw std::invalid_argument("VDF block producer does not match signing key");
    extension.signature = SignHash(headerHash, secretKey);
}

[[nodiscard]] inline bool VerifySignature(const Extension& extension, const Digest& headerHash) noexcept {
    return crypto_sign_verify_detached(extension.signature.data(), headerHash.data(), headerHash.size(),
                                       extension.producer.data()) == 0;
}

inline void ClearProofCache() {
    std::lock_guard lock(detail::proofMutex);
    detail::validProofs.clear();
    detail::proofOrder.clear();
}

// Cache keys contain the complete signed record, including its context. Failed
// proofs are never inserted. This cache does not authorize block producers.
[[nodiscard]] inline bool VerifyProof(const Candidate& record, const Context& expectedContext) {
    if (record.context != expectedContext) { ++invalidProofs; return false; }
    const auto encoded = vdf_election::EncodeCandidate(record);
    const std::string key(reinterpret_cast<const char*>(encoded.data()), encoded.size());
    {
        std::lock_guard lock(detail::proofMutex);
        if (detail::validProofs.contains(key)) { ++proofCacheHits; return true; }
    }
    ++proofChecks;
    if (!vdf_election::VerifyCandidate(record, expectedContext)) { ++invalidProofs; return false; }
    {
        std::lock_guard lock(detail::proofMutex);
        if (detail::validProofs.insert(key).second) {
            detail::proofOrder.push_back(key);
            while (detail::proofOrder.size() > detail::kProofCacheLimit) {
                detail::validProofs.erase(detail::proofOrder.front());
                detail::proofOrder.pop_front();
            }
        }
    }
    return true;
}

[[nodiscard]] inline Digest Score(const Candidate& record) noexcept {
    return vdf_election::DeriveScore(vdf_election::DeriveChallenge(record.context, record.publicKey), record.proof);
}

[[nodiscard]] inline bool CandidateLess(const Candidate& left, const Candidate& right) noexcept {
    const auto leftScore = Score(left), rightScore = Score(right);
    if (leftScore != rightScore) return leftScore < rightScore;
    return left.publicKey < right.publicKey;
}

[[nodiscard]] inline bool VerifyRules(const Extension& extension, std::uint64_t parentSlot,
    std::span<const Candidate> previousCandidates, std::span<const Candidate> activeCandidates,
    const Context& currentContext, const std::optional<Context>& nextContext, std::uint64_t minimumSlot,
    const Config& value = config) noexcept {
    try {
        ValidateConfig(value);
        if (!value.enabled || currentContext.iterations != value.iterations || extension.slot <= parentSlot ||
            minimumSlot < StartSlot(value) || extension.slot < minimumSlot ||
            extension.candidates.size() > kMaxCandidates || previousCandidates.size() > kMaxCandidates ||
            activeCandidates.size() > kMaxCandidates) return false;
        const auto now = CurrentSlot(value);
        if (extension.slot > now && extension.slot - now > value.futureSlots) return false;
        bool member = false;
        for (const auto& record : activeCandidates) {
            if (!VerifyProof(record, currentContext)) return false;
            if (record.publicKey == extension.producer) member = true;
        }
        const bool open = currentContext.targetEpoch < 2 || activeCandidates.empty();
        if (!open && !member && extension.slot - parentSlot < value.fallbackSlots) return false;
        if (open || !member || extension.producerProof) {
            if (!extension.producerProof || extension.producerProof->publicKey != extension.producer ||
                !VerifyProof(*extension.producerProof, currentContext)) return false;
        }
        if (!nextContext) return previousCandidates.empty() && extension.candidates.empty();
        if (currentContext.targetEpoch == std::numeric_limits<std::uint64_t>::max() ||
            nextContext->targetEpoch != currentContext.targetEpoch + 1 ||
            nextContext->chainId != currentContext.chainId || nextContext->iterations != value.iterations) return false;
        std::vector<Candidate> merged;
        merged.reserve(previousCandidates.size() + extension.candidates.size());
        for (const auto& record : previousCandidates) {
            if (!VerifyProof(record, *nextContext)) return false;
            merged.push_back(record);
        }
        for (std::size_t i = 0; i < extension.candidates.size(); ++i) {
            const auto& record = extension.candidates[i];
            if (!VerifyProof(record, *nextContext) || (i && !CandidateLess(extension.candidates[i - 1], record))) return false;
            if (std::any_of(extension.candidates.begin(), extension.candidates.begin() + i,
                           [&](const Candidate& other) { return record.publicKey == other.publicKey; })) return false;
            auto existing = std::find_if(merged.begin(), merged.end(),
                [&](const Candidate& other) { return record.publicKey == other.publicKey; });
            if (existing == merged.end()) merged.push_back(record);
            else if (vdf_election::EncodeCandidate(record) < vdf_election::EncodeCandidate(*existing)) *existing = record;
        }
        std::sort(merged.begin(), merged.end(), CandidateLess);
        if (merged.size() > kMaxCandidates) merged.resize(kMaxCandidates);
        return merged == extension.candidates;
    } catch (...) {
        return false;
    }
}

} // namespace chain::vdf_consensus
