//
// Created by DonQuixote on 2026/5/5.
//

#ifndef CHAIN_BLOCK_H
#define CHAIN_BLOCK_H
#include <array>
#include <cstdint>

#include "Transaction.h"
#include "../Key/Key.h"
#include "../DB/DB.h"
#include "../Crypto/MerkleTree.h"
struct Block {

    array<uint8_t,32> previousHash;
    array<uint8_t,32> merkleRoot;
    uint64_t height;
    uint64_t txNum;
    vector<array<uint8_t,176>> txs;
    array<uint8_t,32> hash;
};
vector<uint8_t> SerializeBlock(const Block& block) {

}
void GenerateBlock() {
    Block block;
    block.previousHash=DBReadCurrentBlock();
    vector<array<uint8_t,32>> txhashs;
    for (auto kv : txpool) {
        block.txs.emplace_back(kv.second);
        txhashs.push_back(kv.first);
    }
    block.merkleRoot=MerkleCompute(txhashs);


}

bool VerifyBlock() {

}

void ProcessBlock(Block block) {

    db.write(block.hash.data(),block.hash.size());
}


#endif //CHAIN_BLOCK_H