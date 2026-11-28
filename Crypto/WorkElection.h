#pragma once

#include "VdfElection.h"
#include "WorkProofVerifier.h"

#include <functional>
#include <future>
#include <limits>
#include <memory>
#include <mutex>
#include <unordered_map>
#include <utility>

namespace chain::work_election {

using vdf_election::Digest;
using vdf_election::PublicKey;
using vdf_election::SecretKey;
using vdf_election::Signature;
using Output = std::array<std::uint8_t, vdf::kOutputBytes>;

inline constexpr std::uint16_t kVersion = 1;
inline constexpr std::uint64_t kDefaultIterations = 65536;
inline constexpr std::size_t kMaxGroups = 32;
inline constexpr std::size_t kMaxGroupSize = 64;
inline constexpr std::size_t kMaxWorks = 256;
inline constexpr std::size_t kWorkBytes = 8 + 32 + vdf::kOutputBytes + vdf::kProofBytes;
inline constexpr std::size_t kEncodedWorkBytes = 6 + kWorkBytes;
inline constexpr std::size_t kBeaconBytes = 6 + 32 + 8 + 32 + 32 + 32 + crypto_sign_BYTES;
inline constexpr std::size_t kTicketHeaderBytes = 6 + 32 + 8 + 32 + 32 + 8 + 8 + 4 + 4 + 32 + 4;
inline constexpr std::size_t kMaxTicketBytes = kTicketHeaderBytes + kMaxWorks * kWorkBytes + crypto_sign_BYTES;

struct BeaconContext {
    Digest chainId{};
    std::uint64_t targetEpoch = 0;
    Digest anchor{};
    bool operator==(const BeaconContext&) const = default;
};

struct Commit {
    BeaconContext context{};
    PublicKey publicKey{};
    Digest commitment{};
    Signature signature{};
    bool operator==(const Commit&) const = default;
};

struct Reveal {
    BeaconContext context{};
    PublicKey publicKey{};
    Digest value{};
    Signature signature{};
    bool operator==(const Reveal&) const = default;
};

struct Context {
    Digest chainId{};
    std::uint64_t targetEpoch = 0;
    Digest anchor{};
    Digest randomSeed{};
    std::uint64_t matureRounds = 0;
    std::uint64_t iterations = kDefaultIterations;
    std::uint32_t groups = 4;
    std::uint32_t groupSize = 8;
    bool operator==(const Context&) const = default;
};

struct Work {
    std::uint64_t round = 0;
    Digest anchor{};
    Output previousOutput{};
    vdf::Proof proof{};
    bool operator==(const Work&) const = default;
};

struct SampleGroup {
    std::uint64_t start = 0;
    std::uint32_t count = 0;
    bool operator==(const SampleGroup&) const = default;
};

struct Ticket {
    Context context{};
    PublicKey publicKey{};
    std::vector<Work> works;
    Signature signature{};
    bool operator==(const Ticket&) const = default;
};

using AnchorLookup = std::function<bool(std::uint64_t, Digest&)>;
using WorkLookup = std::function<bool(std::uint64_t, Work&)>;

namespace detail {

using vdf_election::detail::Hash;
using vdf_election::detail::ValidIterations;

class Writer {
public:
    std::vector<std::uint8_t> bytes;
    explicit Writer(std::size_t reserve = 0) { bytes.reserve(reserve); }
    void U16(std::uint16_t n) { U64(n, 2); }
    void U32(std::uint32_t n) { U64(n, 4); }
    void U64(std::uint64_t n, unsigned count = 8) {
        for (unsigned i = 0; i < count; ++i) bytes.push_back(static_cast<std::uint8_t>(n >> (8 * i)));
    }
    void Bytes(std::span<const std::uint8_t> value) { bytes.insert(bytes.end(), value.begin(), value.end()); }
    void Header(std::array<std::uint8_t, 4> magic) { Bytes(magic); U16(kVersion); }
};

class Reader {
    std::span<const std::uint8_t> bytes_;
    std::size_t at_ = 0;
public:
    explicit Reader(std::span<const std::uint8_t> bytes) : bytes_(bytes) {}
    bool Bytes(std::span<std::uint8_t> out) {
        if (out.size() > bytes_.size() - at_) return false;
        std::copy_n(bytes_.begin() + at_, out.size(), out.begin());
        at_ += out.size();
        return true;
    }
    bool U64(std::uint64_t& value, unsigned count = 8) {
        if (count > bytes_.size() - at_) return false;
        value = 0;
        for (unsigned i = 0; i < count; ++i) value |= std::uint64_t(bytes_[at_++]) << (8 * i);
        return true;
    }
    bool U32(std::uint32_t& value) {
        std::uint64_t n;
        if (!U64(n, 4)) return false;
        value = static_cast<std::uint32_t>(n);
        return true;
    }
    bool Header(std::array<std::uint8_t, 4> magic) {
        std::array<std::uint8_t, 4> got{};
        std::uint64_t version;
        return Bytes(got) && got == magic && U64(version, 2) && version == kVersion;
    }
    bool Done() const noexcept { return at_ == bytes_.size(); }
};

inline bool ValidContext(const Context& context) noexcept {
    return ValidIterations(context.iterations) && context.groups > 0 && context.groups <= kMaxGroups &&
           context.groupSize > 0 && context.groupSize <= kMaxGroupSize &&
           std::uint64_t(context.groups) * context.groupSize <= kMaxWorks;
}

inline void WriteBeacon(Writer& out, const BeaconContext& context) {
    out.Bytes(context.chainId); out.U64(context.targetEpoch); out.Bytes(context.anchor);
}
inline bool ReadBeacon(Reader& in, BeaconContext& context) {
    return in.Bytes(context.chainId) && in.U64(context.targetEpoch) && in.Bytes(context.anchor);
}
inline void WriteContext(Writer& out, const Context& context) {
    out.Bytes(context.chainId); out.U64(context.targetEpoch); out.Bytes(context.anchor);
    out.Bytes(context.randomSeed); out.U64(context.matureRounds); out.U64(context.iterations);
    out.U32(context.groups); out.U32(context.groupSize);
}
inline bool ReadContext(Reader& in, Context& context) {
    return in.Bytes(context.chainId) && in.U64(context.targetEpoch) && in.Bytes(context.anchor) &&
           in.Bytes(context.randomSeed) && in.U64(context.matureRounds) && in.U64(context.iterations) &&
           in.U32(context.groups) && in.U32(context.groupSize) && ValidContext(context);
}
inline void WriteWork(Writer& out, const Work& work) {
    out.U64(work.round); out.Bytes(work.anchor); out.Bytes(work.previousOutput); out.Bytes(work.proof);
}
inline bool ReadWork(Reader& in, Work& work) {
    return in.U64(work.round) && in.Bytes(work.anchor) && in.Bytes(work.previousOutput) && in.Bytes(work.proof);
}
inline std::vector<std::uint8_t> Unsigned(const Commit& record) {
    Writer out(kBeaconBytes); out.Header({'W', 'C', 'M', 'T'}); WriteBeacon(out, record.context);
    out.Bytes(record.publicKey); out.Bytes(record.commitment);
    return std::move(out.bytes);
}
inline std::vector<std::uint8_t> Unsigned(const Reveal& record) {
    Writer out(kBeaconBytes); out.Header({'W', 'R', 'V', 'L'}); WriteBeacon(out, record.context);
    out.Bytes(record.publicKey); out.Bytes(record.value);
    return std::move(out.bytes);
}
inline std::vector<std::uint8_t> Unsigned(const Ticket& record) {
    if (record.works.size() > kMaxWorks) throw std::invalid_argument("Too many sampled Work records");
    Writer out(kTicketHeaderBytes + record.works.size() * kWorkBytes + crypto_sign_BYTES);
    out.Header({'W', 'T', 'K', 'T'}); WriteContext(out, record.context);
    out.Bytes(record.publicKey); out.U32(static_cast<std::uint32_t>(record.works.size()));
    for (const auto& work : record.works) WriteWork(out, work);
    return std::move(out.bytes);
}
template<class Record>
inline void Sign(Record& record, const SecretKey& secretKey, std::string_view domain) {
    PublicKey publicKey{};
    crypto_sign_ed25519_sk_to_pk(publicKey.data(), secretKey.data());
    if (record.publicKey != publicKey) throw std::invalid_argument("Work record signing key mismatch");
    const auto bytes = Unsigned(record);
    const auto digest = Hash(domain, {bytes});
    if (crypto_sign_detached(record.signature.data(), nullptr, digest.data(), digest.size(), secretKey.data()) != 0)
        throw std::runtime_error("Work record signing failed");
}
template<class Record>
inline bool VerifySignature(const Record& record, std::string_view domain) {
    const auto bytes = Unsigned(record);
    const auto digest = Hash(domain, {bytes});
    return crypto_sign_verify_detached(record.signature.data(), digest.data(), digest.size(), record.publicKey.data()) == 0;
}
template<class Record>
inline std::vector<std::uint8_t> Encode(const Record& record) {
    auto bytes = Unsigned(record);
    bytes.insert(bytes.end(), record.signature.begin(), record.signature.end());
    return bytes;
}

inline std::uint64_t SampleBelow(const Digest& score, std::uint64_t bound, std::uint64_t& counter) {
    // Rejection avoids modulo bias. A bound of zero is never passed by SampleGroups.
    const auto threshold = (std::uint64_t{0} - bound) % bound;
    for (unsigned attempt = 0; attempt < 128; ++attempt) {
        std::array<std::uint8_t, 8> encoded{};
        vdf_election::detail::Write64(encoded, counter++);
        const auto digest = Hash("Chain/WorkElection/Sample/v1", {score, encoded});
        const auto value = vdf_election::detail::Read64(std::span(digest).first<8>());
        if (value >= threshold) return value % bound;
    }
    throw std::runtime_error("Work sample rejection limit");
}

} // namespace detail

[[nodiscard]] inline Output WorkOutput(const Work& work) noexcept {
    Output result{};
    const auto output = vdf::Output(work.proof);
    std::copy(output.begin(), output.end(), result.begin());
    return result;
}

[[nodiscard]] inline vdf::Challenge DeriveWorkChallenge(const Digest& chainId, const PublicKey& publicKey,
                                                       const Work& work, std::uint64_t iterations) {
    detail::Writer encoded(32 + 8 + 32 + 32 + vdf::kOutputBytes + 8);
    encoded.Bytes(chainId); encoded.U64(work.round); encoded.Bytes(work.anchor);
    encoded.Bytes(publicKey); encoded.Bytes(work.previousOutput); encoded.U64(iterations);
    return detail::Hash("Chain/WorkElection/Work/v1", {encoded.bytes});
}

[[nodiscard]] inline Work CreateWork(const Digest& chainId, const PublicKey& publicKey, std::uint64_t round,
                                     const Digest& anchor, const Output& previousOutput, std::uint64_t iterations) {
    if (!detail::ValidIterations(iterations)) throw std::invalid_argument("Work iterations outside supported range");
    Work work{round, anchor, previousOutput, {}};
    work.proof = vdf::Prove(DeriveWorkChallenge(chainId, publicKey, work, iterations), iterations);
    return work;
}

[[nodiscard]] inline bool VerifyWork(const Work& work, const Digest& chainId, const PublicKey& publicKey,
                                     const Digest& expectedAnchor, std::uint64_t iterations) noexcept {
    if (work.anchor != expectedAnchor || !detail::ValidIterations(iterations)) return false;
    try { return vdf::Verify(DeriveWorkChallenge(chainId, publicKey, work, iterations), iterations, work.proof); }
    catch (...) { return false; }
}

// Local persisted records still need their anchor compared with the canonical
// chain by the caller; this overload verifies the stored computation itself.
[[nodiscard]] inline bool VerifyWork(const Digest& chainId, const PublicKey& publicKey,
                                     const Work& work, std::uint64_t iterations) noexcept {
    return VerifyWork(work, chainId, publicKey, work.anchor, iterations);
}

[[nodiscard]] inline Digest Commitment(const BeaconContext& context, const PublicKey& publicKey, const Digest& value) {
    detail::Writer out;
    detail::WriteBeacon(out, context); out.Bytes(publicKey); out.Bytes(value);
    return detail::Hash("Chain/WorkElection/Commitment/v1", {out.bytes});
}

inline void SignCommit(Commit& record, const SecretKey& key) { detail::Sign(record, key, "Chain/WorkElection/CommitSignature/v1"); }
inline void SignReveal(Reveal& record, const SecretKey& key) { detail::Sign(record, key, "Chain/WorkElection/RevealSignature/v1"); }
inline void SignTicket(Ticket& record, const SecretKey& key) { detail::Sign(record, key, "Chain/WorkElection/TicketSignature/v1"); }

[[nodiscard]] inline bool VerifyTicketSignature(const Ticket& record) noexcept {
    try { return detail::VerifySignature(record, "Chain/WorkElection/TicketSignature/v1"); }
    catch (...) { return false; }
}

[[nodiscard]] inline Commit CreateCommit(const BeaconContext& context, const SecretKey& key, const Digest& value) {
    Commit record; record.context = context;
    crypto_sign_ed25519_sk_to_pk(record.publicKey.data(), key.data());
    record.commitment = Commitment(context, record.publicKey, value);
    SignCommit(record, key); return record;
}
[[nodiscard]] inline Reveal CreateReveal(const BeaconContext& context, const SecretKey& key, const Digest& value) {
    Reveal record; record.context = context; record.value = value;
    crypto_sign_ed25519_sk_to_pk(record.publicKey.data(), key.data());
    SignReveal(record, key); return record;
}
[[nodiscard]] inline bool VerifyCommit(const Commit& record, const BeaconContext& expected) noexcept {
    if (record.context != expected) return false;
    try { return detail::VerifySignature(record, "Chain/WorkElection/CommitSignature/v1"); }
    catch (...) { return false; }
}
[[nodiscard]] inline bool VerifyReveal(const Reveal& record, const BeaconContext& expected, const Commit& commit) noexcept {
    if (record.context != expected || record.publicKey != commit.publicKey || !VerifyCommit(commit, expected)) return false;
    try {
        return Commitment(expected, record.publicKey, record.value) == commit.commitment &&
               detail::VerifySignature(record, "Chain/WorkElection/RevealSignature/v1");
    } catch (...) { return false; }
}

[[nodiscard]] inline Digest DeriveScore(const Context& context, const PublicKey& publicKey) {
    detail::Writer out(32 + 8 + 32 + 32);
    out.Bytes(context.chainId); out.U64(context.targetEpoch); out.Bytes(context.randomSeed); out.Bytes(publicKey);
    return detail::Hash("Chain/WorkElection/Ticket/v1", {out.bytes});
}

// Choose uniformly among all placements of nonoverlapping, equal-length groups.
// Removing (groupSize - 1) positions per group maps placements to an ordinary
// subset. Floyd sampling uses at most groups draws and never scans the history.
[[nodiscard]] inline std::vector<SampleGroup> SampleGroups(const Context& context, const PublicKey& publicKey) {
    if (!detail::ValidContext(context)) throw std::invalid_argument("Invalid Work election parameters");
    std::vector<SampleGroup> result;
    if (context.matureRounds == 0) return result;
    const std::uint64_t required = std::uint64_t(context.groups) * context.groupSize;
    if (context.matureRounds < required) {
        for (std::uint64_t start = 0; start < context.matureRounds;) {
            const auto count = static_cast<std::uint32_t>(std::min<std::uint64_t>(context.groupSize, context.matureRounds - start));
            result.push_back({start, count}); start += count;
        }
        return result;
    }
    const auto score = DeriveScore(context, publicKey);
    const std::uint64_t available = context.matureRounds - std::uint64_t(context.groups) * (context.groupSize - 1);
    std::vector<std::uint64_t> chosen;
    chosen.reserve(context.groups);
    std::uint64_t counter = 0;
    // available <= UINT64_MAX; j never reaches UINT64_MAX, so j + 1 is defined.
    for (std::uint64_t j = available - context.groups; j < available; ++j) {
        const auto drawn = detail::SampleBelow(score, j + 1, counter);
        const auto selected = std::find(chosen.begin(), chosen.end(), drawn) == chosen.end() ? drawn : j;
        chosen.push_back(selected);
    }
    std::sort(chosen.begin(), chosen.end());
    for (std::size_t i = 0; i < chosen.size(); ++i)
        result.push_back({chosen[i] + i * std::uint64_t(context.groupSize - 1), context.groupSize});
    return result;
}

[[nodiscard]] inline Ticket CreateTicket(const Context& context, const SecretKey& secretKey, const WorkLookup& lookup) {
    if (!detail::ValidContext(context) || context.matureRounds == 0 || !lookup)
        throw std::invalid_argument("A Work ticket requires completed historical rounds");
    Ticket ticket; ticket.context = context;
    crypto_sign_ed25519_sk_to_pk(ticket.publicKey.data(), secretKey.data());
    const auto groups = SampleGroups(context, ticket.publicKey);
    for (const auto& group : groups) {
        for (std::uint32_t offset = 0; offset < group.count; ++offset) {
            Work work;
            if (!lookup(group.start + offset, work) || work.round != group.start + offset)
                throw std::invalid_argument("Sampled Work is unavailable");
            if (offset > 0 && work.previousOutput != WorkOutput(ticket.works.back()))
                throw std::invalid_argument("Sampled Work group is not continuous");
            ticket.works.push_back(work);
        }
    }
    SignTicket(ticket, secretKey);
    return ticket;
}

// Only successful full proof verification is retained. In-flight duplicates
// share one result; callers still check branch context and every historical
// anchor before querying the cache. No chain/database lock is held by this class.
class VerificationCache {
    struct Hash {
        std::size_t operator()(const Digest& value) const noexcept {
            std::size_t result = 0;
            for (std::size_t i = 0; i < sizeof(result); ++i) result = (result << 8) | value[i];
            return result;
        }
    };
    struct Entry {
        std::promise<bool> promise;
        std::shared_future<bool> result = promise.get_future().share();
        bool done = false;
    };
    std::mutex mutex_;
    std::unordered_map<Digest, std::shared_ptr<Entry>, Hash> entries_;
    std::size_t capacity_;
public:
    explicit VerificationCache(std::size_t capacity = 256) : capacity_(std::min<std::size_t>(capacity, 4096)) {}
    VerificationCache(const VerificationCache&) = delete;
    VerificationCache& operator=(const VerificationCache&) = delete;

