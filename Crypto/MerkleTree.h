//
// Created by 61485 on 2026/4/30.
//

#ifndef CHAIN_HASH_H
#define CHAIN_HASH_H
#include <array>
#include <assert.h>
#include <cstdint>
#include <cstring>
#include <vector>
#include <span>
#include <sodium/crypto_generichash.h>
using namespace std;
array<uint8_t,32> HashHash(const array<uint8_t,32>& a,const array<uint8_t,32>& b) {
    array<uint8_t,64> combine={};
    memcpy(combine.data(),a.data(),32);
    memcpy(combine.data()+32,b.data(),32);
    array<uint8_t,32> ressult{};
    crypto_generichash(ressult.data(),32,combine.data(),64,nullptr,0);
    return ressult;
}
std::array<uint8_t, 32> MerkleCompute(std::span<const std::array<uint8_t, 32>> list) {
    if (list.empty()) {
        return {};
    }
    vector<array<uint8_t, 32>> current(list.begin(), list.end());
    while (current.size() > 1) {
        if (current.size() % 2 != 0) {
            current.push_back(current.back());
        }
        vector<array<uint8_t, 32>> next;
        next.reserve(current.size() / 2);
        for (size_t i = 0; i <= current.size()-1; i += 2) {
            next.push_back(HashHash(current[i], current[i + 1]));
        }
        current = move(next);
    }

    return current[0];
}

#endif //CHAIN_HASH_H