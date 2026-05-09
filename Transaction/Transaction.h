//
// Created by 61485 on 2026/4/30.
//

#ifndef CHAIN_TRANSACTION_H
#define CHAIN_TRANSACTION_H
#include <array>
#include <cstdint>
#include "../Key/Key.h"
#include "../DB/DB.h"
using namespace std;
// -----------------------------------------------------------------------交易/交易池定义-------------------------------------------------------------------------------------------------
struct Transaction {
    array<uint8_t,32> sender;
    array<uint8_t,32> receiver;
    uint64_t amount;
    uint64_t nonce;
    array<uint8_t,crypto_generichash_BYTES> hash; // BLAKE2b
    array<uint8_t,64> signature; // Ed25519
};
struct GetMapHash{
    size_t operator()(const std::array<uint8_t, 32>& data) const noexcept {
        size_t h = 0;
        std::memcpy(&h, data.data(), sizeof(size_t));
        return h;
    }
};
unordered_map<array<uint8_t,32>,array<uint8_t,176>,GetMapHash> txpool;
std::mutex txpoolMutex;
// -----------------------------------------------------------------------交易序列化/反序列化-----------------------------------------------------------------------------------------------
// 为 Hash 序列化交易数据：发送者，接收者，数值，nonce。为了 hash 创建的序列化，所以此处没有序列化 Hash 和 签名因为还没生成
array<uint8_t,80> SerializeTxForHash(const Transaction& tx) {
    array<uint8_t,80> partByte;
    int offset = 0;
    memcpy(partByte.data(),tx.sender.data(),tx.sender.size());
    offset += tx.sender.size();
    memcpy(partByte.data()+offset,tx.receiver.data(),tx.receiver.size());
    offset += tx.receiver.size();
    memcpy(partByte.data()+offset,&tx.amount,8);
    offset += 8;
    memcpy(partByte.data()+offset,&tx.nonce,8);
    return partByte;
}
// 序列化整个交易。
array<uint8_t,176> SerializeTxALL(const Transaction& tx,const array<uint8_t,80>& partByte) {
    array<uint8_t,176> txByte;
    int offset = 0;
    memcpy(txByte.data(),partByte.data(),80);
    offset += 80;
    memcpy(txByte.data()+offset,tx.hash.data(),32);
    offset += 32;
    memcpy(txByte.data()+offset,tx.signature.data(),64);
    offset += 64;
    return txByte;

}
// 反序列化交易
Transaction UnserializeTx(array<uint8_t,176> txBytes) {
    Transaction tx{};
    int offset=0;
    memcpy(tx.sender.data(),txBytes.data()+offset,32);
    offset += 32;
    memcpy(tx.receiver.data(),txBytes.data()+offset,32);
    offset += 32;
    memcpy(&tx.amount,txBytes.data()+offset,8);
    offset += 8;
    memcpy(&tx.nonce,txBytes.data()+offset,8);
    offset += 8;
    memcpy(tx.hash.data(),txBytes.data()+offset,32);
    offset += 32;
    memcpy(tx.signature.data(),txBytes.data()+offset,64);
    offset+= 64;
    return tx;
}

// -----------------------------------------------------------------------交易生成/验证-------------------------------------------------------------------------------------------------
array<uint8_t,176> GenerateTx(array<uint8_t,32> sender,array<uint8_t,32> receiver,uint64_t amount,uint64_t nonce, Wallet mywallet) {
    Transaction tx{.sender=sender,.receiver = receiver,.amount = amount,.nonce = nonce};
    array<uint8_t,80> partByte = SerializeTxForHash(tx);
    crypto_generichash(tx.hash.data(),tx.hash.size(),partByte.data(),partByte.size(),nullptr,0);
    crypto_sign_detached(tx.signature.data(),nullptr,tx.hash.data(),tx.hash.size(),mywallet.private_key.data());
    return SerializeTxALL(tx,partByte);
}

bool VerifyTransaction(array<uint8_t,176> txbyte) {
    array<uint8_t,32> sender;
    memcpy(sender.data(),txbyte.data(),32);
    if (crypto_sign_verify_detached(txbyte.data()+80+32,txbyte.data()+80,32,sender.data())!=0) {
        spdlog::info("Tx Verification Failed");

        return false;
    }
    return true;
}


// -----------------------------------------------------------------------工具函数-------------------------------------------------------------------------------------------------
array<uint8_t,32> GetTransactionHash(const array<uint8_t,176>& txByte) {
    array<uint8_t,32> hash;
    memcpy(hash.data(),txByte.data()+80,32);
    return hash;
}
// -------------------------------------------------------------------交易处理入口---------------------------------------------------------------------------------------------------
// 网络中收到交易后通过该入口函数处理，成功后放入交易池
void ProcessTx(array<uint8_t,176> txbyte) {
    // 交易验证失败就不进行处理。
    if (!VerifyTransaction(txbyte)) {
        return;
    }
    lock_guard<mutex> lock(txpoolMutex);
    txpool[GetTransactionHash(txbyte)] = txbyte;
}
// 出块后的交易处理函数：把交易存储到本地的 RockDB 而不是交易池，见出块函数逻辑，此处不再单独编写一个函数。
#endif //CHAIN_TRANSACTION_H