#include "../Crypto/VDF.h"
#include "../ThirdParty/chiavdf/src/thread_scratch.h"

#include <algorithm>
#include <chrono>
#include <future>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <vector>

namespace {

void Require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

template<std::size_t Size>
std::array<std::uint8_t, Size> FromHex(std::string_view text) {
    Require(text.size() == Size * 2, "Invalid test vector length");
    auto nibble = [](char c) -> std::uint8_t {
        if (c >= '0' && c <= '9') return static_cast<std::uint8_t>(c - '0');
        if (c >= 'a' && c <= 'f') return static_cast<std::uint8_t>(c - 'a' + 10);
        throw std::runtime_error("Invalid test vector hex");
    };
    std::array<std::uint8_t, Size> bytes{};
    for (std::size_t i = 0; i < Size; ++i) {
        bytes[i] = static_cast<std::uint8_t>((nibble(text[2 * i]) << 4) | nibble(text[2 * i + 1]));
    }
    return bytes;
}

void RequireInvalidIterations(const chain::vdf::Challenge& challenge, std::uint64_t iterations) {
    try {
        (void)chain::vdf::Prove(challenge, iterations);
    } catch (const std::invalid_argument&) {
        return;
    }
    throw std::runtime_error("Prove accepted invalid iterations");
}

#if defined(__MINGW32__)
struct ScratchFixture {
    inline static int alive = 0;
    ScratchFixture() { ++alive; }
    ~ScratchFixture() { --alive; }
};

void TestScratchLifetime() {
    {
        chiavdf_scratch_scope<ScratchFixture> outer;
        auto* saved = &chiavdf_thread_scratch<ScratchFixture>();
        Require(ScratchFixture::alive == 1, "Outer scratch was not constructed");
        try {
            chiavdf_scratch_scope<ScratchFixture> inner;
            Require(&chiavdf_thread_scratch<ScratchFixture>() != saved && ScratchFixture::alive == 2,
                    "Nested call reused the outer scratch");
            throw 17;
        } catch (int) {}
        Require(&chiavdf_thread_scratch<ScratchFixture>() == saved && ScratchFixture::alive == 1,
                "Exception did not restore the outer scratch");
    }
    Require(ScratchFixture::alive == 0, "Scratch survived its call");
    try {
        (void)chiavdf_thread_scratch<ScratchFixture>();
        throw std::runtime_error("Scratch pointer survived its call");
    } catch (const std::logic_error&) {}
}
#endif

} // namespace

