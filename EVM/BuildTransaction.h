#pragma once
// Shared by the CLI and the classroom signer; same Ed25519 envelope.
vector<uint8_t> MakeEvmTx(const Wallet &wallet, span<const uint8_t> target, span<const uint8_t> input, uint64_t nonce,
                          uint64_t amount, uint64_t gas, bool deploy) {
    if (input.size() > maxEvmInput || (!deploy && target.size() != 20) || !nonce)
        throw invalid_argument("Invalid EVM transaction");
    vector<uint8_t> tx(221 + input.size());
    memcpy(tx.data(), wallet.public_key.data(), 32);
    if (!deploy)
        memcpy(tx.data() + 44, target.data(), 20);
    WriteU64(tx.data() + 64, amount);
    WriteU64(tx.data() + 72, nonce);
    memcpy(tx.data() + 176, "EVM1", 4);
    tx[180] = deploy ? 0 : 1;
    WriteU64(tx.data() + 181, gas);
    memcpy(tx.data() + 189, genesisRoot.data(), 32);
    if (!input.empty())
        memcpy(tx.data() + 221, input.data(), input.size());
    auto hash = EvmTransactionHash(tx, span<const uint8_t>(tx).subspan(176));
    memcpy(tx.data() + 80, hash.data(), 32);
    crypto_sign_detached(tx.data() + 112, nullptr, hash.data(), 32, wallet.private_key.data());
    return tx;
}
