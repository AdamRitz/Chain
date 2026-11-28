#include "../Crypto/WorkElection.h"

#include <atomic>
#include <iostream>
#include <thread>

namespace {

using namespace chain::work_election;

void Require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

SecretKey MakeKey(std::uint8_t byte) {
    std::array<std::uint8_t, crypto_sign_SEEDBYTES> seed{};
    seed.fill(byte);
    PublicKey publicKey{}; SecretKey key{};
    Require(crypto_sign_seed_keypair(publicKey.data(), key.data(), seed.data()) == 0, "Key generation failed");
    return key;
}

PublicKey Public(const SecretKey& key) {
    PublicKey publicKey{};
    crypto_sign_ed25519_sk_to_pk(publicKey.data(), key.data());
    return publicKey;
}

Digest Anchor(std::uint64_t round) {
    Digest digest{};
    chain::vdf_election::detail::Write64(std::span(digest).first<8>(), round);
    digest.back() = 17;
    return digest;
}

void TestBeacon(const SecretKey& key) {
    BeaconContext context; context.chainId[0] = 7; context.targetEpoch = 21; context.anchor[0] = 9;
    Digest value{}; value.fill(41);
    const auto commit = CreateCommit(context, key, value);
    const auto reveal = CreateReveal(context, key, value);
    Require(VerifyCommit(commit, context), "Valid commitment rejected");
    Require(VerifyReveal(reveal, context, commit), "Valid reveal rejected");
    Commit decodedCommit; Reveal decodedReveal;
    Require(EncodeCommit(commit).size() == kBeaconBytes, "Commit width changed");
    Require(DecodeCommit(EncodeCommit(commit), decodedCommit) && decodedCommit == commit, "Commit roundtrip failed");
    Require(DecodeReveal(EncodeReveal(reveal), decodedReveal) && decodedReveal == reveal, "Reveal roundtrip failed");
    Require(!DecodeCommit(EncodeReveal(reveal), decodedCommit), "Reveal decoded as commit");

    auto changed = reveal; changed.value[4] ^= 1;
    Require(!VerifyReveal(changed, context, commit), "Changed revealed value accepted");
    SignReveal(changed, key);
    Require(!VerifyReveal(changed, context, commit), "Signed value not matching commitment accepted");
    changed = reveal; changed.signature[0] ^= 1;
    Require(!VerifyReveal(changed, context, commit), "Modified reveal signature accepted");
    auto changedCommit = commit; changedCommit.commitment[0] ^= 1;
    Require(!VerifyCommit(changedCommit, context), "Modified commitment accepted");
    SignCommit(changedCommit, key);
    Require(!VerifyReveal(reveal, context, changedCommit), "Reveal used another commitment");
    auto expected = context; ++expected.targetEpoch;
    Require(!VerifyCommit(commit, expected) && !VerifyReveal(reveal, expected, commit), "Cross-epoch beacon accepted");
    expected = context; expected.anchor[0] ^= 1;
    Require(!VerifyCommit(commit, expected), "Cross-branch commitment accepted");
    expected = context; expected.chainId[0] ^= 1;
    Require(!VerifyReveal(reveal, expected, commit), "Cross-chain reveal accepted");
    const auto another = CreateReveal(context, MakeKey(8), value);
    Require(!VerifyReveal(another, context, commit), "Another public key reused commitment");
    auto encoded = EncodeCommit(commit); encoded.push_back(0);
    Require(!DecodeCommit(encoded, decodedCommit), "Trailing commit bytes accepted");
}

void TestSampling(const SecretKey& key) {
    Context context; context.iterations = chain::vdf_election::kMinIterations;
    const auto publicKey = Public(key);
    for (std::uint64_t rounds = 0; rounds < 100; ++rounds) {
        context.matureRounds = rounds;
        for (std::uint8_t seed = 0; seed < 5; ++seed) {
            context.randomSeed[0] = seed;
            const auto groups = SampleGroups(context, publicKey);
            Require(groups == SampleGroups(context, publicKey), "Sampling is not deterministic");
            std::uint64_t previousEnd = 0; std::size_t count = 0;
            for (const auto& group : groups) {
                Require(group.start >= previousEnd && group.count > 0 && group.count <= 8,
                        "Sample groups overlap or have wrong size");
                Require(group.start < rounds && group.count <= rounds - group.start, "Sample exceeds mature history");
                previousEnd = group.start + group.count; count += group.count;
            }
            Require(count == std::min<std::uint64_t>(32, rounds), "Wrong number of sampled rounds");
            if (rounds >= 32) Require(groups.size() == 4, "Wrong full group count");
            if (rounds < 32 && rounds != 0)
                Require(groups.front().start == 0 && previousEnd == rounds, "Short history is not fully covered");
        }
    }
    context.matureRounds = std::numeric_limits<std::uint64_t>::max();
    context.groups = 4; context.groupSize = 1;
    const auto extreme = SampleGroups(context, publicKey);
    Require(extreme.size() == 4, "Large history sampling failed");
    for (std::size_t i = 0; i < extreme.size(); ++i) {
        Require(extreme[i].start < context.matureRounds, "Sample arithmetic overflow");
        if (i) Require(extreme[i - 1].start < extreme[i].start, "Duplicate sample");
    }
}

void TestTicket(const SecretKey& key) {
    Context context; context.chainId[0] = 3; context.targetEpoch = 9; context.anchor[0] = 33;
    context.randomSeed[0] = 64; context.matureRounds = 4;
    context.iterations = chain::vdf_election::kMinIterations; context.groups = 2; context.groupSize = 2;
    const auto publicKey = Public(key);
    std::vector<Work> history;
    Output previous{};
    for (std::uint64_t round = 0; round < context.matureRounds; ++round) {
        // Starting another independent segment is allowed. Only links inside
        // a sampled group are required, not an unbroken chain from genesis.
        if (round == 2) previous.fill(123);
        history.push_back(CreateWork(context.chainId, publicKey, round, Anchor(round), previous, context.iterations));
        previous = WorkOutput(history.back());
    }
    const WorkLookup workLookup = [&](std::uint64_t round, Work& work) {
        if (round >= history.size()) return false;
        work = history[round]; return true;
    };
    const AnchorLookup anchors = [](std::uint64_t round, Digest& digest) { digest = Anchor(round); return true; };
    const auto ticket = CreateTicket(context, key, workLookup);
    VerificationCache cache(2);
    Digest score{};
    Require(VerifyTicket(ticket, context, anchors, &score, &cache), "Valid grouped ticket rejected");
    Require(score == DeriveScore(context, publicKey), "Ticket score mismatch");
    Require(cache.Size() == 1, "Valid ticket not cached");
    Require(VerifyTicket(ticket, context, anchors, nullptr, &cache), "Cached ticket rejected");

    unsigned reads = 0;
    const AnchorLookup forked = [&](std::uint64_t round, Digest& digest) {
        ++reads; digest = Anchor(round); if (round == 2) digest[0] ^= 1; return true;
    };
    Require(!VerifyTicket(ticket, context, forked, nullptr, &cache) && reads > 0,
            "Cache reused ticket from different historical branch");
    const AnchorLookup unavailable = [](std::uint64_t, Digest&) { return false; };
    Require(!VerifyTicket(ticket, context, unavailable, nullptr, &cache), "Missing historical anchor accepted");

    auto modified = ticket; modified.works[1].previousOutput[7] ^= 1; SignTicket(modified, key);
    Require(!VerifyTicket(modified, context, anchors, nullptr, &cache), "Disconnected Work inside group accepted");
    modified = ticket; modified.works[1] = modified.works[0]; SignTicket(modified, key);
    Require(!VerifyTicket(modified, context, anchors), "Duplicate sampled Work accepted");
    modified = ticket; modified.works[0].proof.back() ^= 1;
    Require(!VerifyTicket(modified, context, anchors, nullptr, &cache), "Modified proof reused cached result");
    SignTicket(modified, key);
    Require(!VerifyTicket(modified, context, anchors, nullptr, &cache), "Signed invalid VDF accepted");
    modified = ticket; modified.signature[0] ^= 1;
    Require(!VerifyTicket(modified, context, anchors, nullptr, &cache), "Signature not bound to cache key");
    modified = ticket; modified.publicKey = Public(MakeKey(91)); SignTicket(modified, MakeKey(91));
    Require(!VerifyTicket(modified, context, anchors), "VDF work reused under another public key");
    modified = ticket; modified.works[0].anchor[0] ^= 1; SignTicket(modified, key);
    Require(!VerifyTicket(modified, context, anchors), "Peer-supplied historical anchor trusted");

    auto expected = context; ++expected.targetEpoch;
    Require(!VerifyTicket(ticket, expected, anchors, nullptr, &cache), "Context not rechecked before cache");
    expected = context; ++expected.iterations;
    Require(!VerifyTicket(ticket, expected, anchors), "Wrong VDF work difficulty accepted");
    expected = context; expected.chainId[0] ^= 1;
    Require(!VerifyTicket(ticket, expected, anchors), "Wrong chain accepted");

    Ticket decoded;
    auto bytes = EncodeTicket(ticket);
    Require(bytes.size() == kTicketHeaderBytes + 4 * kWorkBytes + crypto_sign_BYTES, "Ticket length mismatch");
    Require(DecodeTicket(bytes, decoded) && decoded == ticket, "Ticket codec roundtrip failed");
    Require(VerifyTicket(decoded, context, anchors), "Decoded ticket rejected");
    const auto saved = decoded;
    bytes.push_back(0);
    Require(!DecodeTicket(bytes, decoded) && decoded == saved, "Trailing bytes changed decoded output");
    bytes = EncodeTicket(ticket); bytes[4] = 2;
    Require(!DecodeTicket(bytes, decoded), "Wrong ticket version accepted");
    bytes = EncodeTicket(ticket); bytes.resize(kTicketHeaderBytes - 1);
    Require(!DecodeTicket(bytes, decoded), "Truncated ticket accepted");

    Work decodedWork;
    Require(EncodeWork(history[0]).size() == kEncodedWorkBytes, "Stored Work length changed");
    Require(DecodeWork(EncodeWork(history[0]), decodedWork) && decodedWork == history[0], "Work codec failed");
    Require(VerifyWork(context.chainId, publicKey, decodedWork, context.iterations), "Stored Work verification failed");
    auto wrongAnchor = Anchor(0); wrongAnchor[0] ^= 1;
    Require(!VerifyWork(decodedWork, context.chainId, publicKey, wrongAnchor, context.iterations), "Wrong expected anchor accepted");

    // The default four-by-eight scheme checks all available history at startup.
    Context shortContext = context; shortContext.groups = 4; shortContext.groupSize = 8; shortContext.matureRounds = 2;
    const auto shortTicket = CreateTicket(shortContext, key, workLookup);
    Require(shortTicket.works.size() == 2 && VerifyTicket(shortTicket, shortContext, anchors), "Short history ticket rejected");
    Context empty = shortContext; empty.matureRounds = 0;
    Ticket bootstrap; bootstrap.context = empty; bootstrap.publicKey = publicKey; SignTicket(bootstrap, key);
    Require(!VerifyTicket(bootstrap, empty, anchors), "Bootstrap admitted zero-Work ticket");
    bool threw = false;
    try { (void)CreateTicket(empty, key, workLookup); } catch (const std::invalid_argument&) { threw = true; }
    Require(threw, "Created zero-Work ticket");
    Require(!DecodeTicket(EncodeTicket(bootstrap), decoded), "Decoded zero-Work ticket");
}

void TestCacheCoalescing() {
    VerificationCache cache(2); Digest key{}; key[0] = 4;
    std::promise<void> enteredPromise, releasePromise;
    auto entered = enteredPromise.get_future(); auto release = releasePromise.get_future().share();
    std::atomic<unsigned> calls{0};
    auto first = std::async(std::launch::async, [&] {
        return cache.Check(key, [&] { ++calls; enteredPromise.set_value(); release.wait(); return true; });
    });
    entered.wait();
    auto second = std::async(std::launch::async, [&] { return cache.Check(key, [&] { ++calls; return true; }); });
    releasePromise.set_value();
    Require(first.get() && second.get() && calls == 1, "Concurrent cached work not coalesced");
    for (unsigned i = 5; i < 12; ++i) {
        key[0] = static_cast<std::uint8_t>(i);
        Require(cache.Check(key, [] { return true; }), "Cache insertion failed");
        Require(cache.Size() <= 2, "Verification cache exceeded capacity");
    }
    key[0] = 90;
    Require(!cache.Check(key, [] { return false; }), "Failed proof accepted by cache");
    Require(cache.Check(key, [] { return true; }), "Failed result retained in cache");

    VerificationCache clearing(2);
    std::promise<void> startedPromise, finishPromise;
    auto started = startedPromise.get_future(); auto finish = finishPromise.get_future().share();
    auto owner = std::async(std::launch::async, [&] {
        return clearing.Check(key, [&] { startedPromise.set_value(); finish.wait(); return true; });
    });
    started.wait();
    clearing.Clear();
    Require(clearing.Size() == 0, "Clear retained cache entries");
    finishPromise.set_value();
    Require(owner.get() && clearing.Size() == 0, "Clearing interrupted or republished an in-flight result");
}

} // namespace

int main() {
    try {
        Require(sodium_init() >= 0, "libsodium initialization failed");
        const auto key = MakeKey(42);
        TestBeacon(key); TestSampling(key); TestTicket(key); TestCacheCoalescing();
        std::cout << "Work election checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Work election check failed: " << error.what() << '\n';
        return 1;
    }
}
