#include <filesystem>
#include <iostream>
#include "../Network/Server.h"

namespace {

namespace legacy = chain::vdf_consensus;
namespace rules = chain::work_consensus;
namespace election = chain::work_election;

void Require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}

Wallet Seeded(std::uint8_t value) {
    std::array<std::uint8_t, crypto_sign_SEEDBYTES> seed{}; seed.fill(value);
    Wallet wallet;
    Require(crypto_sign_seed_keypair(wallet.public_key.data(), wallet.private_key.data(), seed.data()) == 0,
        "Test identity generation failed");
    return wallet;
}

void TestOwnTicketVerificationTiming() {
    struct Restore {
        legacy::Config config = legacy::config;
        election::PublicKey publicKey = vdfPublicKey;
        bool ready = vdfIdentityReady;
        ~Restore() {
            legacy::config = config;
            vdfPublicKey = publicKey;
            vdfIdentityReady = ready;
        }
    } restore;
    legacy::config.epochBlocks = 160;
    legacy::config.commitStartSlot = 80;
    legacy::config.commitEndSlot = 90;
    legacy::config.revealEndSlot = 100;
    legacy::config.ticketEndSlot = 120;
    legacy::config.proofEndSlot = 140;

    rules::State state;
    state.chainId[0] = 17;
    state.anchor[0] = 23;
    state.epoch = 34;
    state.startSlot = 1000;
    state.seed[0] = 41;
    state.seedReady = true;
    const auto context = rules::TicketContextFor(state, legacy::config);
    // Fixed inputs select score ranges deterministically; no clock or random
    // generator is used, and these checks do not run VDF calculations.
    auto ticketInRange = [&](unsigned lower, unsigned upper) {
        election::Ticket ticket;
        ticket.context = context;
        for (unsigned key = 1; key <= 4096; ++key) {
            ticket.publicKey[0] = static_cast<std::uint8_t>(key);
            ticket.publicKey[1] = static_cast<std::uint8_t>(key >> 8);
            const auto score = election::DeriveScore(context, ticket.publicKey);
            const std::uint64_t prefix = (std::uint64_t(score[0]) << 24) |
                (std::uint64_t(score[1]) << 16) | (std::uint64_t(score[2]) << 8) | score[3];
            if (prefix * 100 >= (std::uint64_t{1} << 32) * lower &&
                prefix * 100 < (std::uint64_t{1} << 32) * upper) return ticket;
        }
        throw std::runtime_error("Fixed ticket fixtures do not cover the requested score range");
    };
    const auto high = ticketInRange(15, 100);
    const auto middle = ticketInRange(5, 15);
    const auto low = ticketInRange(0, 5);
    vdfPublicKey = high.publicKey;
    vdfIdentityReady = true;
    constexpr std::uint64_t remote = 17;
    auto slot = [&](std::uint64_t phase) { return state.startSlot + phase; };

    for (const auto phase : {100u, 110u, 119u}) {
        Require(WorkTicketVerificationAllowed(high, state, slot(phase), 0),
            "Own ticket did not start verification when the seed became available");
        Require(!WorkBroadcastScore(high, state, slot(phase)),
            "Own ticket was allowed to broadcast before its score threshold");
        Require(!WorkTicketVerificationAllowed(high, state, slot(phase), remote),
            "Remote copy of our ticket bypassed its score threshold");
    }
    vdfPublicKey[31] ^= 1;
    Require(!WorkTicketVerificationAllowed(high, state, slot(100), 0),
        "Local source with another public key bypassed its score threshold");
    vdfPublicKey = high.publicKey;
    vdfIdentityReady = false;
    Require(!WorkTicketVerificationAllowed(high, state, slot(100), 0),
        "Uninitialized local identity bypassed the score threshold");
    vdfIdentityReady = true;

    Require(WorkTicketVerificationAllowed(low, state, slot(100), remote),
        "Remote ticket below five percent was delayed");
    Require(!WorkTicketVerificationAllowed(middle, state, slot(100), remote) &&
            WorkTicketVerificationAllowed(middle, state, slot(110), remote),
        "Remote fifteen-percent threshold changed");
    for (const auto phase : {120u, 139u}) {
        Require(WorkTicketVerificationAllowed(high, state, slot(phase), 0) &&
                WorkTicketVerificationAllowed(high, state, slot(phase), remote) &&
                WorkBroadcastScore(high, state, slot(phase)),
            "Ticket was delayed after all scores were allowed");
    }
    for (const auto phase : {99u, 140u}) {
        Require(!WorkTicketCurrent(high, state, slot(phase)) &&
                !WorkTicketVerificationAllowed(high, state, slot(phase), 0) &&
                !WorkTicketVerificationAllowed(high, state, slot(phase), remote) &&
                !WorkBroadcastScore(high, state, slot(phase)),
            "Ticket crossed the seed or proof deadline");
    }
    auto refused = [&](const rules::State& current, const election::Ticket& ticket) {
        return !WorkTicketCurrent(ticket, current, slot(120)) &&
            !WorkTicketVerificationAllowed(ticket, current, slot(120), 0) &&
            !WorkTicketVerificationAllowed(ticket, current, slot(120), remote) &&
            !WorkBroadcastScore(ticket, current, slot(120));
    };
    auto changed = state;
    changed.frozen = true;
    Require(refused(changed, high), "Frozen election admitted a ticket");
    changed = state; changed.seedReady = false;
    Require(refused(changed, high), "Election without a seed admitted a ticket");
    auto stale = high;
    ++stale.context.targetEpoch;
    Require(refused(state, stale), "Another election context admitted a ticket");
    stale = high; stale.context.anchor[0] ^= 1;
    Require(refused(state, stale), "Another branch anchor admitted a ticket");
    stale = high; stale.context.randomSeed[0] ^= 1;
    Require(refused(state, stale), "Another election seed admitted a ticket");
}

