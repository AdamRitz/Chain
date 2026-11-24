#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>

namespace chain::vdf {

inline constexpr std::size_t kChallengeBytes = 32;
inline constexpr int kDiscriminantBits = 1024;
inline constexpr std::size_t kOutputBytes = 100;
inline constexpr std::size_t kProofBytes = 2 * kOutputBytes;
inline constexpr std::uint64_t kMaxIterations = 100'000'000;

using Challenge = std::array<std::uint8_t, kChallengeBytes>;
// Canonical chiavdf encoding: Y (100 bytes), followed by its proof (100 bytes).
using Proof = std::array<std::uint8_t, kProofBytes>;

// The caller hashes the protocol domain, chain, public key and block context
// into challenge, and supplies the expected iterations when verifying.
// Throws std::invalid_argument for iterations outside [1, kMaxIterations].
// This CPU-intensive operation belongs on a worker, not the network thread.
[[nodiscard]] Proof Prove(const Challenge& challenge, std::uint64_t iterations);

// Rejects invalid lengths, iteration counts, noncanonical forms and bad proofs.
// No peer-supplied group, generator or discriminant is accepted.
[[nodiscard]] bool Verify(const Challenge& challenge, std::uint64_t iterations,
                          std::span<const std::uint8_t> proof) noexcept;

[[nodiscard]] inline std::span<const std::uint8_t, kOutputBytes> Output(const Proof& proof) noexcept {
    return std::span<const std::uint8_t, kProofBytes>(proof).first<kOutputBytes>();
}

} // namespace chain::vdf
