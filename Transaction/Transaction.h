#ifndef CHAIN_TRANSACTION_H
#define CHAIN_TRANSACTION_H
#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <span>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include "../Key/Key.h"
#include "../DB/DB.h"
using namespace std;

// ------------------------------------------------交易和交易池------------------------------------------------
struct Transaction {
    array<uint8_t,32> sender;
    array<uint8_t,32> receiver;
    uint64_t amount;
    uint64_t nonce;
    array<uint8_t,32> hash;
    array<uint8_t,64> signature;
};
unordered_map<array<uint8_t,32>,array<uint8_t,176>,GetMapHash> txpool;
struct PendingUser {
    map<uint64_t,array<uint8_t,32>> txs;
    uint64_t readyNonce=0;
};
unordered_map<array<uint8_t,32>,PendingUser,GetMapHash> txUserPool;
size_t readyPoolTx=0;
mutex txpoolMutex;
condition_variable txpoolCondition;
void WakeMainLoop() {
    lock_guard lock(txpoolMutex);
    txpoolCondition.notify_one();
}
size_t maxPoolTx=200000;
size_t maxBlockTx=6000;
atomic<uint64_t> receivedTx{0},validTx{0},invalidTx{0},duplicateTx{0},rejectedTx{0};
atomic<uint64_t> invalidUserTx{0},conflictTx{0};
atomic<uint64_t> txCommitWaitNs{0},txReadNs{0},txPoolWaitNs{0};
atomic<uint64_t> verifyNs{0},poolNs{0},committedTx{0},firstTxTime{0},lastCommitTime{0};
atomic<bool> nodeRunning{true},nodeFailed{false};

// 调用方持有交易池锁。提交某个序号后，一起清理该账户已过期的候选交易。
void PruneUserPool(const array<uint8_t,32>& key,uint64_t nonce) {
    auto found=txUserPool.find(key);
    if (found==txUserPool.end()) return;
    auto& item=found->second;
    auto& pending=item.txs;
    while (!pending.empty()&&pending.begin()->first<=nonce) {
        if (pending.begin()->first<=item.readyNonce) readyPoolTx--;
        txpool.erase(pending.begin()->second);
        pending.erase(pending.begin());
    }
    if (pending.empty()) { txUserPool.erase(found); return; }
    item.readyNonce=max(item.readyNonce,nonce);
    while (item.readyNonce<UINT64_MAX&&pending.count(item.readyNonce+1)) { item.readyNonce++; readyPoolTx++; }
}
void DropUserPoolFrom(const array<uint8_t,32>& key,uint64_t nonce) {
    auto found=txUserPool.find(key);
    if (found==txUserPool.end()) return;
    auto& item=found->second;
    for (auto it=item.txs.lower_bound(nonce);it!=item.txs.end();) {
        if (it->first<=item.readyNonce) readyPoolTx--;
        txpool.erase(it->second);
        it=item.txs.erase(it);
        invalidUserTx++;
    }
    item.readyNonce=min(item.readyNonce,nonce-1);
    if (item.txs.empty()) txUserPool.erase(found);
}

// ------------------------------------------------序列化------------------------------------------------
array<uint8_t,80> SerializeTxForHash(const Transaction& tx) {
    array<uint8_t,80> data;
    memcpy(data.data(),tx.sender.data(),32);
    memcpy(data.data()+32,tx.receiver.data(),32);
    WriteU64(data.data()+64,tx.amount);
    WriteU64(data.data()+72,tx.nonce);
    return data;
}
array<uint8_t,176> SerializeTxALL(const Transaction& tx,const array<uint8_t,80>& partByte) {
    array<uint8_t,176> data;
    memcpy(data.data(),partByte.data(),80);
    memcpy(data.data()+80,tx.hash.data(),32);
    memcpy(data.data()+112,tx.signature.data(),64);
    return data;
}
Transaction UnserializeTx(const array<uint8_t,176>& data) {
    Transaction tx{};
    memcpy(tx.sender.data(),data.data(),32);
    memcpy(tx.receiver.data(),data.data()+32,32);
    tx.amount=ReadU64(data.data()+64);
    tx.nonce=ReadU64(data.data()+72);
    memcpy(tx.hash.data(),data.data()+80,32);
    memcpy(tx.signature.data(),data.data()+112,64);
    return tx;
}
array<uint8_t,32> GetTransactionHash(const array<uint8_t,176>& data) {
    array<uint8_t,32> hash;
    memcpy(hash.data(),data.data()+80,32);
    return hash;
}
// ------------------------------------------------生成和验证------------------------------------------------
array<uint8_t,176> GenerateTx(const array<uint8_t,32>& sender,const array<uint8_t,32>& receiver,uint64_t amount,uint64_t nonce,const Wallet& wallet) {
    if (sender!=wallet.public_key) throw invalid_argument("Sender does not match wallet");
    Transaction tx{.sender=sender,.receiver=receiver,.amount=amount,.nonce=nonce,.hash={},.signature={}};
    auto partByte=SerializeTxForHash(tx);
    crypto_generichash(tx.hash.data(),32,partByte.data(),partByte.size(),nullptr,0);
    if (crypto_sign_detached(tx.signature.data(),nullptr,tx.hash.data(),32,wallet.private_key.data())!=0) throw runtime_error("Sign failed");
    return SerializeTxALL(tx,partByte);
}
bool VerifyTransaction(const array<uint8_t,176>& data) {
    array<uint8_t,32> hash;
    crypto_generichash(hash.data(),32,data.data(),80,nullptr,0);
    if (sodium_memcmp(hash.data(),data.data()+80,32)!=0) return false;
    return crypto_sign_verify_detached(data.data()+112,hash.data(),32,data.data())==0;
}

