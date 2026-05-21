#pragma once

#include <string>
#include <vector>
#include <sodium.h>
#include <array>

using namespace  std;
struct Wallet
{
    array<uint8_t, crypto_sign_PUBLICKEYBYTES> public_key;
    array<uint8_t, crypto_sign_SECRETKEYBYTES> private_key;
};
Wallet mywallet;
Wallet GenerateWallet() {
    Wallet wallet;
    // 传入公钥和私钥的存储位置（指针），写入公私钥数据到目标位置。
    crypto_sign_keypair(wallet.public_key.data(), wallet.private_key.data());

    return wallet;

}
void InitWallet() {
    mywallet=GenerateWallet();
}

