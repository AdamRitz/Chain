#pragma once
#include <array>
#include <sodium.h>
#include <stdexcept>
using namespace std;
struct Wallet {
    array<uint8_t,crypto_sign_PUBLICKEYBYTES> public_key;
    array<uint8_t,crypto_sign_SECRETKEYBYTES> private_key;
};
Wallet mywallet;
Wallet GenerateWallet() {
    Wallet wallet;
    if (crypto_sign_keypair(wallet.public_key.data(),wallet.private_key.data())!=0) throw runtime_error("GenerateWallet failed");
    return wallet;
}
void InitWallet() { mywallet=GenerateWallet(); }