// ------------------------------------------------交易处理入口------------------------------------------------
// 锁外批量验签，锁内检查账户状态并更新交易池。
size_t ProcessTxPackage(span<const uint8_t> data) {
    if (data.empty()||data.size()%176!=0||data.size()/176>6000) return 0;
    uint64_t expected=0;
    firstTxTime.compare_exchange_strong(expected,GetSteadyTime());
    receivedTx+=data.size()/176;
    vector<pair<array<uint8_t,32>,array<uint8_t,176>>> checked;
    checked.reserve(data.size()/176);
    auto start=GetSteadyTime();
    for (size_t offset=0;offset<data.size();offset+=176) {
        array<uint8_t,176> tx;
        memcpy(tx.data(),data.data()+offset,176);
        if (!VerifyTransaction(tx)) { invalidTx++; continue; }
        checked.emplace_back(GetTransactionHash(tx),tx);
    }
    verifyNs+=GetSteadyTime()-start;
    start=GetSteadyTime();
    size_t added=0;
    // 查询可并发；提交拿独占锁，防止确认与重新入池之间出现竞态。
    shared_lock commitLock(dbCommitMutex);
    auto readStart=GetSteadyTime();
    txCommitWaitNs+=readStart-start;
    vector<pair<array<uint8_t,32>,array<uint8_t,176>>> fresh;
    fresh.reserve(checked.size());
    for (auto& item:checked) {
        array<uint8_t,32> key;
        memcpy(key.data(),item.second.data(),32);
        auto user=GetUser(users,key);
        auto amount=ReadU64(item.second.data()+64),nonce=ReadU64(item.second.data()+72);
        // 正常新交易用内存账户序号判断；只有过期序号需要查历史哈希。
        if (nonce<=user.nonce&&DBHasTx(item.first)) duplicateTx++;
        else if (!CheckUserTx(user,amount,nonce,true)) { invalidUserTx++; invalidTx++; }
        else fresh.push_back(std::move(item));
    }
    auto poolStart=GetSteadyTime();
    txReadNs+=poolStart-readStart;
    {
        lock_guard lock(txpoolMutex);
        txPoolWaitNs+=GetSteadyTime()-poolStart;
        for (auto& [hash,tx]:fresh) {
            if (txpool.count(hash)!=0) { duplicateTx++; continue; }
            if (txpool.size()>=maxPoolTx) { rejectedTx++; continue; }
            array<uint8_t,32> key;
            memcpy(key.data(),tx.data(),32);
            auto nonce=ReadU64(tx.data()+72);
            auto confirmed=GetUser(users,key).nonce;
            PruneUserPool(key,confirmed);
            auto& pending=txUserPool[key];
            if (pending.txs.count(nonce)) { conflictTx++; continue; }
            pending.txs.emplace(nonce,hash);
            pending.readyNonce=max(pending.readyNonce,confirmed);
            while (pending.readyNonce<UINT64_MAX&&pending.txs.count(pending.readyNonce+1)) { pending.readyNonce++; readyPoolTx++; }
            txpool.emplace(hash,std::move(tx));
            added++;
        }
    }
    validTx+=added;
    poolNs+=GetSteadyTime()-start;
    if (added) WakeMainLoop();
    return added;
}
void ProcessTx(const array<uint8_t,176>& tx) { ProcessTxPackage(span<const uint8_t>(tx)); }
#endif