    template<class Verify>
    bool Check(const Digest& key, Verify&& verify) {
        std::shared_ptr<Entry> entry;
        bool owner = false;
        {
            std::lock_guard lock(mutex_);
            const auto found = entries_.find(key);
            if (found != entries_.end()) entry = found->second;
            else if (capacity_ != 0) {
                if (entries_.size() >= capacity_) {
                    const auto completed = std::find_if(entries_.begin(), entries_.end(), [](const auto& item) { return item.second->done; });
                    if (completed != entries_.end()) entries_.erase(completed);
                }
                if (entries_.size() < capacity_) {
                    entry = std::make_shared<Entry>(); entries_.emplace(key, entry); owner = true;
                }
            }
        }
        if (!entry) return verify();
        if (!owner) return entry->result.get();
        bool valid = false;
        try { valid = verify(); } catch (...) { valid = false; }
        entry->promise.set_value(valid);
        {
            std::lock_guard lock(mutex_);
            entry->done = true;
            if (!valid) {
                const auto found = entries_.find(key);
                if (found != entries_.end() && found->second == entry) entries_.erase(found);
            }
        }
        return valid;
    }

    std::size_t Size() { std::lock_guard lock(mutex_); return entries_.size(); }
    // In-flight callers own their Entry and finish normally after eviction.
    // Clearing never waits for a VDF worker while a caller holds a chain lock.
    void Clear() { std::lock_guard lock(mutex_); entries_.clear(); }
};

[[nodiscard]] inline bool VerifyTicket(const Ticket& ticket, const Context& expected,
                                      const AnchorLookup& lookup, Digest* verifiedScore = nullptr,
                                      VerificationCache* cache = nullptr,
                                      ProofVerifier* verifier = nullptr,
                                      ProofPriority priority = ProofPriority::Normal) noexcept {
    if (ticket.context != expected || !detail::ValidContext(expected) || expected.matureRounds == 0 ||
        ticket.works.empty() || ticket.works.size() > kMaxWorks || !lookup) return false;
    try {
        const auto groups = SampleGroups(expected, ticket.publicKey);
        std::size_t count = 0;
        for (const auto& group : groups) count += group.count;
        if (ticket.works.size() != count) return false;
        std::size_t at = 0;
        for (const auto& group : groups) {
            for (std::uint32_t offset = 0; offset < group.count; ++offset, ++at) {
                const auto& work = ticket.works[at];
                if (work.round != group.start + offset) return false;
                Digest anchor{};
                if (!lookup(work.round, anchor) || anchor != work.anchor) return false;
                if (offset > 0 && work.previousOutput != WorkOutput(ticket.works[at - 1])) return false;
            }
        }
        const auto verify = [&] {
            if (!VerifyTicketSignature(ticket)) return false;
            std::vector<std::size_t> starts;
            starts.reserve(groups.size());
            std::size_t start = 0;
            for (const auto& group : groups) { starts.push_back(start); start += group.count; }
            const auto verifyGroup = [&](std::size_t index) {
                for (std::size_t i = starts[index]; i < starts[index] + groups[index].count; ++i) {
                    const auto& work = ticket.works[i];
                    if (!vdf::Verify(DeriveWorkChallenge(expected.chainId, ticket.publicKey, work, expected.iterations),
                                     expected.iterations, work.proof)) return false;
                }
                return true;
            };
            if (verifier) return verifier->VerifyGroups(groups.size(), verifyGroup, priority);
            for (std::size_t i = 0; i < groups.size(); ++i) if (!verifyGroup(i)) return false;
            return true;
        };
        bool valid;
        if (cache) {
            const auto encoded = detail::Encode(ticket);
            const auto key = detail::Hash("Chain/WorkElection/VerifiedTicket/v1", {encoded});
            valid = cache->Check(key, verify);
        } else valid = verify();
        if (!valid) return false;
        if (verifiedScore) *verifiedScore = DeriveScore(expected, ticket.publicKey);
        return true;
    } catch (...) { return false; }
}

[[nodiscard]] inline std::vector<std::uint8_t> EncodeWork(const Work& work) {
    detail::Writer out(kEncodedWorkBytes); out.Header({'W', 'O', 'R', 'K'}); detail::WriteWork(out, work); return std::move(out.bytes);
}
[[nodiscard]] inline bool DecodeWork(std::span<const std::uint8_t> bytes, Work& work) noexcept {
    if (bytes.size() != kEncodedWorkBytes) return false;
    detail::Reader in(bytes); Work decoded;
    if (!in.Header({'W', 'O', 'R', 'K'}) || !detail::ReadWork(in, decoded) || !in.Done()) return false;
    work = decoded; return true;
}
[[nodiscard]] inline std::vector<std::uint8_t> EncodeCommit(const Commit& record) { return detail::Encode(record); }
[[nodiscard]] inline std::vector<std::uint8_t> EncodeReveal(const Reveal& record) { return detail::Encode(record); }
[[nodiscard]] inline std::vector<std::uint8_t> EncodeTicket(const Ticket& record) { return detail::Encode(record); }

[[nodiscard]] inline bool DecodeCommit(std::span<const std::uint8_t> bytes, Commit& record) noexcept {
    if (bytes.size() != kBeaconBytes) return false;
    detail::Reader in(bytes); Commit decoded;
    if (!in.Header({'W', 'C', 'M', 'T'}) || !detail::ReadBeacon(in, decoded.context) || !in.Bytes(decoded.publicKey) ||
        !in.Bytes(decoded.commitment) || !in.Bytes(decoded.signature) || !in.Done()) return false;
    record = decoded; return true;
}
[[nodiscard]] inline bool DecodeReveal(std::span<const std::uint8_t> bytes, Reveal& record) noexcept {
    if (bytes.size() != kBeaconBytes) return false;
    detail::Reader in(bytes); Reveal decoded;
    if (!in.Header({'W', 'R', 'V', 'L'}) || !detail::ReadBeacon(in, decoded.context) || !in.Bytes(decoded.publicKey) ||
        !in.Bytes(decoded.value) || !in.Bytes(decoded.signature) || !in.Done()) return false;
    record = decoded; return true;
}
[[nodiscard]] inline bool DecodeTicket(std::span<const std::uint8_t> bytes, Ticket& record) noexcept {
    if (bytes.size() < kTicketHeaderBytes + crypto_sign_BYTES || bytes.size() > kMaxTicketBytes) return false;
    try {
        detail::Reader in(bytes); Ticket decoded; std::uint32_t count;
        if (!in.Header({'W', 'T', 'K', 'T'}) || !detail::ReadContext(in, decoded.context) || !in.Bytes(decoded.publicKey) ||
            !in.U32(count) || count == 0 || count > kMaxWorks || decoded.context.matureRounds == 0 ||
            bytes.size() != kTicketHeaderBytes + std::size_t(count) * kWorkBytes + crypto_sign_BYTES) return false;
        decoded.works.resize(count);
        for (auto& work : decoded.works) if (!detail::ReadWork(in, work)) return false;
        if (!in.Bytes(decoded.signature) || !in.Done()) return false;
        record = std::move(decoded); return true;
    } catch (...) { return false; }
}

} // namespace chain::work_election