int main(int argc, char** argv) {
    using namespace chain::vdf;
    try {
#if defined(__MINGW32__)
        TestScratchLifetime();
#endif
        Challenge challenge{};
        for (std::size_t i = 0; i < challenge.size(); ++i) challenge[i] = static_cast<std::uint8_t>(i);

        constexpr std::uint64_t iterations = 4096;
        const auto start = std::chrono::steady_clock::now();
        const auto proof = Prove(challenge, iterations);
        const auto proved = std::chrono::steady_clock::now();
        Require(Verify(challenge, iterations, proof), "Generated proof failed verification");
        const auto verified = std::chrono::steady_clock::now();
        Require(proof == Prove(challenge, iterations), "Proof is not deterministic");
        Require(Output(proof).size() == kOutputBytes, "Unexpected VDF output length");

        // Official chiavdf 1.2.3 src/vdf.txt, lines 85-90: independently
        // generated proof, not a round trip through this adapter's prover.
        const auto knownChallenge = FromHex<kChallengeBytes>(
            "9104c5b5e45d48f374efa0488fe6a617790e9aecb3c9cddec06809b09f45ce9b");
        const auto knownProof = FromHex<kProofBytes>(
            "03004b69a0d05b931884df11ed668b938a80236075efe218bc77a756134d218b53d"
            "993a7593f1d06928b700245c7dae1b91409138be61e6a4b5b326cefb5d2b29859e3"
            "658e3c19bcc57c97601cd488f0874c9704255369555b342168c33f2088db790100"
            "0100433c6fff6a6258faf1e102ebe6276838c4842307abe25e3c9230c2ef8718189d"
            "dbbaf4c263db881bc512596bdc04bd8fcac312dd7a1e40dcc19b859976d9742183"
            "7d101315b81fa1b2b1e2b061867f7dc1d919f2577779e59f1ae6e92fce4c230100");
        Require(Verify(knownChallenge, 2'023'424, knownProof), "Upstream proof vector failed");
        if (argc == 2 && std::string_view(argv[1]) == "--extended") {
            Require(Prove(knownChallenge, 2'023'424) == knownProof,
                    "Checkpoint prover differs from upstream proof vector");
        } else {
            Require(argc == 1, "Usage: vdf_test [--extended]");
        }

        auto changedChallenge = challenge;
        changedChallenge[0] ^= 1;
        Require(!Verify(changedChallenge, iterations, proof), "Changed challenge accepted");
        Require(!Verify(challenge, iterations - 1, proof), "Changed iterations accepted");
        Require(!Verify(challenge, iterations + 1, proof), "Changed iterations accepted");

        for (const std::size_t index : {std::size_t{0}, std::size_t{37}, kOutputBytes, kProofBytes - 1}) {
            auto changed = proof;
            changed[index] ^= 1;
            Require(!Verify(challenge, iterations, changed), "Changed proof accepted");
        }
        Require(!Verify(challenge, iterations, {}), "Empty proof accepted");
        Require(!Verify(challenge, iterations, std::span(proof).first(kProofBytes - 1)), "Short proof accepted");
        std::vector<std::uint8_t> longProof(proof.begin(), proof.end());
        longProof.push_back(0);
        Require(!Verify(challenge, iterations, longProof), "Long proof accepted");

        for (const auto count : {std::uint64_t{0}, kMaxIterations + 1, std::numeric_limits<std::uint64_t>::max()}) {
            Require(!Verify(challenge, count, proof), "Invalid iterations accepted by Verify");
            RequireInvalidIterations(challenge, count);
        }
        Proof malformed{};
        Require(!Verify(challenge, iterations, malformed), "Zero proof accepted");
        malformed.fill(0xff);
        Require(!Verify(challenge, iterations, malformed), "Invalid form accepted");

        // Tiny T has a valid identity witness. The identity's unused bytes must
        // still be zero: accepting them would allow multiple proof encodings.
        for (const std::uint64_t count : {1, 2, 55, 66, 264, 265}) {
            auto tiny = Prove(challenge, count);
            Require(Verify(challenge, count, tiny), "Small iteration proof failed");
            if (count == 1) {
                tiny[kProofBytes - 1] = 1;
                Require(!Verify(challenge, count, tiny), "Noncanonical identity padding accepted");
            }
        }

        // Fixed malformed samples exercise parsing without accepting arbitrary
        // lengths or attacker-chosen groups.
        std::uint32_t state = 0x938a2c17;
        for (int sample = 0; sample < 16; ++sample) {
            for (auto& byte : malformed) {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                byte = static_cast<std::uint8_t>(state);
            }
            Require(!Verify(challenge, iterations, malformed), "Malformed proof accepted");
        }

        // Recreate threads repeatedly to cover scratch cleanup at thread exit.
        for (int round = 0; round < 8; ++round) {
            std::array<std::future<bool>, 4> workers;
            for (auto& worker : workers) {
                worker = std::async(std::launch::async, [&] {
                    return Prove(challenge, iterations) == proof && Verify(challenge, iterations, proof);
                });
            }
            for (auto& worker : workers) Require(worker.get(), "Concurrent prove/verify failed");
        }

        std::cout << "VDF correctness passed; iterations=" << iterations
                  << " prove_ms=" << std::chrono::duration<double, std::milli>(proved - start).count()
                  << " verify_ms=" << std::chrono::duration<double, std::milli>(verified - proved).count()
                  << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "VDF correctness failed: " << error.what() << '\n';
        return 1;
    }
}
