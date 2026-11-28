#pragma once

#include "VdfRules.h"
#include "../Crypto/WorkElection.h"

#include <memory>

namespace chain::work_consensus {

using vdf_consensus::Config;
using vdf_consensus::Digest;
using vdf_consensus::PublicKey;
using vdf_consensus::ProducerTurn;
using work_election::Commit;
using work_election::Reveal;
using work_election::Ticket;
using work_election::AnchorLookup;

inline constexpr std::size_t kPoolSize = 10;
inline work_election::VerificationCache proofCache;

struct Finalization {
    Digest seed{};
    std::uint8_t missingMask = 0;
    std::vector<Digest> values;
    std::vector<PublicKey> nextCommittee;
    bool operator==(const Finalization&) const = default;
};

struct Delta {
    std::vector<Commit> commits;
    std::vector<Reveal> reveals;
    std::vector<Ticket> tickets;
    std::optional<Finalization> finalization;
    bool operator==(const Delta&) const = default;
};

// Snapshots share accepted proofs. A normal transaction block copies at most
// ten pointers, not thirty-two proofs per candidate.
struct State {
    Digest chainId{};
    std::uint64_t epoch = 0;
    std::uint64_t startSlot = 0;
    std::uint64_t lastSlot = 0;
    Digest anchor{};
    std::vector<PublicKey> committee;
    std::vector<Commit> commits;
    std::vector<Reveal> reveals;
    Digest seed{};
    std::uint8_t missingMask = 0;
    bool seedReady = false;
    std::vector<std::shared_ptr<const Ticket>> pool;
    std::vector<PublicKey> nextCommittee;
    bool frozen = false;
    ProducerTurn turn{};
    std::uint64_t blocksProduced = 0;
    std::uint8_t disqualifiedMask = 0;
};

struct Schedule {
    PublicKey producer{};
    ProducerTurn turn{};
    std::uint8_t disqualifiedMask = 0;
};

[[nodiscard]] inline std::uint64_t Offset(const State& state, std::uint64_t slot) {
    if (slot < state.startSlot) throw std::invalid_argument("Work slot precedes its round");
    return slot - state.startSlot;
}

[[nodiscard]] inline work_election::BeaconContext BeaconContextFor(const State& state) {
    if (state.epoch == std::numeric_limits<std::uint64_t>::max())
        throw std::invalid_argument("Work epoch overflow");
    return {state.chainId, state.epoch + 1, state.anchor};
}

[[nodiscard]] inline State Initialize(const Config& config, const Digest& chainId,
    std::uint64_t epoch, std::uint64_t startSlot, const Digest& anchor,
    const std::vector<PublicKey>& committee, std::uint64_t lastSlot = 0) {
    vdf_consensus::ValidateConfig(config);
    if (!config.workElection || committee.empty() || committee.size() > vdf_consensus::kMaxCandidates ||
        startSlot < vdf_consensus::StartSlot(config) || lastSlot >= startSlot)
        throw std::invalid_argument("Invalid Work round initialization");
    for (std::size_t i = 0; i < committee.size(); ++i) {
        if (committee[i] == PublicKey{} || std::find(committee.begin(), committee.begin() + i, committee[i]) != committee.begin() + i)
            throw std::invalid_argument("Invalid Work committee");
    }
    State state;
    state.chainId = chainId;
    state.epoch = epoch;
    state.startSlot = startSlot;
    state.lastSlot = lastSlot;
    state.anchor = anchor;
    state.committee = committee;
    return state;
}

[[nodiscard]] inline State ForNextEpoch(const State& parent, std::uint64_t slot,
    const Digest& anchor, const Config& config) {
    if (!parent.frozen || parent.blocksProduced != config.epochBlocks ||
        parent.epoch == std::numeric_limits<std::uint64_t>::max())
        throw std::invalid_argument("Incomplete previous Work round");
    return Initialize(config, parent.chainId, parent.epoch + 1, slot, anchor,
        parent.nextCommittee, parent.lastSlot);
}

// Reveals become authoritative only through the parent chain. All nodes derive
// the same absent values even when their broadcast receive times differ.
[[nodiscard]] inline State ForSlot(const State& parent, std::uint64_t slot, const Config& config) {
    State state = parent;
    if (state.seedReady || Offset(state, slot) < config.revealEndSlot) return state;
    const auto context = BeaconContextFor(state);
    std::vector<std::uint8_t> bytes;
    bytes.insert(bytes.end(), state.chainId.begin(), state.chainId.end());
    const auto at = bytes.size(); bytes.resize(at + 8);
    vdf_consensus::detail::Write64(bytes.data() + at, context.targetEpoch);
    bytes.insert(bytes.end(), state.anchor.begin(), state.anchor.end());
    state.missingMask = 0;
    bytes.push_back(static_cast<std::uint8_t>(state.committee.size()));
    for (std::size_t i = 0; i < state.committee.size(); ++i) {
        const auto& pk = state.committee[i];
        bytes.insert(bytes.end(), pk.begin(), pk.end());
        const auto found = std::find_if(state.reveals.begin(), state.reveals.end(),
            [&](const Reveal& record) { return record.publicKey == pk; });
        Digest value{};
        if (found != state.reveals.end()) value = found->value;
        else state.missingMask |= static_cast<std::uint8_t>(1u << i);
        bytes.insert(bytes.end(), value.begin(), value.end());
    }
    bytes.push_back(state.missingMask);
    state.seed = vdf_election::detail::Hash("Chain/WorkElection/Beacon/v1", {bytes});
    state.seedReady = true;
    return state;
}

[[nodiscard]] inline work_election::Context TicketContextFor(const State& state, const Config& config) {
    if (!state.seedReady) throw std::invalid_argument("Work random seed is not available");
    work_election::Context context;
    context.chainId = state.chainId;
    context.targetEpoch = state.epoch + 1;
    context.anchor = state.anchor;
    context.randomSeed = state.seed;
    // Round e-1 finished immediately before this round and may still be computing.
    context.matureRounds = state.epoch > 0 ? state.epoch - 1 : 0;
    context.iterations = config.workIterations;
    context.groups = static_cast<std::uint32_t>(config.workGroups);
    context.groupSize = static_cast<std::uint32_t>(config.workGroupSize);
    return context;
}

[[nodiscard]] inline bool TicketLess(const Ticket& left, const Ticket& right) {
    const auto a = work_election::DeriveScore(left.context, left.publicKey);
    const auto b = work_election::DeriveScore(right.context, right.publicKey);
    return a == b ? left.publicKey < right.publicKey : a < b;
}

[[nodiscard]] inline Finalization ExpectedFinalization(const State& parent, std::uint64_t slot,
    const Config& config) {
    const auto state = ForSlot(parent, slot, config);
    if (Offset(state, slot) < config.proofEndSlot || !state.seedReady)
        throw std::invalid_argument("Work election is not complete");
    Finalization record;
    record.seed = state.seed;
    record.missingMask = state.missingMask;
    for (const auto& pk : state.committee) {
        const auto found = std::find_if(state.reveals.begin(), state.reveals.end(),
            [&](const Reveal& reveal) { return reveal.publicKey == pk; });
        record.values.push_back(found == state.reveals.end() ? Digest{} : found->value);
    }
    for (const auto& ticket : state.pool) {
        record.nextCommittee.push_back(ticket->publicKey);
        if (record.nextCommittee.size() == vdf_consensus::kMaxCandidates) break;
    }
    if (record.nextCommittee.empty()) record.nextCommittee = state.committee;
    return record;
}

// Empty slots remove an owner for the rest of this round. The following owner
// completes the unfinished 32-block allocation. If every member timed out,
// recovery reopens only the existing committee after the configured gap.
[[nodiscard]] inline std::optional<Schedule> Preview(const State& state, std::uint64_t slot,
    const Config& config) noexcept {
    try {
        if (!config.workElection || state.committee.empty() || state.committee.size() > 5 ||
            !config.producerTimeoutSlots || !config.consecutiveBlocks || state.turn.blocks > config.consecutiveBlocks ||
            slot < state.startSlot || slot <= state.lastSlot || state.blocksProduced >= config.epochBlocks)
            return std::nullopt;
        auto turn = state.turn;
        auto mask = state.disqualifiedMask;
        const auto count = state.committee.size();
        const auto all = static_cast<std::uint8_t>((1u << count) - 1);
        if (mask & static_cast<std::uint8_t>(~all)) return std::nullopt;
        if (!state.epoch && !state.blocksProduced) {
            // Before the first block, all nodes use the genesis clock. Delayed
            // startup removes no identity; an offline first key cannot stall
            // another fixed bootstrap member forever.
            const auto start = vdf_consensus::StartSlot(config);
            if (slot < start) return std::nullopt;
            const auto owner = ((slot - start) / config.producerTimeoutSlots) % count;
            return Schedule{state.committee[owner], ProducerTurn{owner, 1}, 0};
        }
        auto next = [&] {
            for (std::size_t i = 0; i < count; ++i) {
                if (turn.number == std::numeric_limits<std::uint64_t>::max()) throw std::invalid_argument("Work turn overflow");
                ++turn.number;
                if (!(mask & (1u << (turn.number % count)))) return;
            }
        };
        if (turn.blocks == config.consecutiveBlocks) { turn.blocks = 0; next(); }
        auto skipped = state.lastSlot ? (slot - state.lastSlot - 1) / config.producerTimeoutSlots : 0;
        while (skipped && mask != all) {
            mask |= static_cast<std::uint8_t>(1u << (turn.number % count));
            --skipped;
            if (mask != all) next();
        }
        if (mask == all) {
            if (slot - state.lastSlot < vdf_consensus::RecoverySlotGap(count, config)) return std::nullopt;
            mask = 0;
            const auto recovery = vdf_consensus::RecoverySlotGap(count, config);
            const auto offset = (slot - state.lastSlot - recovery) / config.producerTimeoutSlots;
            if (state.turn.number == std::numeric_limits<std::uint64_t>::max() ||
                offset > std::numeric_limits<std::uint64_t>::max() - state.turn.number - 1) return std::nullopt;
            turn.number = state.turn.number + 1 + offset;
            // Keep the remaining allocation, including when the timed-out owner was first.
            if (state.turn.blocks == config.consecutiveBlocks) turn.blocks = 0;
        }
        ++turn.blocks;
        return Schedule{state.committee[turn.number % count], turn, mask};
    } catch (...) { return std::nullopt; }
}

[[nodiscard]] inline State ValidateDelta(const State& parent, const Delta& delta, std::uint64_t slot,
    const AnchorLookup& lookup, const Config& config) {
    if (!config.workElection || delta.commits.size() > 5 || delta.reveals.size() > 5 || delta.tickets.size() > kPoolSize)
        throw std::invalid_argument("Invalid Work delta size");
    auto state = ForSlot(parent, slot, config);
    const auto offset = Offset(state, slot);
    const auto beacon = BeaconContextFor(state);
    const auto member = [&](const PublicKey& pk) {
        return std::find(state.committee.begin(), state.committee.end(), pk) != state.committee.end();
    };
    for (const auto& commit : delta.commits) {
        if (offset < config.commitStartSlot || offset >= config.commitEndSlot || !member(commit.publicKey) ||
            std::any_of(state.commits.begin(), state.commits.end(), [&](const Commit& old) { return old.publicKey == commit.publicKey; }) ||
            !work_election::VerifyCommit(commit, beacon))
            throw std::invalid_argument("Invalid Work random commitment");
        state.commits.push_back(commit);
    }
    for (const auto& reveal : delta.reveals) {
        if (offset < config.commitEndSlot || offset >= config.revealEndSlot || !member(reveal.publicKey) ||
            std::any_of(state.reveals.begin(), state.reveals.end(), [&](const Reveal& old) { return old.publicKey == reveal.publicKey; }))
            throw std::invalid_argument("Invalid Work random reveal");
        const auto commit = std::find_if(state.commits.begin(), state.commits.end(),
            [&](const Commit& old) { return old.publicKey == reveal.publicKey; });
        if (commit == state.commits.end() || !work_election::VerifyReveal(reveal, beacon, *commit))
            throw std::invalid_argument("Work reveal does not match its commitment");
        state.reveals.push_back(reveal);
    }
    for (const auto& ticket : delta.tickets) {
        if (offset < config.revealEndSlot || offset >= config.proofEndSlot || state.frozen ||
            std::any_of(state.pool.begin(), state.pool.end(), [&](const auto& old) { return old->publicKey == ticket.publicKey; }) ||
            (state.pool.size() == kPoolSize && !TicketLess(ticket, *state.pool.back())))
            throw std::invalid_argument("Invalid or noncompetitive Work ticket");
        const auto context = TicketContextFor(state, config);
        if (!context.matureRounds || !work_election::VerifyTicket(ticket, context, lookup, nullptr, &proofCache))
            throw std::invalid_argument("Invalid Work history proof");
        state.pool.push_back(std::make_shared<const Ticket>(ticket));
        std::sort(state.pool.begin(), state.pool.end(), [](const auto& a, const auto& b) { return TicketLess(*a, *b); });
        if (state.pool.size() > kPoolSize) state.pool.pop_back();
    }
    if (offset >= config.proofEndSlot && !state.frozen) {
        const auto expected = ExpectedFinalization(state, slot, config);
        if (!delta.finalization || *delta.finalization != expected)
            throw std::invalid_argument("Missing or inconsistent Work election result");
        state.nextCommittee = expected.nextCommittee;
        state.frozen = true;
    } else if (delta.finalization) {
        throw std::invalid_argument("Unexpected Work election result");
    }
    return state;
}

[[nodiscard]] inline State Advance(const State& parent, const Delta& delta, std::uint64_t slot,
    const PublicKey& producer, const ProducerTurn& turn, const AnchorLookup& lookup, const Config& config) {
    const auto expected = Preview(parent, slot, config);
    if (!expected || expected->producer != producer || expected->turn != turn)
        throw std::invalid_argument("Invalid Work block producer or turn");
    auto state = ValidateDelta(parent, delta, slot, lookup, config);
    state.turn = turn;
    state.lastSlot = slot;
    state.disqualifiedMask = expected->disqualifiedMask;
    ++state.blocksProduced;
    return state;
}

// Run in block verification workers before taking the database commit lock.
// This checks VDF mathematics against the ticket's declared historical hashes.
// Advance still checks the election context and all hashes against the branch.
[[nodiscard]] inline bool VerifyDeltaProofs(const Delta& delta, const Config& config) noexcept {
    try {
        if (!config.workElection || delta.commits.size() > 5 || delta.reveals.size() > 5 ||
            delta.tickets.size() > kPoolSize) return false;
        for (std::size_t i = 0; i < delta.tickets.size(); ++i) {
            const auto& ticket = delta.tickets[i];
            if (ticket.context.iterations != config.workIterations || ticket.context.groups != config.workGroups ||
                ticket.context.groupSize != config.workGroupSize || ticket.works.empty() ||
                ticket.works.size() > work_election::kMaxWorks ||
                std::any_of(delta.tickets.begin(), delta.tickets.begin() + i,
                    [&](const Ticket& other) { return other.publicKey == ticket.publicKey; })) return false;
            // Valid samples are in ascending order with no overlapping groups.
            // Enforce this before constructing the O(log k) lookup.
            for (std::size_t j = 1; j < ticket.works.size(); ++j)
                if (ticket.works[j - 1].round >= ticket.works[j].round) return false;
            const AnchorLookup lookup = [&](std::uint64_t round, Digest& anchor) {
                const auto found = std::lower_bound(ticket.works.begin(), ticket.works.end(), round,
                    [](const work_election::Work& work, std::uint64_t number) { return work.round < number; });
                if (found == ticket.works.end() || found->round != round) return false;
                anchor = found->anchor;
                return true;
            };
            if (!work_election::VerifyTicket(ticket, ticket.context, lookup, nullptr, &proofCache)) return false;
        }
        return true;
    } catch (...) { return false; }
}

namespace detail {
inline void Append32(std::vector<std::uint8_t>& out, std::uint32_t value) {
    const auto offset = out.size(); out.resize(offset + 4);
    vdf_consensus::detail::Write32(out.data() + offset, value);
}
inline void Append(std::vector<std::uint8_t>& out, std::span<const std::uint8_t> value) {
    out.insert(out.end(), value.begin(), value.end());
}
struct Reader {
    std::span<const std::uint8_t> bytes;
    std::size_t offset = 0;
    std::span<const std::uint8_t> Take(std::size_t n) {
        if (n > bytes.size() - offset) throw std::invalid_argument("Truncated Work delta");
        auto result = bytes.subspan(offset, n); offset += n; return result;
    }
    std::uint8_t Byte() { return Take(1)[0]; }
    std::span<const std::uint8_t> Record() {
        const auto n = vdf_consensus::detail::Read32(Take(4).data()); return Take(n);
    }
    template<class T> void Fixed(T& out) { const auto in = Take(out.size()); std::copy(in.begin(), in.end(), out.begin()); }
};
} // namespace detail

[[nodiscard]] inline std::vector<std::uint8_t> EncodeDelta(const Delta& delta) {
    if (delta.commits.size() > 5 || delta.reveals.size() > 5 || delta.tickets.size() > kPoolSize)
        throw std::invalid_argument("Oversized Work delta");
    std::vector<std::uint8_t> bytes{1, static_cast<std::uint8_t>(delta.commits.size()),
        static_cast<std::uint8_t>(delta.reveals.size()), static_cast<std::uint8_t>(delta.tickets.size()),
        static_cast<std::uint8_t>(delta.finalization ? 1 : 0)};
    const auto add = [&](const std::vector<std::uint8_t>& encoded) {
        detail::Append32(bytes, static_cast<std::uint32_t>(encoded.size())); detail::Append(bytes, encoded);
    };
    for (const auto& record : delta.commits) add(work_election::EncodeCommit(record));
    for (const auto& record : delta.reveals) add(work_election::EncodeReveal(record));
    for (const auto& record : delta.tickets) add(work_election::EncodeTicket(record));
    if (delta.finalization) {
        const auto& record = *delta.finalization;
        if (record.values.empty() || record.values.size() > 5 || record.nextCommittee.empty() || record.nextCommittee.size() > 5 ||
            record.missingMask >= (1u << record.values.size())) throw std::invalid_argument("Invalid Work election result encoding");
        detail::Append(bytes, record.seed);
        bytes.push_back(record.missingMask);
        bytes.push_back(static_cast<std::uint8_t>(record.values.size()));
        for (const auto& value : record.values) detail::Append(bytes, value);
        bytes.push_back(static_cast<std::uint8_t>(record.nextCommittee.size()));
        for (const auto& pk : record.nextCommittee) detail::Append(bytes, pk);
    }
    if (bytes.size() > vdf_consensus::kMaxWorkDeltaBytes) throw std::invalid_argument("Oversized Work delta");
    return bytes;
}

[[nodiscard]] inline bool DecodeDelta(std::span<const std::uint8_t> bytes, Delta& result) noexcept {
    try {
        if (bytes.size() > vdf_consensus::kMaxWorkDeltaBytes) return false;
        detail::Reader reader{bytes};
        if (reader.Byte() != 1) return false;
        const auto commits = reader.Byte(), reveals = reader.Byte(), tickets = reader.Byte(), finalized = reader.Byte();
        if (commits > 5 || reveals > 5 || tickets > kPoolSize || finalized > 1) return false;
        Delta delta;
        delta.commits.resize(commits); delta.reveals.resize(reveals); delta.tickets.resize(tickets);
        for (auto& record : delta.commits) if (!work_election::DecodeCommit(reader.Record(), record)) return false;
        for (auto& record : delta.reveals) if (!work_election::DecodeReveal(reader.Record(), record)) return false;
        for (auto& record : delta.tickets) if (!work_election::DecodeTicket(reader.Record(), record)) return false;
        if (finalized) {
            Finalization record;
            reader.Fixed(record.seed); record.missingMask = reader.Byte();
            const auto count = reader.Byte();
            if (!count || count > 5 || record.missingMask >= (1u << count)) return false;
            record.values.resize(count);
            for (auto& value : record.values) reader.Fixed(value);
            const auto members = reader.Byte();
            if (!members || members > 5) return false;
            record.nextCommittee.resize(members);
            for (auto& pk : record.nextCommittee) reader.Fixed(pk);
            delta.finalization = std::move(record);
        }
        if (reader.offset != bytes.size()) return false;
        result = std::move(delta);
        return true;
    } catch (...) { return false; }
}

} // namespace chain::work_consensus
