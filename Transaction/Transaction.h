//
// Created by 61485 on 2026/4/30.
//

#ifndef CHAIN_TRANSACTION_H
#define CHAIN_TRANSACTION_H
#include <array>
#include <cstdint>
using namespace std;
struct Transaction {
    array<uint8_t,32> hash; // SHA-256
    array<uint8_t,20> sender;
    array<uint8_t,20> receiver;
    uint64_t amount;
    uint64_t nonce;
    array<uint8_t,64> signature;// ECDSA
};

uint8_t* test(){}
#endif //CHAIN_TRANSACTION_H