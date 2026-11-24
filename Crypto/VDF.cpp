#include "VDF.h"

// Upstream defines functions in headers. Keep them in this translation unit.
#include "verifier.h"
#include "prover_slow.h"

namespace chain::vdf {
namespace {

static_assert(kOutputBytes == BQFC_FORM_SIZE);
static_assert(kDiscriminantBits == BQFC_MAX_D_BITS);

bool ValidIterations(std::uint64_t iterations) noexcept {
    return iterations != 0 && iterations <= kMaxIterations;
}

integer Discriminant(const Challenge& challenge) {
    std::vector<std::uint8_t> seed(challenge.begin(), challenge.end());
    return CreateDiscriminant(seed, kDiscriminantBits);
}

} // namespace

Proof Prove(const Challenge& challenge, std::uint64_t iterations) {
    if (!ValidIterations(iterations)) {
        throw std::invalid_argument("VDF iterations must be in [1, 100000000]");
    }
    auto discriminant = Discriminant(challenge);
    auto x = DeserializeForm(discriminant, DEFAULT_ELEMENT, kOutputBytes, true);
    // The ordinary prover stores intermediate forms. Above one million
    // squarings, use upstream's checkpoint implementation to bound memory.
    const auto bytes = iterations <= 1'000'000
        ? ProveSlow(discriminant, x, iterations, "")
        : ProveSlowLowMemory(discriminant, x, iterations, "");
    if (bytes.size() != kProofBytes) {
        throw std::runtime_error("VDF prover returned an invalid proof length");
    }
    Proof proof{};
    std::copy(bytes.begin(), bytes.end(), proof.begin());
    return proof;
}

bool Verify(const Challenge& challenge, std::uint64_t iterations,
            std::span<const std::uint8_t> proof) noexcept {
    if (!ValidIterations(iterations) || proof.size() != kProofBytes) {
        return false;
    }
    try {
        auto discriminant = Discriminant(challenge);
        auto x = DeserializeForm(discriminant, DEFAULT_ELEMENT, kOutputBytes, true);
        // Strict decoding checks the discriminant, reduced form and canonical
        // serialization, including zero padding for the identity and generator.
        auto y = DeserializeForm(discriminant, proof.data(), kOutputBytes, true);
        auto witness = DeserializeForm(discriminant, proof.data() + kOutputBytes, kOutputBytes, true);
        bool valid = false;
        VerifyWesolowskiProof(discriminant, x, y, witness, iterations, valid);
        return valid;
    } catch (...) {
        return false;
    }
}

} // namespace chain::vdf