bool SameState(const rules::State& a, const rules::State& b) {
    if (a.chainId != b.chainId || a.epoch != b.epoch || a.startSlot != b.startSlot || a.lastSlot != b.lastSlot ||
        a.anchor != b.anchor || a.committee != b.committee || a.commits != b.commits || a.reveals != b.reveals ||
        a.seed != b.seed || a.missingMask != b.missingMask || a.seedReady != b.seedReady || a.nextCommittee != b.nextCommittee ||
        a.frozen != b.frozen || a.turn != b.turn || a.blocksProduced != b.blocksProduced ||
        a.disqualifiedMask != b.disqualifiedMask || a.pool.size() != b.pool.size()) return false;
    for (std::size_t i = 0; i < a.pool.size(); ++i) if (*a.pool[i] != *b.pool[i]) return false;
    return true;
}

rules::State CurrentState() {
    std::shared_lock lock(dbCommitMutex);
    const auto state = DBWorkState();
    Require(bool(state), "Current Work state is missing");
    return *state;
}

struct Branch {
    std::map<std::uint64_t, election::Digest> hashes;
    std::vector<ForkEntry> blocks;
    rules::State state;
    const std::vector<Wallet>* wallets;
    std::uint8_t variant;
    std::size_t elected;

    Branch(const election::Digest& genesis, const std::vector<Wallet>& identities,
           std::uint8_t branchVariant, std::size_t electedIndex)
        : wallets(&identities), variant(branchVariant), elected(electedIndex) {
        hashes.emplace(1, genesis);
        state = rules::Initialize(legacy::config, genesisRoot, 0, legacy::StartSlot(), genesis,
                                 legacy::config.bootstrapProducers, legacy::StartSlot() - 1);
    }

    const Wallet& WalletFor(const election::PublicKey& key) const {
        const auto found = std::find_if(wallets->begin(), wallets->end(), [&](const Wallet& wallet) { return wallet.public_key == key; });
        Require(found != wallets->end(), "Test committee contains an unknown key");
        return *found;
    }

    election::Digest Anchor(std::uint64_t round) const {
        const auto start = legacy::EpochStart(round);
        std::vector<std::uint8_t> encoded(16 + legacy::config.epochBlocks * 32);
        legacy::detail::Write64(encoded.data(), round);
        legacy::detail::Write64(encoded.data() + 8, legacy::config.epochBlocks);
        for (std::uint64_t i = 0; i < legacy::config.epochBlocks; ++i) {
            const auto& hash = hashes.at(start + i);
            std::copy(hash.begin(), hash.end(), encoded.begin() + 16 + i * 32);
        }
        return chain::vdf_election::detail::Hash("Chain/VdfConsensus/Anchor/v1", {genesisRoot, encoded});
    }

