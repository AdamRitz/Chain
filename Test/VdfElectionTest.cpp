#include "../Crypto/VdfElection.h"

#include <algorithm>
#include <iostream>
#include <limits>
#include <random>
#include <stdexcept>
#include <vector>

namespace {

using namespace chain::vdf_election;

void Require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

SecretKey MakeKey(std::uint8_t value) {
    std::array<std::uint8_t, crypto_sign_SEEDBYTES> seed{};
    seed.fill(value);
    PublicKey publicKey{};
    SecretKey secretKey{};
    Require(crypto_sign_seed_keypair(publicKey.data(), secretKey.data(), seed.data()) == 0, "Key generation failed");
    return secretKey;
}

void TestBinding(const Candidate& original, const SecretKey& secretKey) {
    const auto& context = original.context;
    Digest score{};
    Require(VerifyCandidate(original, context, &score), "Valid candidate rejected");
    Require(score == DeriveScore(DeriveChallenge(context, original.publicKey), original.proof), "Score mismatch");

    auto changedContext = context;
    changedContext.chainId[0] ^= 1;
    Require(!VerifyCandidate(original, changedContext), "Cross-chain candidate accepted");
    changedContext = context;
    ++changedContext.targetEpoch;
    Require(!VerifyCandidate(original, changedContext), "Cross-epoch candidate accepted");
    changedContext = context;
    changedContext.anchor[0] ^= 1;
    Require(!VerifyCandidate(original, changedContext), "Different anchor accepted");
    changedContext = context;
    ++changedContext.iterations;
    Require(!VerifyCandidate(original, changedContext), "Different expected iterations accepted");

    // Re-signing modified fields cannot reuse a proof for the old challenge.
    for (int field = 0; field < 4; ++field) {
        auto changed = original;
        if (field == 0) changed.context.chainId[0] ^= 1;
        if (field == 1) ++changed.context.targetEpoch;
        if (field == 2) changed.context.anchor[0] ^= 1;
        if (field == 3) ++changed.context.iterations;
        Require(!VerifyCandidate(changed, changed.context), "Modified context with old signature accepted");
        SignCandidate(changed, secretKey);
        Require(!VerifyCandidate(changed, changed.context), "Proof reused after context change");
    }

    auto changed = original;
    const auto secondKey = MakeKey(99);
    crypto_sign_ed25519_sk_to_pk(changed.publicKey.data(), secondKey.data());
    Require(!VerifyCandidate(changed, context), "Modified public key accepted");
    SignCandidate(changed, secondKey);
    Require(!VerifyCandidate(changed, context), "Proof reused by another public key");
    try {
        SignCandidate(changed, secretKey);
        throw std::runtime_error("Signing with mismatched public key accepted");
    } catch (const std::invalid_argument&) {
    }

    for (const std::size_t offset : {std::size_t{0}, chain::vdf::kOutputBytes, chain::vdf::kProofBytes - 1}) {
        changed = original;
        changed.proof[offset] ^= 1;
        Require(!VerifyCandidate(changed, context), "Changed proof with old signature accepted");
        SignCandidate(changed, secretKey);
        Require(!VerifyCandidate(changed, context), "Signed invalid proof accepted");
    }
    changed = original;
    changed.signature[17] ^= 1;
    Digest untouched{};
    untouched.fill(0xa5);
    const auto before = untouched;
    Require(!VerifyCandidate(changed, context, &untouched), "Invalid signature accepted");
    Require(untouched == before, "Failed verification changed returned score");

    for (const auto iterations : {std::uint64_t{0}, std::uint64_t{1}, kMinIterations - 1,
                                  chain::vdf::kMaxIterations + 1,
                                  std::numeric_limits<std::uint64_t>::max()}) {
        changed = original;
        changed.context.iterations = iterations;
        if (iterations == kMinIterations - 1) {
            const auto challenge = DeriveChallenge(changed.context, changed.publicKey);
            changed.proof = chain::vdf::Prove(challenge, iterations);
            Require(chain::vdf::Verify(challenge, iterations, changed.proof), "Boundary VDF proof is invalid");
        }
        SignCandidate(changed, secretKey);
        Require(!VerifyCandidate(changed, changed.context), "Invalid iteration count accepted");
        Candidate decoded;
        Require(!DecodeCandidate(EncodeCandidate(changed), decoded), "Encoded invalid iteration count accepted");
        try {
            (void)CreateCandidate(changed.context, secretKey);
            throw std::runtime_error("Generated candidate with invalid iteration count");
        } catch (const std::invalid_argument&) {
        }
    }
}

void TestEncoding(const Candidate& original) {
    const auto encoded = EncodeCandidate(original);
    Require(encoded.size() == 382, "Unexpected encoded length");
    const std::array<std::uint8_t, 6> prefix{'V', 'F', 'E', 'L', 1, 0};
    const std::array<std::uint8_t, 8> epochBytes{8, 7, 6, 5, 4, 3, 2, 1};
    const std::array<std::uint8_t, 8> iterationBytes{0, 16, 0, 0, 0, 0, 0, 0};
    Require(std::equal(prefix.begin(), prefix.end(), encoded.begin()), "Wrong record prefix");
    Require(std::equal(epochBytes.begin(), epochBytes.end(), encoded.begin() + 38), "Epoch encoding is not little-endian");
    Require(std::equal(iterationBytes.begin(), iterationBytes.end(), encoded.begin() + 110), "Iteration encoding is not little-endian");
    Candidate decoded;
    Require(DecodeCandidate(encoded, decoded), "Candidate decoding failed");
    Require(decoded == original, "Candidate roundtrip changed fields");
    Require(VerifyCandidate(decoded, original.context), "Decoded candidate rejected");
    Require(EncodeCandidate(decoded) == encoded, "Encoding is not canonical");

    const auto saved = decoded;
    Require(!DecodeCandidate(std::span(encoded).first(kEncodedBytes - 1), decoded), "Short encoding accepted");
    Require(decoded == saved, "Failed decoding changed output");
    std::vector<std::uint8_t> longEncoded(encoded.begin(), encoded.end());
    longEncoded.push_back(0);
    Require(!DecodeCandidate(longEncoded, decoded), "Trailing bytes accepted");
    Require(!DecodeCandidate({}, decoded), "Empty encoding accepted");
    for (const std::size_t offset : {std::size_t{0}, std::size_t{4}, std::size_t{5}}) {
        auto changed = encoded;
        changed[offset] ^= 1;
        Require(!DecodeCandidate(changed, decoded), "Wrong magic or version accepted");
    }
    auto changed = encoded;
    std::fill_n(changed.begin() + 110, 8, 0);
    Require(!DecodeCandidate(changed, decoded), "Encoded zero iterations accepted");
    std::fill_n(changed.begin() + 110, 8, 0xff);
    Require(!DecodeCandidate(changed, decoded), "Encoded excessive iterations accepted");
    Require(decoded == saved, "Invalid encoding changed output");

    // This distinguishes syntactic decoding from cryptographic verification.
    changed = encoded;
    changed.back() ^= 1;
    Require(DecodeCandidate(changed, decoded), "Fixed-format signed record failed decoding");
    Require(!VerifyCandidate(decoded, original.context), "Decoded invalid signature accepted");
}

void TestSelection(const Context& context, const std::vector<Candidate>& candidates) {
    Require(candidates.size() > kMaxProducers, "Insufficient test candidates");
    const auto selected = SelectTopCandidates(candidates, context);
    Require(selected.size() == kMaxProducers, "Wrong producer count");
    Require(std::is_sorted(selected.begin(), selected.end(), RankLess), "Candidates are not ranked");
    for (std::size_t i = 0; i < selected.size(); ++i) {
        Require(VerifyCandidate(selected[i].candidate, context), "Invalid selected candidate");
        for (std::size_t j = 0; j < i; ++j) {
            Require(selected[i].candidate.publicKey != selected[j].candidate.publicKey, "Duplicate producer selected");
        }
    }
    std::vector<RankedCandidate> expected;
    for (const auto& candidate : candidates) {
        expected.push_back({candidate, DeriveScore(DeriveChallenge(context, candidate.publicKey), candidate.proof)});
    }
    std::sort(expected.begin(), expected.end(), RankLess);
    expected.resize(kMaxProducers);
    Require(selected == expected, "Selection omitted a better candidate");

    // An attacker can cheaply hash arbitrary Y bytes to get a better score.
    // A correct signature must not let an invalid VDF remove a valid producer.
    Candidate invalidProof;
    invalidProof.context = context;
    const auto attackerKey = MakeKey(201);
    crypto_sign_ed25519_sk_to_pk(invalidProof.publicKey.data(), attackerKey.data());
    const auto attackerChallenge = DeriveChallenge(context, invalidProof.publicKey);
    bool foundLowerScore = false;
    for (std::uint32_t nonce = 0; nonce < 4096; ++nonce) {
        for (std::size_t i = 0; i < sizeof(nonce); ++i) {
            invalidProof.proof[i] = static_cast<std::uint8_t>(nonce >> (8 * i));
        }
        if (DeriveScore(attackerChallenge, invalidProof.proof) < selected.front().score) {
            foundLowerScore = true;
            break;
        }
    }
    Require(foundLowerScore, "Failed to construct an invalid candidate with a lower score");
    SignCandidate(invalidProof, attackerKey);
    const auto invalidDigest = SigningDigest(invalidProof);
    Require(crypto_sign_verify_detached(invalidProof.signature.data(), invalidDigest.data(), invalidDigest.size(),
                                        invalidProof.publicKey.data()) == 0, "Attack test signature is invalid");
    Require(!VerifyCandidate(invalidProof, context), "Signed invalid VDF accepted");
    auto attacked = candidates;
    attacked.push_back(invalidProof);
    Require(SelectTopCandidates(attacked, context) == selected, "Invalid low score removed a valid producer");

    auto duplicated = candidates;
    auto invalidDuplicate = selected.front().candidate;
    invalidDuplicate.signature[0] ^= 1;
    duplicated.insert(duplicated.begin(), invalidDuplicate);
    duplicated.insert(duplicated.end(), candidates.begin(), candidates.end());
    auto crossEpoch = candidates.front();
    ++crossEpoch.context.targetEpoch;
    duplicated.push_back(crossEpoch);
    duplicated.insert(duplicated.begin(), invalidProof);
    duplicated.push_back(invalidProof);
    Require(SelectTopCandidates(duplicated, context) == selected, "Duplicate or invalid record changed selection");

    std::mt19937 random(0x4a31);
    for (int round = 0; round < 4; ++round) {
        std::shuffle(duplicated.begin(), duplicated.end(), random);
        Require(SelectTopCandidates(duplicated, context) == selected, "Input order changed selection");
    }
    Require(SelectTopCandidates(candidates, context, 0).empty(), "Zero limit returned records");
    Require(SelectTopCandidates({}, context).empty(), "Empty set returned records");
    const auto topTwo = SelectTopCandidates(candidates, context, 2);
    Require(topTwo.size() == 2 && std::equal(topTwo.begin(), topTwo.end(), selected.begin()), "Smaller limit changed ranking");
    Require(SelectTopCandidates(std::vector<Candidate>{candidates.front(), candidates.front()}, context).size() == 1,
            "Duplicate key occupied multiple positions");
    try {
        (void)SelectTopCandidates(candidates, context, kMaxProducers + 1);
        throw std::runtime_error("Excessive producer limit accepted");
    } catch (const std::invalid_argument&) {
    }
}

} // namespace

int main() {
    try {
        Require(sodium_init() >= 0, "libsodium initialization failed");
        Context context;
        for (std::size_t i = 0; i < context.chainId.size(); ++i) {
            context.chainId[i] = static_cast<std::uint8_t>(i + 1);
            context.anchor[i] = static_cast<std::uint8_t>(255 - i);
        }
        context.targetEpoch = 0x0102030405060708ULL;
        // Use enough squarings for the group discriminant to affect the output.
        // Production chooses iterations after timing the deployment hardware.
        context.iterations = 4096;
        std::vector<Candidate> candidates;
        for (std::uint8_t key = 1; key <= 7; ++key) candidates.push_back(CreateCandidate(context, MakeKey(key)));
        TestBinding(candidates.front(), MakeKey(1));
        TestEncoding(candidates.front());
        TestSelection(context, candidates);
        std::cout << "VdfElectionTest passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "VdfElectionTest failed: " << error.what() << '\n';
        return 1;
    }
}
