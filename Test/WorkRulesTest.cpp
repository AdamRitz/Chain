#include "../Consensus/WorkRules.h"

#include <iostream>

namespace {
namespace rules = chain::work_consensus;
namespace legacy = chain::vdf_consensus;
namespace election = chain::work_election;

void Require(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
template<class Fn> void Reject(Fn fn, const char* message) {
    bool rejected = false;
    try { fn(); } catch (const std::invalid_argument&) { rejected = true; }
    Require(rejected, message);
}

struct Fixture {
    legacy::Config config;
    std::array<election::SecretKey, 5> keys{};
    election::Digest chainId{}, anchor{};
    Fixture() {
        config.enabled = config.producerRotation = config.workElection = true;
        config.epochBlocks = 160; config.consecutiveBlocks = 32;
        config.slotMs = 200; config.startTimeMs = 200000;
        config.producerTimeoutSlots = 2; config.futureSlots = 1;
        chainId[0] = 71; anchor[0] = 31;
        for (std::size_t i = 0; i < keys.size(); ++i) {
            std::array<std::uint8_t, crypto_sign_SEEDBYTES> seed{}; seed.fill(static_cast<std::uint8_t>(i + 1));
            election::PublicKey pk{};
            Require(crypto_sign_seed_keypair(pk.data(), keys[i].data(), seed.data()) == 0, "Key generation failed");
            config.bootstrapProducers.push_back(pk);
        }
    }
    rules::State Initial(std::uint64_t epoch = 0) const {
        return rules::Initialize(config, chainId, epoch, 1000, anchor, config.bootstrapProducers, 999);
    }
};

const election::AnchorLookup noHistory = [](std::uint64_t, election::Digest&) { return false; };

void TestConfigurationAndCodec(const Fixture& f) {
    auto old = f.config; old.workElection = false;
    const auto bytes = legacy::EncodeConfig(old);
    Require(bytes.size() == 96 && legacy::detail::Read64(bytes.data()) == 3, "Legacy turn genesis changed");
    old.workIterations += 17; old.workGroups = 1; old.bootstrapProducers.clear();
    Require(legacy::EncodeConfig(old) == bytes, "Disabled Work fields changed legacy genesis bytes");
    Require(legacy::detail::Read64(legacy::EncodeConfig(f.config).data()) == 4, "Work genesis version missing");
    auto changed = f.config; changed.bootstrapProducers[0][0] ^= 1;
    Require(legacy::EncodeConfig(changed) != legacy::EncodeConfig(f.config), "Bootstrap identity not committed by genesis");
    changed = f.config; changed.workGroups = 32; changed.workGroupSize = 64;
    Reject([&] { legacy::ValidateConfig(changed); }, "Unbounded Work proof count accepted");

    legacy::Extension extension;
    extension.slot = 1000; extension.producer = f.config.bootstrapProducers[0];
    extension.turn = legacy::ProducerTurn{0, 1}; extension.work = rules::EncodeDelta({});
    legacy::Extension decoded;
    const auto encoded = legacy::Encode(extension);
    Require(legacy::Parse(encoded, decoded) && decoded == extension, "Work extension roundtrip failed");
    rules::Delta delta;
    Require(rules::DecodeDelta(*decoded.work, delta) && delta == rules::Delta{}, "Empty Work delta roundtrip failed");
    auto trailing = *decoded.work; trailing.push_back(0);
    Require(!rules::DecodeDelta(trailing, delta), "Work delta trailing data accepted");
    auto bad = encoded; bad[50] = 1;
    Require(!legacy::Parse(bad, decoded), "Work block accepted legacy candidate count");
    extension.work.reset();
    const auto oldExtension = legacy::Encode(extension);
    Require(legacy::Parse(oldExtension, decoded) && decoded == extension, "Legacy turn extension no longer decodes");
    Require(rules::VerifyDeltaProofs({}, f.config), "Empty block proof preparation failed");
    rules::Delta duplicateWork;
    election::Ticket repeated;
    repeated.context.iterations = f.config.workIterations;
    repeated.context.groups = static_cast<std::uint32_t>(f.config.workGroups);
    repeated.context.groupSize = static_cast<std::uint32_t>(f.config.workGroupSize);
    repeated.context.matureRounds = 2;
    repeated.works.resize(2);
    duplicateWork.tickets.push_back(repeated);
    Require(!rules::VerifyDeltaProofs(duplicateWork, f.config), "Repeated history round accepted by proof preparation");
    duplicateWork.tickets[0].context.iterations += 1;
    Require(!rules::VerifyDeltaProofs(duplicateWork, f.config), "Wrong Work iterations accepted by proof preparation");
}

void TestBeaconAndFinalization(const Fixture& f) {
    auto state = f.Initial(2);
    const auto context = rules::BeaconContextFor(state);
    election::Digest first{}, second{}; first[0] = 23; second[0] = 29;
    rules::Delta commits;
    commits.commits = {election::CreateCommit(context, f.keys[0], first), election::CreateCommit(context, f.keys[1], second)};
    Reject([&] { (void)rules::ValidateDelta(state, commits, 1079, noHistory, f.config); }, "Early commitment accepted");
    auto committed = rules::ValidateDelta(state, commits, 1080, noHistory, f.config);
    Reject([&] { (void)rules::ValidateDelta(committed, commits, 1081, noHistory, f.config); }, "Duplicate commitment accepted");
    Reject([&] { (void)rules::ValidateDelta(state, commits, 1090, noHistory, f.config); }, "Late commitment accepted");
    rules::Delta reveals;
    reveals.reveals = {election::CreateReveal(context, f.keys[0], first)};
    Reject([&] { (void)rules::ValidateDelta(committed, reveals, 1089, noHistory, f.config); }, "Early reveal accepted");
    auto revealed = rules::ValidateDelta(committed, reveals, 1090, noHistory, f.config);
    Reject([&] { (void)rules::ValidateDelta(revealed, reveals, 1091, noHistory, f.config); }, "Duplicate reveal accepted");
    Reject([&] { (void)rules::ValidateDelta(committed, reveals, 1100, noHistory, f.config); }, "Late reveal changed seed");
    auto otherBranch = committed; otherBranch.anchor[0] ^= 1;
    Reject([&] { (void)rules::ValidateDelta(otherBranch, reveals, 1090, noHistory, f.config); }, "Reveal replayed across round anchor");
    const auto seeded = rules::ForSlot(revealed, 1100, f.config);
    Require(seeded.seedReady && seeded.missingMask == 30, "Accepted reveal incorrectly marked missing");
    Require(rules::TicketContextFor(seeded, f.config).matureRounds == 1, "Latest unfinished Work was sampled");
    Require(rules::ForSlot(revealed, 1120, f.config).seed == seeded.seed, "Seed changed with receive time");
    Reject([&] { (void)rules::ValidateDelta(seeded, {}, 1140, noHistory, f.config); }, "Election result silently omitted");
    rules::Delta final;
    final.finalization = rules::ExpectedFinalization(seeded, 1140, f.config);
    Require(final.finalization->values[0] == first && final.finalization->nextCommittee == f.config.bootstrapProducers,
        "Election result lost reveal or empty-pool committee");
    auto forged = final; forged.finalization->values[0] = {}; forged.finalization->missingMask |= 1;
    Reject([&] { (void)rules::ValidateDelta(seeded, forged, 1140, noHistory, f.config); }, "Producer erased accepted reveal");
    const auto frozen = rules::ValidateDelta(seeded, final, 1140, noHistory, f.config);
    Require(frozen.frozen && frozen.nextCommittee == f.config.bootstrapProducers, "Fallback committee changed");
    Reject([&] { (void)rules::ValidateDelta(frozen, final, 1141, noHistory, f.config); }, "Election finalized twice");
    rules::Delta decoded;
    Require(rules::DecodeDelta(rules::EncodeDelta(final), decoded) && decoded == final, "Final election codec failed");

    auto bootstrap = rules::ForSlot(f.Initial(), 1100, f.config);
    rules::Delta emptyWorkTicket;
    election::Ticket ticket; ticket.context = rules::TicketContextFor(bootstrap, f.config);
    ticket.publicKey = f.config.bootstrapProducers[2];
    election::SignTicket(ticket, f.keys[2]); emptyWorkTicket.tickets.push_back(ticket);
    Reject([&] { (void)rules::ValidateDelta(bootstrap, emptyWorkTicket, 1100, noHistory, f.config); }, "Identity without historical Work entered bootstrap election");
}

void TestScheduling(const Fixture& f) {
    auto state = f.Initial();
    auto schedule = rules::Preview(state, 4000, f.config);
    Require(schedule && schedule->producer == f.config.bootstrapProducers[0], "Delayed initial startup removed bootstrap owner");
    const auto initialReplacement = rules::Preview(state, 1002, f.config);
    Require(initialReplacement && initialReplacement->producer == f.config.bootstrapProducers[1] &&
        initialReplacement->turn == legacy::ProducerTurn{1, 1} && initialReplacement->disqualifiedMask == 0,
        "Offline first bootstrap member prevented chain startup");
    const auto delayedReplacement = rules::Preview(state, 4002, f.config);
    Require(delayedReplacement && delayedReplacement->producer == f.config.bootstrapProducers[1] &&
        delayedReplacement->disqualifiedMask == 0, "Delayed startup changed the deterministic initial schedule");
    for (std::uint64_t index = 0; index < 160; ++index) {
        const auto slot = 1000 + index;
        schedule = rules::Preview(state, slot, f.config);
        Require(schedule && schedule->producer == f.config.bootstrapProducers[index / 32], "Consecutive producer schedule differs");
        rules::Delta delta;
        if (index == 140) delta.finalization = rules::ExpectedFinalization(state, slot, f.config);
        state = rules::Advance(state, delta, slot, schedule->producer, schedule->turn, noHistory, f.config);
    }
    Require(state.blocksProduced == 160 && state.frozen && !rules::Preview(state, 1160, f.config), "Finished round continued producing");
    auto next = rules::ForNextEpoch(state, 1160, f.anchor, f.config);
    Require(next.epoch == 1 && next.blocksProduced == 0 && next.startSlot == 1160, "Next round did not reset its deadlines");
    Require(rules::Preview(next, 1160, f.config)->producer == f.config.bootstrapProducers[0], "Next round did not restart schedule");

    state = f.Initial();
    auto first = rules::Preview(state, 1000, f.config);
    state = rules::Advance(state, {}, 1000, first->producer, first->turn, noHistory, f.config);
    auto timeout = rules::Preview(state, 1003, f.config);
    Require(timeout && timeout->producer == f.config.bootstrapProducers[1] && timeout->turn.blocks == 2 && timeout->disqualifiedMask == 1,
        "Two empty slots did not transfer remaining allocation");
    auto outsider = f.config.bootstrapProducers[1]; outsider[0] ^= 1;
    Reject([&] { (void)rules::Advance(state, {}, 1003, outsider, timeout->turn, noHistory, f.config); }, "Outside identity produced after timeout");
    state = rules::Advance(state, {}, 1003, timeout->producer, timeout->turn, noHistory, f.config);
    Require(state.disqualifiedMask == 1, "Timed-out member was not retained in round state");
    Require(!rules::Preview(state, 1012, f.config), "All-timed-out committee recovered before recovery gap");
    const auto recovered = rules::Preview(state, 1023, f.config);
    Require(recovered && recovered->disqualifiedMask == 0 &&
        std::find(f.config.bootstrapProducers.begin(), f.config.bootstrapProducers.end(), recovered->producer) != f.config.bootstrapProducers.end(),
        "Recovery failed or admitted outside identity");
}
} // namespace

int main() {
    try {
        Require(sodium_init() >= 0, "Sodium initialization failed");
        const Fixture fixture;
        TestConfigurationAndCodec(fixture);
        TestBeaconAndFinalization(fixture);
        TestScheduling(fixture);
        std::cout << "Work rules tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n'; return 1;
    }
}