    election::Digest Secret(const election::PublicKey& key) const {
        auto value = key;
        value[0] ^= static_cast<std::uint8_t>(state.epoch + 101);
        // Both branches share the first two complete rounds, then choose
        // different legitimate randomness for the following election.
        if (state.epoch >= 2) value[1] ^= variant;
        return value;
    }

    const ForkEntry& Add(const std::vector<election::Ticket>& tickets = {}) {
        const auto height = hashes.rbegin()->first + 1;
        const auto slot = legacy::StartSlot() + blocks.size();
        const auto parent = hashes.rbegin()->second;
        if (legacy::Epoch(height) != state.epoch) state = rules::ForNextEpoch(state, slot, parent, legacy::config);
        state = rules::ForSlot(state, slot, legacy::config);
        const auto schedule = rules::Preview(state, slot, legacy::config);
        Require(bool(schedule), "Test branch has no scheduled producer");
        const auto offset = rules::Offset(state, slot);
        const auto beacon = rules::BeaconContextFor(state);
        rules::Delta delta;
        delta.tickets = tickets;
        if (offset == legacy::config.commitStartSlot) {
            for (const auto& key : state.committee)
                delta.commits.push_back(election::CreateCommit(beacon, WalletFor(key).private_key, Secret(key)));
        }
        if (offset == legacy::config.commitEndSlot) {
            for (const auto& key : state.committee)
                delta.reveals.push_back(election::CreateReveal(beacon, WalletFor(key).private_key, Secret(key)));
        }
        if (state.epoch == 2 && offset == legacy::config.revealEndSlot && tickets.empty()) {
            const auto context = rules::TicketContextFor(state, legacy::config);
            Require(context.matureRounds == 1, "Test maturity assumption changed");
            const auto& wallet = wallets->at(elected);
            const auto work = election::CreateWork(genesisRoot, wallet.public_key, 0, Anchor(0), {}, context.iterations);
            delta.tickets.push_back(election::CreateTicket(context, wallet.private_key,
                [&](std::uint64_t round, election::Work& output) {
                    if (round != 0) return false;
                    output = work; return true;
                }));
        }
        if (offset >= legacy::config.proofEndSlot && !state.frozen)
            delta.finalization = rules::ExpectedFinalization(state, slot, legacy::config);

        Block block;
        block.height = height; block.previousHash = parent;
        legacy::Extension extension;
        extension.slot = slot; extension.producer = schedule->producer; extension.turn = schedule->turn;
        extension.work = rules::EncodeDelta(delta); block.vdf = std::move(extension);
        block.merkleRoot = legacy::MerkleRoot(MerkleCompute(std::vector<election::Digest>{}), *block.vdf);
        (void)SerializeBlockALL(block);
        legacy::Sign(*block.vdf, block.hash, WalletFor(schedule->producer).private_key);
        auto bytes = SerializeBlockALL(block);
        Require(VerifyBlock(bytes), "Independent Work branch block failed generic verification");
        state = rules::Advance(state, delta, slot, schedule->producer, schedule->turn,
            [&](std::uint64_t round, election::Digest& anchor) {
                try { anchor = Anchor(round); return true; } catch (...) { return false; }
            }, legacy::config);
        hashes.emplace(height, block.hash);
        blocks.emplace_back(std::move(block), std::move(bytes));
        return blocks.back();
    }
};

