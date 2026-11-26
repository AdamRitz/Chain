#include "../Consensus/VdfRules.h"
#include "../User/User.h"

#include <iostream>
#include <map>
#include <stdexcept>
#include <tuple>

namespace {

namespace rules = chain::vdf_consensus;
namespace election = chain::vdf_election;

void Require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

election::SecretKey MakeKey(std::uint8_t value) {
    std::array<std::uint8_t, crypto_sign_SEEDBYTES> seed{};
    seed.fill(value);
    election::PublicKey publicKey{};
    election::SecretKey secretKey{};
    Require(crypto_sign_seed_keypair(publicKey.data(), secretKey.data(), seed.data()) == 0, "Key generation failed");
    return secretKey;
}

election::Digest NumberHash(std::uint64_t number) {
    std::array<std::uint8_t, 8> bytes{};
    rules::detail::Write64(bytes.data(), number);
    return election::detail::Hash("VdfRulesTest/Block", {bytes});
}

void TestGenesis() {
    const nlohmann::json legacy{{"base_fee", 0}, {"accounts", nlohmann::json::array()}};
    SetGenesisUsers(legacy);
    const auto legacyRoot = genesisRoot;
    std::vector<std::uint8_t> legacyBytes;
    const std::string domain = "Chain accounts v3";
    legacyBytes.insert(legacyBytes.end(), domain.begin(), domain.end());
    legacyBytes.resize(legacyBytes.size() + 16, 0);
    election::Digest expected{};
    crypto_generichash(expected.data(), expected.size(), legacyBytes.data(), legacyBytes.size(), nullptr, 0);
    Require(legacyRoot == expected, "Legacy genesis hash changed");
    Require(GetGenesisConfig() == legacy && !rules::config.enabled, "Legacy genesis configuration changed");

    auto configured = legacy;
    configured["vdf"] = {{"enabled", true}, {"epoch_blocks", 2}, {"iterations", 4096}, {"slot_ms", 50},
                         {"start_time_ms", 10000}, {"fallback_slots", 20}, {"future_slots", 2}, {"producers", 5}};
    SetGenesisUsers(configured);
    const auto enabledRoot = genesisRoot;
    Require(enabledRoot != legacyRoot && rules::config.enabled, "VDF parameters did not change chain identity");
    // Reproduce the original configuration independently: adding an optional
    // rule must not change any byte hashed by an existing VDF genesis.
    const std::array<std::uint64_t, 9> originalFields{1, 1, 2, 4096, 50, 20, 2, 5, 10000};
    std::vector<std::uint8_t> originalParameters(72, 0);
    for (std::size_t i = 0; i < originalFields.size(); ++i)
        rules::detail::Write64(originalParameters.data() + i * 8, originalFields[i]);
    Require(rules::EncodeConfig(rules::config) == originalParameters, "Existing VDF configuration bytes changed");
    auto originalGenesisBytes = legacyBytes;
    const std::string vdfDomain = "Chain VDF consensus v1";
    originalGenesisBytes.insert(originalGenesisBytes.end(), vdfDomain.begin(), vdfDomain.end());
    originalGenesisBytes.insert(originalGenesisBytes.end(), originalParameters.begin(), originalParameters.end());
    crypto_generichash(expected.data(), expected.size(), originalGenesisBytes.data(), originalGenesisBytes.size(), nullptr, 0);
    Require(enabledRoot == expected, "Existing VDF genesis hash changed");
    const auto canonical = GetGenesisConfig();
    SetGenesisUsers(canonical);
    Require(genesisRoot == enabledRoot && GetGenesisConfig() == canonical, "VDF genesis roundtrip changed identity");
    auto rotation = configured;
    rotation["vdf"]["producer_rotation"] = false;
    SetGenesisUsers(rotation);
    Require(genesisRoot == enabledRoot && GetGenesisConfig() == canonical && !rules::config.producerRotation,
            "Explicitly disabled rotation changed the existing VDF genesis");
    rotation["vdf"]["producer_rotation"] = true;
    SetGenesisUsers(rotation);
    const auto rotationRoot = genesisRoot;
    Require(rotationRoot != enabledRoot && rules::config.producerRotation,
            "Producer rotation is not bound to the genesis hash");
    const auto rotationBytes = rules::EncodeConfig(rules::config);
    Require(rotationBytes.size() == 80 && rules::detail::Read64(rotationBytes.data()) == 2 &&
            rules::detail::Read64(rotationBytes.data() + 72) == 1,
            "Producer rotation did not use its configuration version");
    Require(GetGenesisConfig() == rotation, "Genesis export omitted the producer rotation rule");
    SetGenesisUsers(GetGenesisConfig());
    Require(genesisRoot == rotationRoot && rules::config.producerRotation, "Rotating genesis roundtrip changed identity");
    SetGenesisUsers(configured);
    for (const auto& invalidValue : std::vector<nlohmann::json>{nullptr, 0, 1, "true", nlohmann::json::array(), nlohmann::json::object()}) {
        auto invalid = rotation;
        invalid["vdf"]["producer_rotation"] = invalidValue;
        try {
            SetGenesisUsers(invalid);
            throw std::runtime_error("Non-boolean producer rotation accepted");
        } catch (const std::invalid_argument&) {
        }
        Require(genesisRoot == enabledRoot && !rules::config.producerRotation,
                "Invalid producer rotation changed the existing configuration");
    }
    for (const auto& [field, value] : std::map<std::string, std::uint64_t>{{"epoch_blocks", 3}, {"iterations", 8192},
            {"slot_ms", 100}, {"start_time_ms", 10050}, {"fallback_slots", 21}, {"future_slots", 1}}) {
        auto changed = configured;
        changed["vdf"][field] = value;
        SetGenesisUsers(changed);
        Require(genesisRoot != enabledRoot, "A VDF parameter is not bound to genesis");
    }
    SetGenesisUsers(configured);
    for (const auto& [field, value] : std::map<std::string, std::uint64_t>{{"epoch_blocks", 0}, {"iterations", 4095},
            {"slot_ms", 0}, {"start_time_ms", 0}, {"fallback_slots", 2}, {"future_slots", 3}, {"producers", 4}, {"unknown", 1}}) {
        auto invalid = configured;
        invalid["vdf"][field] = value;
        try {
            SetGenesisUsers(invalid);
            throw std::runtime_error("Invalid VDF genesis parameter accepted");
        } catch (const std::invalid_argument&) {
        }
        Require(genesisRoot == enabledRoot, "Invalid genesis changed the existing chain identity");
    }
    for (const auto& start : std::vector<nlohmann::json>{nullptr, 10001, -50, 50.5}) {
        auto invalid = configured;
        if (start.is_null()) invalid["vdf"].erase("start_time_ms");
        else invalid["vdf"]["start_time_ms"] = start;
        try {
            SetGenesisUsers(invalid);
            throw std::runtime_error("Missing, unaligned or non-integer VDF start time accepted");
        } catch (const std::invalid_argument&) {
        }
        Require(genesisRoot == enabledRoot, "Invalid start time changed chain identity");
    }
    SetGenesisUsers(legacy);
    Require(genesisRoot == legacyRoot && !rules::config.enabled && !rules::config.producerRotation,
            "Loading legacy genesis retained VDF mode or rotation");
}

void TestRotation(rules::Config configured, const std::vector<election::Candidate>& active,
    const std::vector<election::Candidate>& next, const election::Candidate& outsider,
    const election::Candidate& bootstrap, const election::Context& context0,
    const election::Context& context2, const election::Context& context3) {
    configured.producerRotation = true;
    const auto start = rules::StartSlot(configured);
    const auto minimum = rules::MinimumSlot(rules::EpochStart(2, configured), configured);
    Require(!rules::ScheduledProducer(start - 1, active, configured), "Schedule accepted a slot before genesis");
    Require(!rules::ScheduledProducer(start, {}, configured), "Empty committee has a scheduled producer");
    for (std::size_t size : {std::size_t{1}, std::size_t{2}, std::size_t{5}}) {
        const auto members = std::span<const election::Candidate>(active).first(size);
        for (std::uint64_t slot = 101; slot < 111; ++slot) {
            const auto expectedIndex = (slot - start) % size;
            Require(rules::ScheduledProducer(slot, members, configured) == members[expectedIndex].publicKey,
                    "Schedule does not follow score order and absolute slot");
            for (std::size_t i = 0; i < size; ++i) {
                rules::Extension block;
                block.slot = slot;
                block.producer = members[i].publicKey;
                Require(rules::CanProduce(block.producer, slot, slot - 1, 2, members, configured) == (i == expectedIndex),
                        "Producer timing helper accepted the wrong member");
                Require(rules::VerifyRules(block, slot - 1, {}, members, context2, context3, minimum, configured) == (i == expectedIndex),
                        "Consensus validation differs from scheduled producer");
                block.producerProof = members[i];
                Require(rules::VerifyRules(block, slot - 1, {}, members, context2, context3, minimum, configured) == (i == expectedIndex),
                        "Member proof bypassed the rotation rule");
            }
        }
    }
    rules::Extension block;
    block.slot = 103;
    block.producer = *rules::ScheduledProducer(block.slot, active, configured);
    Require(rules::VerifyRules(block, 100, {}, active, context2, context3, minimum, configured),
            "Next available scheduled member could not skip two absent producers");
    Require(!rules::CanProduce(block.producer, 100, 100, 2, active, configured), "Duplicate slot passed the timing helper");
    block.slot = 120;
    block.producer = active[0].publicKey;
    Require(block.producer != *rules::ScheduledProducer(block.slot, active, configured), "Wrong-slot test needs a different member");
    block.producerProof = active[0];
    Require(!rules::VerifyRules(block, 100, {}, active, context2, context3, minimum, configured),
            "Recovery gap allowed a member to ignore its rotation");
    block.producer = outsider.publicKey;
    block.producerProof = outsider;
    block.slot = 119;
    Require(!rules::VerifyRules(block, 100, {}, active, context2, context3, minimum, configured),
            "Rotation shortened outsider recovery gap");
    block.slot = 120;
    Require(rules::VerifyRules(block, 100, {}, active, context2, context3, minimum, configured),
            "Rotation removed existing outsider recovery");
    block.producerProof.reset();
    Require(!rules::VerifyRules(block, 100, {}, active, context2, context3, minimum, configured),
            "Outsider recovery accepted a missing proof");

    // The next epoch changes the list, but not the global slot arithmetic.
    const auto following = std::span<const election::Candidate>(next).first(5);
    block.slot = 104;
    block.producer = *rules::ScheduledProducer(block.slot, following, configured);
    Require(block.producer == following[(block.slot - start) % following.size()].publicKey,
            "Epoch transition reset the rotation");
    Require(rules::VerifyRules(block, 103, {}, following, context3, std::nullopt,
            rules::MinimumSlot(rules::EpochStart(3, configured), configured), configured),
            "New epoch's scheduled member rejected");
    block.producer = active[(block.slot - start) % active.size()].publicKey;
    if (block.producer != *rules::ScheduledProducer(block.slot, following, configured))
        Require(!rules::VerifyRules(block, 103, {}, following, context3, std::nullopt, minimum, configured),
                "Old epoch's schedule remained authorized");

    block.slot = 101;
    block.producer = bootstrap.publicKey;
    block.producerProof = bootstrap;
    Require(rules::VerifyRules(block, 100, {}, {}, context0, std::nullopt, start, configured),
            "Rotation prevented open bootstrap production");
    block.producer = outsider.publicKey;
    block.producerProof = outsider;
    Require(rules::VerifyRules(block, 100, {}, {}, context2, context3, minimum, configured),
            "Rotation prevented production with an empty active list");

    auto invalidList = active;
    std::swap(invalidList[0], invalidList[1]);
    block.producerProof.reset();
    block.producer = *rules::ScheduledProducer(block.slot, invalidList, configured);
    Require(!rules::VerifyRules(block, 100, {}, invalidList, context2, context3, minimum, configured),
            "Unsorted active list changed the producer schedule");
    invalidList = active;
    invalidList.back() = invalidList.front();
    Require(!rules::VerifyRules(block, 100, {}, invalidList, context2, context3, minimum, configured),
            "Duplicate active key created extra producer slots");
    invalidList = active;
    invalidList.push_back(outsider);
    Require(!rules::ScheduledProducer(101, invalidList, configured) &&
            !rules::CanProduce(outsider.publicKey, 101, 100, 2, invalidList, configured),
            "Oversized active list entered the schedule");

    auto largest = configured;
    largest.slotMs = 1;
    largest.startTimeMs = UINT64_MAX - 1;
    Require(rules::ScheduledProducer(UINT64_MAX, active, largest) == active[1].publicKey,
            "Rotation near maximum slot overflowed");
    Require(rules::CanProduce(active[1].publicKey, UINT64_MAX, UINT64_MAX - 1, 2, active, largest),
            "Largest valid slot was rejected");
    Require(!rules::CanProduce(active[0].publicKey, UINT64_MAX - 2, 0, 2, active, largest),
            "Rotation before genesis underflowed");
    largest.enabled = false;
    Require(!rules::ScheduledProducer(UINT64_MAX, active, largest), "Disabled VDF retained its schedule");
}

void TestEncoding(const election::Candidate& record, const election::SecretKey& key) {
    rules::Extension extension;
    extension.slot = 0x0102030405060708ULL;
    extension.producer = record.publicKey;
    extension.producerProof = record;
    const auto headerHash = NumberHash(55);
    rules::Sign(extension, headerHash, key);
    Require(rules::VerifySignature(extension, headerHash), "Block signature rejected");
    Require(!rules::VerifySignature(extension, NumberHash(56)), "Block signature reused for another header");
    Require(extension.signature == rules::SignHash(headerHash, key), "Signing helpers use different messages");
    const auto bytes = rules::Encode(extension);
    rules::Extension decoded;
    Require(rules::Parse(bytes, decoded) && decoded == extension, "VDF extension roundtrip failed");
    const auto saved = decoded;
    for (const std::size_t offset : {std::size_t{0}, std::size_t{4}, std::size_t{8}, std::size_t{9},
                                    std::size_t{50}, std::size_t{51}}) {
        auto corrupt = bytes;
        corrupt[offset] ^= 0x80;
        Require(!rules::Parse(corrupt, decoded), "Malformed extension frame accepted");
        Require(decoded == saved, "Failed parsing modified output");
    }
    Require(!rules::Parse(std::span(bytes).first(bytes.size() - 1), decoded), "Short extension accepted");
    auto extra = bytes;
    extra.push_back(0);
    Require(!rules::Parse(extra, decoded), "Extension trailing bytes accepted");
    Require(!rules::Parse({}, decoded), "Empty extension accepted");

    const auto root = rules::MerkleRoot(NumberHash(77), extension);
    auto changed = extension;
    ++changed.slot;
    Require(rules::MerkleRoot(NumberHash(77), changed) != root, "Slot is not bound to block root");
    changed = extension;
    changed.producerProof->proof[0] ^= 1;
    Require(rules::MerkleRoot(NumberHash(77), changed) != root, "Producer proof is not bound to block root");
    changed = extension;
    changed.signature[0] ^= 1;
    Require(rules::MerkleRoot(NumberHash(77), changed) == root, "Signature creates a circular block commitment");
    Require(rules::MerkleRoot(NumberHash(78), extension) != root, "Transaction root is not bound to block root");
    extension.candidates.assign(5, record);
    Require(rules::Encode(extension).size() == rules::kMaxEncodedBytes - rules::kTurnEncodedBytes,
            "Legacy maximum extension size changed");
    Require(rules::Parse(rules::Encode(extension), decoded), "Maximum-size extension cannot be decoded");
    const auto legacyMaximum = rules::Encode(extension);
    extension.turn = rules::ProducerTurn{17, 31};
    const auto turnBytes = rules::Encode(extension);
    Require(turnBytes.size() == rules::kMaxEncodedBytes && turnBytes[8] == 2 && turnBytes[9] == 0,
            "Producer turn did not use extension version 2");
    Require(rules::Parse(turnBytes, decoded) && decoded == extension, "Producer turn roundtrip failed");
    Require(rules::detail::Read64(turnBytes.data() + 52) == 17 &&
            rules::detail::Read64(turnBytes.data() + 60) == 31, "Producer turn field offsets changed");
    Require(std::equal(legacyMaximum.begin() + 52, legacyMaximum.end(), turnBytes.begin() + 68),
            "Producer turn changed legacy candidate or signature encoding");
    const auto turnRoot = rules::MerkleRoot(NumberHash(77), extension);
    changed = extension;
    ++changed.turn->number;
    Require(rules::MerkleRoot(NumberHash(77), changed) != turnRoot, "Turn number is not bound to block root");
    changed = extension;
    ++changed.turn->blocks;
    Require(rules::MerkleRoot(NumberHash(77), changed) != turnRoot, "Turn block count is not bound to block root");
    auto downgrade = turnBytes;
    downgrade[8] = 1;
    Require(!rules::Parse(downgrade, decoded), "Turn bytes accepted with legacy version");
    extension.candidates.push_back(record);
    try {
        (void)rules::Encode(extension);
        throw std::runtime_error("Six candidates encoded");
    } catch (const std::invalid_argument&) {
    }
}

void TestConsecutiveTurns(rules::Config configured, const std::vector<election::Candidate>& active,
    const std::vector<election::Candidate>& next, const election::Candidate& outsider,
    const election::Candidate& bootstrap, const election::Context& context0,
    const election::Context& context2, const election::Context& context3) {
    configured.producerRotation = true;
    const auto oldBytes = rules::EncodeConfig(configured);
    auto legacyTimeout = configured;
    legacyTimeout.producerTimeoutSlots = 333;
    Require(rules::EncodeConfig(legacyTimeout) == oldBytes, "Unused timeout changed legacy genesis bytes");
    configured.consecutiveBlocks = 32;
    configured.producerTimeoutSlots = 8;
    configured.epochBlocks = 160;
    const auto configBytes = rules::EncodeConfig(configured);
    Require(configBytes.size() == 96 && rules::detail::Read64(configBytes.data()) == 3 &&
            rules::detail::Read64(configBytes.data() + 72) == 1 &&
            rules::detail::Read64(configBytes.data() + 80) == 32 &&
            rules::detail::Read64(configBytes.data() + 88) == 8, "Consecutive configuration encoding changed");
    for (const auto& invalid : std::vector<rules::Config>{
            [&] { auto value = configured; value.producerRotation = false; return value; }(),
            [&] { auto value = configured; value.epochBlocks = 32; return value; }(),
            [&] { auto value = configured; value.consecutiveBlocks = 820; return value; }(),
            [&] { auto value = configured; value.producerTimeoutSlots = 2; return value; }(),
            [&] { auto value = configured; value.producerTimeoutSlots = 65537; return value; }()}) {
        try {
            rules::ValidateConfig(invalid);
            throw std::runtime_error("Invalid consecutive configuration accepted");
        } catch (const std::invalid_argument&) {}
    }
    Require(!rules::ScheduledProducer(101, active, configured) &&
            !rules::CanProduce(active[0].publicKey, 101, 100, 2, active, configured),
            "Legacy producer helper authorized consecutive production");
    std::optional<rules::ProducerTurn> parentTurn;
    std::uint64_t parentSlot = 100;
    for (std::uint64_t i = 0; i < 160; ++i) {
        const bool sameEpoch = i != 0;
        const auto slot = parentSlot + 1;
        const auto turn = rules::ScheduleTurn(slot, parentSlot, parentTurn, sameEpoch, configured);
        Require(turn == rules::ProducerTurn{i / 32, i % 32 + 1}, "Producer did not retain 32 consecutive blocks");
        const auto expected = active[(i / 32) % active.size()].publicKey;
        Require(rules::ScheduledTurnProducer(slot, parentSlot, parentTurn, sameEpoch, active, configured) == expected,
                "Consecutive producer does not follow elected order");
        rules::Extension block;
        block.slot = slot;
        block.producer = expected;
        block.turn = turn;
        Require(rules::VerifyRules(block, parentSlot, {}, active, context2, context3, 1,
                configured, parentTurn, sameEpoch), "Consecutive member block rejected");
        for (const auto& other : active) {
            if (other.publicKey == expected) continue;
            block.producer = other.publicKey;
            block.producerProof = other;
            Require(!rules::VerifyRules(block, parentSlot, {}, active, context2, context3, 1,
                    configured, parentTurn, sameEpoch), "Wrong producer bypassed consecutive turn with a proof");
        }
        block.producer = expected;
        block.producerProof.reset();
        ++block.turn->blocks;
        Require(!rules::VerifyRules(block, parentSlot, {}, active, context2, context3, 1,
                configured, parentTurn, sameEpoch), "Forged consecutive block count accepted");
        block.turn = turn;
        const auto encoded = rules::Encode(block);
        rules::Extension restarted;
        Require(rules::Parse(encoded, restarted) && restarted.turn == block.turn,
                "Stored parent turn cannot be recovered after restart");
        parentTurn = restarted.turn;
        parentSlot = slot;
    }
    Require(rules::ScheduleTurn(parentSlot + 1, parentSlot, parentTurn, false, configured) == rules::ProducerTurn{0, 1},
            "New epoch did not restart from the first elected producer");
    rules::Extension nextBlock;
    nextBlock.slot = parentSlot + 1;
    nextBlock.producer = next.front().publicKey;
    nextBlock.turn = rules::ProducerTurn{0, 1};
    Require(rules::VerifyRules(nextBlock, parentSlot, {}, std::span(next).first(5), context3, std::nullopt,
            1, configured, parentTurn, false), "New epoch did not use its own elected list");

    parentSlot = 100;
    parentTurn = rules::ProducerTurn{0, 7};
    const auto verify = [&](const rules::Extension& block, const std::optional<rules::ProducerTurn>& state = rules::ProducerTurn{0, 7}) {
        return rules::VerifyRules(block, parentSlot, {}, active, context2, context3, 1, configured, state, true);
    };
    for (std::uint64_t missed = 0; missed < 3; ++missed) {
        const auto slot = parentSlot + missed * 8 + 1;
        const auto expectedTurn = rules::ProducerTurn{missed, missed ? 1ULL : 8ULL};
        Require(rules::ScheduleTurn(slot, parentSlot, parentTurn, true, configured) == expectedTurn,
                "Multiple offline producers did not advance after each eight empty slots");
        rules::Extension block;
        block.slot = slot;
        block.producer = active[missed].publicKey;
        block.turn = expectedTurn;
        Require(verify(block), "Timeout successor block rejected");
        if (missed) {
            block.producer = active[0].publicKey;
            block.producerProof = active[0];
            Require(!verify(block), "Old leader remained authorized after timeout");
        }
    }
    Require(rules::ScheduleTurn(108, 100, parentTurn, true, configured) == rules::ProducerTurn{0, 8} &&
            rules::ScheduleTurn(109, 100, parentTurn, true, configured) == rules::ProducerTurn{1, 1},
            "Timeout boundary did not wait eight complete empty slots");
    rules::Extension block;
    block.slot = 101;
    block.producer = active[0].publicKey;
    block.turn = rules::ProducerTurn{0, 8};
    Require(!verify(block, std::nullopt), "Missing same-epoch parent turn accepted");
    Require(!verify(block, rules::ProducerTurn{0, 33}), "Invalid parent block count accepted");
    block.turn.reset();
    Require(!verify(block), "Consecutive block without a turn accepted");
    block.turn = rules::ProducerTurn{0, 8};
    auto legacy = configured;
    legacy.consecutiveBlocks = 0;
    Require(!rules::VerifyRules(block, 100, {}, active, context2, context3, 1, legacy),
            "Consecutive extension accepted by legacy chain");

    Require(rules::RecoverySlotGap(active.size(), configured) == 41, "Recovery did not allow five producer timeouts");
    block.producer = outsider.publicKey;
    block.producerProof = outsider;
    block.slot = 140;
    block.turn = rules::ProducerTurn{4, 0};
    Require(!verify(block), "Outsider recovered before all elected producers timed out");
    block.slot = 141;
    block.turn = rules::ProducerTurn{5, 0};
    Require(verify(block), "Outsider recovery after all timeouts rejected");
    block.turn->blocks = 1;
    Require(!verify(block), "Outsider forged a member's completed block count");
    Require(rules::ScheduleTurn(142, 141, rules::ProducerTurn{5, 0}, true, configured) == rules::ProducerTurn{5, 1},
            "Member did not resume from recovery turn");

    block.slot = 101;
    block.producer = bootstrap.publicKey;
    block.producerProof = bootstrap;
    block.turn = rules::ProducerTurn{};
    Require(rules::VerifyRules(block, 100, {}, {}, context0, std::nullopt, 1, configured),
            "Consecutive bootstrap proof rejected");
    block.turn->blocks = 1;
    Require(!rules::VerifyRules(block, 100, {}, {}, context0, std::nullopt, 1, configured),
            "Bootstrap claimed an elected producer count");
    block.turn = rules::ProducerTurn{};
    block.producer = outsider.publicKey;
    block.producerProof = outsider;
    Require(rules::VerifyRules(block, 100, {}, {}, context2, context3, 1, configured),
            "Empty elected list could not advance with proof");
    block.producerProof.reset();
    Require(!rules::VerifyRules(block, 100, {}, {}, context2, context3, 1, configured),
            "Open production accepted a missing proof");

    for (const auto& [slot, previous, turn] : std::vector<std::tuple<std::uint64_t, std::uint64_t, rules::ProducerTurn>>{
            {101, 100, {UINT64_MAX, 32}}, {109, 100, {UINT64_MAX, 1}}, {100, 100, {0, 1}}}) {
        try {
            (void)rules::ScheduleTurn(slot, previous, turn, true, configured);
            throw std::runtime_error("Invalid or overflowing producer turn accepted");
        } catch (const std::invalid_argument&) {}
    }
    Require(rules::ScheduleTurn(101, 100, rules::ProducerTurn{UINT64_MAX, 31}, true, configured) ==
            rules::ProducerTurn{UINT64_MAX, 32}, "Largest valid producer turn rejected");
}

void TestRules() {
    rules::Config configured;
    configured.enabled = true;
    configured.epochBlocks = 2;
    configured.iterations = 4096;
    configured.startTimeMs = 50;
    rules::currentTimeMs = [] { return std::uint64_t{25000}; };
    const auto chainId = NumberHash(100), genesisHash = NumberHash(1);
    rules::ReadHash readHash = [](std::uint64_t height) -> std::optional<election::Digest> { return NumberHash(height); };
    const auto context0 = rules::ContextForEpoch(configured, chainId, genesisHash, 0, readHash);
    const auto context1 = rules::ContextForEpoch(configured, chainId, genesisHash, 1, readHash);
    const auto context2 = rules::ContextForEpoch(configured, chainId, genesisHash, 2, readHash);
    const auto context3 = rules::ContextForEpoch(configured, chainId, genesisHash, 3, readHash);
    Require(rules::Epoch(2, configured) == 0 && rules::Epoch(3, configured) == 0 &&
            rules::Epoch(4, configured) == 1 && rules::EpochStart(2, configured) == 6, "Epoch boundary mismatch");
    Require(rules::StartSlot(configured) == 1 && rules::MinimumSlot(2, configured) == 1 &&
            rules::MinimumSlot(6, configured) == 5, "Genesis start time does not bound block height");
    auto largest = configured;
    largest.slotMs = 1;
    largest.startTimeMs = UINT64_MAX;
    Require(rules::MinimumSlot(2, largest) == UINT64_MAX, "Largest minimum slot changed");
    try {
        (void)rules::MinimumSlot(3, largest);
        throw std::runtime_error("Minimum slot overflow accepted");
    } catch (const std::invalid_argument&) {
    }
    Require(context0.anchor == context1.anchor && context2.anchor != context3.anchor, "Wrong bootstrap or epoch anchor");
    const auto forkContext = rules::ContextForEpoch(configured, chainId, genesisHash, 2,
        [](std::uint64_t height) -> std::optional<election::Digest> { return NumberHash(height == 3 ? 300 : height); });
    Require(forkContext.anchor != context2.anchor, "Fork anchor did not change");
    try {
        (void)rules::ContextForEpoch(configured, chainId, genesisHash, 2,
            [](std::uint64_t) -> std::optional<election::Digest> { return std::nullopt; });
        throw std::runtime_error("Missing anchor block accepted");
    } catch (const std::invalid_argument&) {
    }
    std::vector<election::Candidate> current, next;
    for (std::uint8_t key = 1; key <= 6; ++key) {
        current.push_back(election::CreateCandidate(context2, MakeKey(key)));
        next.push_back(election::CreateCandidate(context3, MakeKey(key)));
    }
    std::sort(current.begin(), current.end(), rules::CandidateLess);
    std::sort(next.begin(), next.end(), rules::CandidateLess);
    const std::vector<election::Candidate> active(current.begin(), current.begin() + 5);
    const auto outsider = current.back();
    const auto boot0 = election::CreateCandidate(context0, MakeKey(1));
    const auto boot1 = election::CreateCandidate(context1, MakeKey(1));
    TestEncoding(boot0, MakeKey(1));
    TestRotation(configured, active, next, outsider, boot0, context0, context2, context3);
    TestConsecutiveTurns(configured, active, next, outsider, boot0, context0, context2, context3);

    rules::Extension block;
    block.slot = 101;
    block.producer = boot0.publicKey;
    block.producerProof = boot0;
    const auto minimum0 = rules::MinimumSlot(2, configured);
    const auto minimum1 = rules::MinimumSlot(4, configured);
    const auto minimum2 = rules::MinimumSlot(6, configured);
    Require(rules::VerifyRules(block, 100, {}, {}, context0, std::nullopt, minimum0, configured), "Bootstrap proof rejected");
    Require(!rules::VerifyRules(block, 100, {}, {}, context0, std::nullopt, 102, configured),
            "Slot below height minimum accepted");
    Require(rules::VerifyRules(block, 100, {}, {}, context0, std::nullopt, 101, configured),
            "Slot at height minimum rejected");
    Require(!rules::VerifyRules(block, 100, {}, {}, context0, std::nullopt, 0, configured),
            "Minimum slot before genesis accepted");
    block.producerProof.reset();
    Require(!rules::VerifyRules(block, 100, {}, {}, context0, std::nullopt, minimum0, configured), "Bootstrap without proof accepted");
    block.producerProof = boot1;
    Require(rules::VerifyRules(block, 100, {}, {}, context1, context2, minimum1, configured), "Second bootstrap epoch rejected");
    block.producerProof = boot0;
    Require(!rules::VerifyRules(block, 100, {}, {}, context1, context2, minimum1, configured), "Old bootstrap proof replayed");

    block.producer = active.front().publicKey;
    block.producerProof.reset();
    Require(rules::VerifyRules(block, 100, {}, active, context2, context3, minimum2, configured), "Elected producer rejected");
    for (const auto& member : active) {
        block.producer = member.publicKey;
        Require(rules::VerifyRules(block, 100, {}, active, context2, context3, minimum2, configured),
                "Existing VDF chain stopped allowing all members in one slot");
    }
    block.producer = outsider.publicKey;
    Require(!rules::VerifyRules(block, 100, {}, active, context2, context3, minimum2, configured), "Nonmember produced without proof");
    block.producerProof = outsider;
    block.slot = 119;
    Require(!rules::VerifyRules(block, 100, {}, active, context2, context3, minimum2, configured), "Recovery allowed before slot gap");
    block.slot = 120;
    Require(rules::VerifyRules(block, 100, {}, active, context2, context3, minimum2, configured), "Recovery at slot gap rejected");
    Require(!rules::VerifyRules(block, 120, {}, active, context2, context3, minimum2, configured), "Duplicate slot accepted");
    Require(!rules::VerifyRules(block, 100, {}, active, forkContext, context3, minimum2, configured), "Old branch proof reused after reorganization");
    block.slot = 101;
    Require(rules::VerifyRules(block, 100, {}, {}, context2, context3, minimum2, configured), "Empty election could not advance with valid proof");
    block.producerProof.reset();
    Require(!rules::VerifyRules(block, 100, {}, {}, context2, context3, minimum2, configured), "Empty election advanced without proof");
    block.producer = active.front().publicKey;
    block.slot = 502;
    Require(rules::VerifyRules(block, 100, {}, active, context2, context3, minimum2, configured), "Allowed future tolerance rejected");
    block.slot = 503;
    Require(!rules::VerifyRules(block, 100, {}, active, context2, context3, minimum2, configured), "Future slot bypassed recovery delay");

    block.slot = 101;
    std::vector<election::Candidate> previous(next.begin() + 1, next.end());
    block.candidates.assign(next.begin(), next.begin() + 5);
    Require(rules::VerifyRules(block, 100, previous, active, context2, context3, minimum2, configured), "Improved top five rejected");
    previous = block.candidates;
    block.candidates.assign(next.begin() + 1, next.end());
    Require(!rules::VerifyRules(block, 100, previous, active, context2, context3, minimum2, configured), "Better parent candidate omitted");
    block.candidates = previous;
    block.candidates.pop_back();
    Require(!rules::VerifyRules(block, 100, previous, active, context2, context3, minimum2, configured), "Parent candidate silently deleted");
    block.candidates = previous;
    std::swap(block.candidates[0], block.candidates[1]);
    Require(!rules::VerifyRules(block, 100, previous, active, context2, context3, minimum2, configured), "Unsorted candidate set accepted");
    block.candidates = previous;
    block.candidates.back() = block.candidates.front();
    Require(!rules::VerifyRules(block, 100, {}, active, context2, context3, minimum2, configured), "Duplicate public key accepted");

    rules::ClearProofCache();
    Require(rules::VerifyProof(outsider, context2), "Valid proof cache insertion failed");
    const auto checks = rules::proofChecks.load();
    Require(rules::VerifyProof(outsider, context2) && rules::proofChecks.load() == checks, "Valid proof was not reused");
    Require(!rules::VerifyProof(outsider, forkContext), "Proof cache bypassed context binding");
    auto invalid = outsider;
    invalid.signature[0] ^= 1;
    Require(!rules::VerifyProof(invalid, context2), "Invalid proof accepted");
    const auto failedChecks = rules::proofChecks.load();
    Require(!rules::VerifyProof(invalid, context2) && rules::proofChecks.load() == failedChecks + 1,
            "Failed proof entered the successful-proof cache");
}

} // namespace

int main() {
    try {
        Require(sodium_init() >= 0, "libsodium initialization failed");
        TestGenesis();
        TestRules();
        std::cout << "VdfRulesTest passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "VdfRulesTest failed: " << error.what() << '\n';
        return 1;
    }
}
