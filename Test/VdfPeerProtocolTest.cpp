#include "../Network/VdfPeerProtocol.h"

#include <iostream>

namespace {
void Require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
}

int main() {
    try {
        if (sodium_init() < 0) return 1;
        using namespace chain::vdf_peer;
        PublicKey publicKey{};
        SecretKey secretKey{};
        crypto_sign_keypair(publicKey.data(), secretKey.data());
        Digest chainId{};
        chainId[0] = 7;
        const auto challenge = MakeChallenge();
        Require(challenge != MakeChallenge(), "Connection challenges reused");
        const auto identity = CreateIdentity(chainId, challenge, 8089, secretKey);
        Require(identity.publicKey == publicKey && VerifyIdentity(identity, chainId, challenge, 8089), "Valid identity rejected");
        auto otherChain = chainId;
        otherChain[31] ^= 1;
        Require(!VerifyIdentity(identity, otherChain, challenge, 8089), "Cross-chain identity accepted");
        auto otherChallenge = challenge;
        otherChallenge[31] ^= 1;
        Require(!VerifyIdentity(identity, chainId, otherChallenge, 8089), "Identity from another connection accepted");
        Require(!VerifyIdentity(identity, chainId, challenge, 8090), "Different listen port accepted");
        Require(!VerifyIdentity(identity, chainId, challenge, 0), "Zero listen port accepted");
        auto modified = identity;
        modified.publicKey[0] ^= 1;
        Require(!VerifyIdentity(modified, chainId, challenge, 8089), "Different public key accepted");
        modified = identity;
        modified.signature[63] ^= 1;
        Require(!VerifyIdentity(modified, chainId, challenge, 8089), "Modified signature accepted");
        const auto encoded = EncodeIdentity(identity);
        Identity decoded;
        Require(DecodeIdentity(encoded, decoded) && decoded == identity, "Identity round trip failed");
        Require(!DecodeIdentity(std::span(encoded).first(95), decoded) && decoded == identity, "Truncated identity changed result");
        std::vector<std::uint8_t> trailing(encoded.begin(), encoded.end());
        trailing.push_back(0);
        Require(!DecodeIdentity(trailing, decoded), "Identity trailing byte accepted");

        EndpointHint v4;
        v4.publicKey = publicKey;
        v4.family = 4;
        v4.address[0] = 192;
        v4.address[1] = 168;
        v4.address[3] = 54;
        v4.port = 28189;
        EndpointHint v6;
        v6.publicKey = publicKey;
        v6.family = 6;
        v6.address[0] = 0x24;
        v6.address[1] = 0x08;
        v6.address[15] = 1;
        v6.port = 8089;
        const std::vector<EndpointHint> original{v4, v6};
        const auto hintBytes = EncodeHints(original);
        std::vector<EndpointHint> hints;
        Require(hintBytes.size() == 2 * kHintBytes && DecodeHints(hintBytes, hints) && hints == original,
                "IPv4/IPv6 endpoint round trip failed");
        Require(hintBytes[49] == static_cast<std::uint8_t>(v4.port) && hintBytes[50] == static_cast<std::uint8_t>(v4.port >> 8),
                "Endpoint port is not little endian");
        Require(!DecodeHints({}, hints) && hints.empty(), "Empty hints accepted");
        Require(!DecodeHints(std::span(hintBytes).first(hintBytes.size() - 1), hints), "Truncated hints accepted");
        const std::vector<EndpointHint> maximum(kMaxHints, v4);
        Require(DecodeHints(EncodeHints(maximum), hints) && hints.size() == kMaxHints, "Maximum hint count rejected");
        auto oversized = EncodeHints(maximum);
        oversized.insert(oversized.end(), hintBytes.begin(), hintBytes.begin() + kHintBytes);
        Require(!DecodeHints(oversized, hints) && hints.empty(), "Oversized hints accepted");
        for (const auto [offset, value] : std::vector<std::pair<std::size_t, std::uint8_t>>{
                 {32, 5}, {33, 0}, {33, 224}, {37, 1}, {49, 0}, {50, 0}}) {
            auto malformed = EncodeHints(std::span(original).first(1));
            malformed[offset] = value;
            // Clear both bytes for the zero-port case.
            if (offset == 49 || offset == 50) malformed[49] = malformed[50] = 0;
            Require(!DecodeHints(malformed, hints) && hints.empty(), "Invalid IPv4 hint accepted");
        }
        auto malformed = hintBytes;
        malformed[kHintBytes + 33] = 0xff;
        Require(!DecodeHints(malformed, hints) && hints.empty(), "Invalid trailing IPv6 leaked earlier hints");
        v6.address.fill(0);
        bool threw = false;
        try { (void)EncodeHints(std::span(&v6, 1)); }
        catch (const std::invalid_argument&) { threw = true; }
        Require(threw, "Unspecified IPv6 encoded");
        v4.address = {};
        v4.address[0] = 127;
        v4.address[3] = 1;
        Require(DecodeHints(EncodeHints(std::span(&v4, 1)), hints), "Loopback endpoint rejected");
        sodium_memzero(secretKey.data(), secretKey.size());
        std::cout << "VDF peer protocol correctness passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