void TestPreparedTicketBroadcastTiming(const std::filesystem::path& directory,
                                     const std::vector<Wallet>& wallets) {
    struct Restore {
        legacy::Config config = legacy::config;
        decltype(legacy::currentTimeMs) clock = legacy::currentTimeMs;
        election::PublicKey publicKey = vdfPublicKey;
        bool ready = vdfIdentityReady;
        bool producing = produceBlocks;
        ~Restore() {
            db.reset();
            ClearVdfExtensionCache(); ClearWorkStateCache();
            { std::lock_guard lock(workRuntimeMutex); workRuntimeSets.clear(); }
            legacy::config = config; legacy::currentTimeMs = clock;
            vdfPublicKey = publicKey; vdfIdentityReady = ready; produceBlocks = producing;
        }
    } restore;
    auto bootstrap = nlohmann::json::array();
    for (std::size_t i = 0; i < 5; ++i) bootstrap.push_back(U32ToHex(wallets[i].public_key));
    SetGenesisUsers({{"base_fee", 0}, {"accounts", nlohmann::json::array()},
        {"vdf", {{"enabled", true}, {"producer_rotation", true}, {"epoch_blocks", 160},
            {"consecutive_blocks", 32}, {"producer_timeout_slots", 8}, {"slot_ms", 200},
            {"start_time_ms", 200000}, {"fallback_slots", 20}, {"future_slots", 1}, {"producers", 5},
            {"iterations", chain::vdf_election::kMinIterations}, {"work_election", true},
            {"work_iterations", chain::vdf_election::kMinIterations}, {"work_groups", 4}, {"work_group_size", 8},
            {"commit_start_slot", 80}, {"commit_end_slot", 90}, {"reveal_end_slot", 100},
            {"ticket_end_slot", 120}, {"proof_end_slot", 140}, {"bootstrap_producers", bootstrap}}}});
    ClearVdfExtensionCache(); ClearWorkStateCache();
    InitDB(directory.string(), true); GenerateGenesisBlock();
    Branch branch(DBReadCurrentBlock(), wallets, 0, 5);
    const auto commit = [&](const std::vector<election::Ticket>& tickets = std::vector<election::Ticket>{}) {
        const auto& entry = branch.Add(tickets);
        Require(DBCheckBlockUsers(entry.second) && DBCommitBlock(entry.first.hash, entry.second),
            "Broadcast boundary fixture block was rejected");
    };
    for (unsigned phase = 0; phase < 2 * 160 + 100; ++phase) commit();
    const auto startSlot = branch.state.startSlot;
    const auto state = rules::ForSlot(branch.state, startSlot + 100, legacy::config);
    const auto context = rules::TicketContextFor(state, legacy::config);
    Require(context.matureRounds == 1, "Broadcast fixture should require one historical Work");
    std::vector<election::Ticket> high;
    for (unsigned seed = 8; seed <= 255 && high.size() < 2; ++seed) {
        const auto wallet = Seeded(static_cast<std::uint8_t>(seed));
        const auto score = election::DeriveScore(context, wallet.public_key);
        const std::uint64_t prefix = (std::uint64_t(score[0]) << 24) |
            (std::uint64_t(score[1]) << 16) | (std::uint64_t(score[2]) << 8) | score[3];
        if (prefix * 100 < (std::uint64_t{1} << 32) * 15) continue;
        const auto work = election::CreateWork(genesisRoot, wallet.public_key, 0,
            branch.Anchor(0), {}, context.iterations);
        high.push_back(election::CreateTicket(context, wallet.private_key,
            [&](std::uint64_t round, election::Work& output) {
                if (round != 0) return false;
                output = work; return true;
            }));
        Require(election::VerifyTicket(high.back(), context,
            [&](std::uint64_t round, election::Digest& anchor) {
                if (round != 0) return false;
                anchor = branch.Anchor(0); return true;
            }, nullptr, &rules::proofCache), "Broadcast fixture ticket is invalid");
    }
    Require(high.size() == 2, "Fixed identities did not produce two high-score tickets");
    Require(StoreWorkTicket(std::make_shared<const election::Ticket>(high[0]), state),
        "Verified high-score fixture was not stored");
    vdfIdentityReady = true; produceBlocks = true;
    const auto prepare = [&](unsigned phase, std::size_t expectedTickets) {
        const auto slot = startSlot + phase;
        legacy::currentTimeMs = [slot] { return slot * legacy::config.slotMs; };
        const auto current = rules::ForSlot(branch.state, slot, legacy::config);
        const auto schedule = rules::Preview(current, slot, legacy::config);
        Require(bool(schedule), "Broadcast fixture has no scheduled producer");
        vdfPublicKey = schedule->producer;
        legacy::Extension extension;
        Require(PrepareWorkBlock(DBReadBlockHeight() + 1, DBReadCurrentBlock(), extension),
            "Scheduled producer could not prepare the broadcast fixture block");
        rules::Delta delta;
        Require(extension.work && rules::DecodeDelta(*extension.work, delta),
            "Prepared broadcast delta could not be decoded");
        Require(delta.tickets.size() == expectedTickets,
            "Prepared block did not respect the ticket broadcast threshold");
        if (expectedTickets) Require(delta.tickets.front() == high[0],
            "Prepared block omitted the newly eligible ticket or repeated an existing ticket");
        Require(SameState(CurrentState(), branch.state), "Preparing a block changed the existing ticket pool");
    };
    prepare(100, 0);
    // An already accepted ticket stays in the parent pool even if this node
    // also holds its runtime copy. Block preparation must not send it again.
    legacy::currentTimeMs = [] { return std::uint64_t{100000} * legacy::config.slotMs; };
    commit({high[1]});
    Require(StoreWorkTicket(std::make_shared<const election::Ticket>(high[1]), branch.state),
        "Existing pool ticket could not be copied into the runtime fixture");
    for (unsigned phase = 101; phase < 119; ++phase) commit();
    Require(branch.state.pool.size() == 1 && *branch.state.pool.front() == high[1],
        "Accepted fixture ticket was not preserved in the parent pool");
    prepare(119, 0);
    prepare(120, 1);
}

