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
// -----------------------------------------------------------------------区块定义-------------------------------------------------------------------------------------------------
struct Block {
    // 区块头 80 字节
    array<uint8_t,32> previousHash;
    array<uint8_t,32> merkleRoot;
    uint64_t height;
    uint64_t txNum;
    array<uint8_t,32> hash;
    // 交易数据
    vector<array<uint8_t,176>> txs;
};
uint64_t epoch = 2;
pair<Block,vector<uint8_t>> BlockBuffer[3];
mutex blockBufferLock;
unordered_map<uint64_t,Block> blockPool;
mutex blockMutex;

// -----------------------------------------------------------------------区块序列化/反序列化-------------------------------------------------------------------------------------------------
array<uint8_t,80> SerializeBlockHeader(const Block& block) {
    array<uint8_t,80> header{};
    int offset = 0;
    memcpy(header.data()+offset,block.previousHash.data(),32);
    offset += 32;
    memcpy(header.data()+offset,block.merkleRoot.data(),32);
    offset += 32;
    memcpy(header.data()+offset,&block.height,8);
    offset += 8;
    memcpy(header.data()+offset,&block.txNum,8);
    offset += 8;
    return header;
}

Block UnSerializeBlock(const vector<uint8_t>& blockByte) {
    Block block{};
    int offset = 0;
    memcpy(block.previousHash.data(),blockByte.data(),32);
    offset += 32;
    memcpy(block.merkleRoot.data(),blockByte.data()+offset,32);
    offset += 32;
    memcpy(&block.height,blockByte.data()+offset,8);
    offset += 8;
    memcpy(&block.txNum,blockByte.data()+offset,8);
    offset += 8;
    memcpy(block.hash.data(),blockByte.data()+offset,32);
    offset += 32;
    block.txs.resize(block.txNum);
    memcpy(block.txs.data(),blockByte.data()+offset,block.txNum*176);
    return block;
}
// -----------------------------------------------------------------------区块生成/验证-------------------------------------------------------------------------------------------------
vector<uint8_t> GenerateBlock() {
    Block block;
    block.previousHash=DBReadCurrentBlock();
    vector<array<uint8_t,32>> txhashs;
    // 此处加锁是因为一个线程在添加交易到交易池，另一个线程产生区块。
    if (txpool.size()==0) {
        vector<uint8_t> txbyte{};
        return txbyte;
    }
    {
        lock_guard<mutex> lock(txpoolMutex);
        int num = 0;
        for (auto kv =txpool.begin(); kv != txpool.end(); ) {
            if (num==6000)break;
            block.txs.emplace_back(kv->second);
            txhashs.push_back(kv->first);
            kv = txpool.erase(kv);
            num++;
        }
    }
    block.merkleRoot=MerkleCompute(txhashs);
    block.height=epoch;
    block.txNum=block.txs.size();
    array<uint8_t,80> blockHeaderByte= SerializeBlockHeader(block);
    crypto_generichash(block.hash.data(),32,blockHeaderByte.data(),80,nullptr,0);
    vector<uint8_t> blockData;
    blockData.resize(80+block.txNum*176+32);
    memcpy(blockData.data(),blockHeaderByte.data(),80);
    memcpy(blockData.data()+80,block.hash.data(),32);
    int offset = 80+32;
    for (auto tx : block.txs) {
        memcpy(blockData.data()+offset,tx.data(),176);
        offset += 176;
    }

    return blockData;
}

// 验证是否是新区块、合法区块；不是则丢弃；
bool VerifyBlock(vector<uint8_t> blockByte) {
    // 反序列化
    Block block=UnSerializeBlock(blockByte);

    // 验证 Hash
    auto blockHeader = SerializeBlockHeader(block);
    array<uint8_t,32> hash;
    crypto_generichash(hash.data(),32,blockHeader.data(),80,nullptr,0);
    if (block.hash!=hash) {
        return false;
    }
    // 验证 交易正确性 和 MerkleRoot
    vector<array<uint8_t,32>> txHashs;
    for (auto tx : block.txs) {
        if (VerifyTransaction(tx)==false) {
            spdlog::info("Block's Tx Verification Failed");
        };
        array<uint8_t,32> txHash;
        memcpy(txHash.data(),tx.data()+80,32);
        txHashs.emplace_back(txHash);
    }
    if (MerkleCompute(txHashs)!=block.merkleRoot) {
        return false;
    }
    return true;
}

// 创世块生成
void GenerateGenesisBlock() {
    Block block;
    block.previousHash={};
    block.merkleRoot={};
    block.height=1;
    block.txNum=0;
    array<uint8_t,80> blockHeaderByte= SerializeBlockHeader(block);
    crypto_generichash(block.hash.data(),32,blockHeaderByte.data(),80,nullptr,0);
    vector<array<uint8_t,32>> txhashs;
    DBWriteCurrentBlock(block.hash);
    DBWriteBlockHeight(block.height);
    vector<uint8_t> blockData;
    blockData.resize(80+32);
    memcpy(blockData.data(),blockHeaderByte.data(),80);
    memcpy(blockData.data()+80,block.hash.data(),32);
    DBWriteBlockALL(block.hash,blockData);
    spdlog::info("Genesis Block Generated. Current Block Height: 1 ");
}
// -----------------------------------------------------------------------核心入口函数：区块处理/定时打包区块-------------------------------------------------------




// -----------------------------------------------------------------------核心入口函数：区块处理/定时打包区块-------------------------------------------------------
// 网络中收到区块时交给该入口函数处理，此处只把区块放入 blockBuffer，不写入区块链。由主定时函数定时读取 blockBuffer 写入区块链。
void ProcessBlock(vector<uint8_t> blockByte) {
    // 1.验证区块
    if (VerifyBlock(blockByte)== false) {
        spdlog::info("Block Verification Failed");
        return;
    }
    // 2.反序列化
    Block block=UnSerializeBlock(blockByte);
    if (blockPool.count(block.height)!=0) {

    }
    // 3.判断是否放入缓存 0 < block.height - height < 3
    //  （1）交易多被选中 （2）交易相等，则 Hash 小的被选中
    if (block.height - epoch >=0 && block.height - epoch <=2) {
        if ((block.txNum > BlockBuffer[block.height - epoch].first.txNum) || block.txNum == BlockBuffer[block.height].first.txNum&&block.hash < BlockBuffer[block.height].first.hash) {
            BlockBuffer[block.height - epoch].first = block;
            BlockBuffer[block.height - epoch].second = blockByte;
        }
    }


}


void PeriodTxMonitor() {
    sleep(1);
    {
        lock_guard<mutex> lock(txpoolMutex);
        spdlog::info("TX num in pool: {}",txpool.size());
    }
}

#endif //CHAIN_BLOCK_H