void ClearForkCache() {
    std::lock_guard lock(forkMutex);
    forkBlocks.clear(); forkChecks.clear(); forkBytes = 0; forkReady = false;
}

void QueueAncestors(const Branch& branch) {
    for (std::size_t i = 0; i + 1 < branch.blocks.size(); ++i) {
        const auto& entry = branch.blocks[i];
        if (DBReadBlockHashByHeight(entry.first.height) == entry.first.hash) continue;
        Require(QueueForkBlock(entry.first, entry.second, false), "Alternative ancestor was not queued");
    }
}

ForkEntry WrongProducer(const ForkEntry& entry, const Wallet& wrong) {
    auto block = entry.first;
    block.vdf->producer = wrong.public_key;
    block.merkleRoot = legacy::MerkleRoot(MerkleCompute(std::vector<election::Digest>{}), *block.vdf);
    (void)SerializeBlockALL(block);
    legacy::Sign(*block.vdf, block.hash, wrong.private_key);
    auto bytes = SerializeBlockALL(block);
    Require(VerifyBlock(bytes), "Wrong-producer fixture must retain valid block encoding and signatures");
    return {std::move(block), std::move(bytes)};
}

} // namespace

int main(int argc, char** argv) {
    try {
        Require(sodium_init() >= 0, "Sodium initialization failed");
        spdlog::set_level(spdlog::level::off);
        const auto directory = std::filesystem::path(argc > 1 ? argv[1] : "work-state-data") / std::to_string(GetSteadyTime());
        std::filesystem::create_directories(directory);
        std::vector<Wallet> wallets;
        for (std::uint8_t i = 1; i <= 7; ++i) wallets.push_back(Seeded(i));
        auto bootstrap = nlohmann::json::array();
        for (std::size_t i = 0; i < 5; ++i) bootstrap.push_back(U32ToHex(wallets[i].public_key));
        SetGenesisUsers({{"base_fee", 0}, {"accounts", nlohmann::json::array()},
            {"vdf", {{"enabled", true}, {"producer_rotation", true}, {"epoch_blocks", 10},
                {"consecutive_blocks", 2}, {"producer_timeout_slots", 2}, {"slot_ms", 200},
                {"start_time_ms", 200000}, {"fallback_slots", 20}, {"future_slots", 1}, {"producers", 5},
                {"iterations", chain::vdf_election::kMinIterations}, {"work_election", true},
                {"work_iterations", chain::vdf_election::kMinIterations}, {"work_groups", 4}, {"work_group_size", 8},
                {"commit_start_slot", 1}, {"commit_end_slot", 2}, {"reveal_end_slot", 3},
                {"ticket_end_slot", 4}, {"proof_end_slot", 5}, {"bootstrap_producers", bootstrap}}}});
        TestOwnTicketVerificationTiming();
        legacy::currentTimeMs = [] { return std::uint64_t{100000} * legacy::config.slotMs; };
        InitDB(directory.string(), true); GenerateGenesisBlock();
        const auto genesis = DBReadCurrentBlock();
        Branch main(genesis, wallets, 0, 5);
        for (unsigned i = 0; i < 31; ++i) {
            const auto& entry = main.Add();
            Require(DBCheckBlockUsers(entry.second), "Prepared Work block rejected");
            Require(DBCommitBlock(entry.first.hash, entry.second), "Work block commit failed");
            Require(SameState(CurrentState(), main.state), "Committed state differs from independent branch calculation");
            // A full block can arrive before its own 200 ms slot has ended.
            // Observing the next round must also tolerate a clock correction.
            std::uint64_t previewSlot = 0;
            const auto upcoming = ReadWorkState(entry.first.vdf->slot, &previewSlot);
            Require(previewSlot == entry.first.vdf->slot + 1 && upcoming.epoch == legacy::Epoch(entry.first.height + 1),
                    "Same-slot head did not produce a valid next-block preview");
            const auto corrected = ReadWorkState(entry.first.vdf->slot - 1, &previewSlot);
            Require(previewSlot == entry.first.vdf->slot + 1 && SameState(upcoming, corrected),
                    "Clock correction changed the next-block preview");
        }
        const auto mainHead = DBReadCurrentBlock();
        const auto savedMain = CurrentState();
        Require(savedMain.epoch == 3 && savedMain.committee == std::vector<election::PublicKey>{wallets[5].public_key},
                "Historical Work did not elect a new producer");
        {
            std::shared_lock lock(dbCommitMutex);
            Require(DBWorkAnchor(0) == main.Anchor(0), "Database historical digest differs from branch digest");
        }
        ClearVdfExtensionCache(); ClearWorkStateCache();
        Require(SameState(CurrentState(), savedMain), "Cold replay changed committee, random seed or producer turn");

        Branch alternate(genesis, wallets, 1, 6);
        for (unsigned i = 0; i < 32; ++i) alternate.Add();
        Require(alternate.hashes.at(21) == main.hashes.at(21) && alternate.hashes.at(23) != main.hashes.at(23),
                "Fork does not preserve its intended common prefix");
        Require(alternate.state.committee == std::vector<election::PublicKey>{wallets[6].public_key},
                "Alternative branch did not derive its own committee");

        QueueAncestors(alternate);
        auto invalidTip = WrongProducer(alternate.blocks.back(), wallets[0]);
        std::vector<std::vector<std::uint8_t>> detached;
        Require(AdoptFork(invalidTip, detached) == -1, "Invalid Work fork was accepted");
        Require(detached.empty() && DBReadCurrentBlock() == mainHead && SameState(CurrentState(), savedMain),
                "Failed fork leaked its committee or state into the canonical chain");
        ClearVdfExtensionCache(); ClearWorkStateCache();
        Require(SameState(CurrentState(), savedMain), "Failed fork changed persistent election records");
        ClearForkCache();

        QueueAncestors(alternate);
        Require(AdoptFork(alternate.blocks.back(), detached) == 1, "Valid Work fork was not adopted");
        Require(!detached.empty() && DBReadCurrentBlock() == alternate.blocks.back().first.hash &&
                SameState(CurrentState(), alternate.state), "Adopted fork retained old committee state");
        ClearForkCache();
        const auto savedHead = DBReadCurrentBlock();
        const auto savedAlternate = CurrentState();
        db.reset(); InitDB(directory.string(), true); GenerateGenesisBlock();
        Require(DBReadCurrentBlock() == savedHead && SameState(CurrentState(), savedAlternate),
                "Database reopening did not recover the selected committee and producer turn");
        Require(CurrentState().committee == std::vector<election::PublicKey>{wallets[6].public_key},
                "Restart restored the wrong branch committee");
        db.reset(); ClearForkCache();
        TestPreparedTicketBroadcastTiming(directory / "broadcast", wallets);
        std::cout << "Work state checks passed. Database: " << directory.string() << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Work state check failed: " << error.what() << '\n';
        return 1;
    }
}
