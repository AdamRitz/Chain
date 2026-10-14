#ifndef CHAIN_TRANSACTION_H
#define CHAIN_TRANSACTION_H
#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <deque>
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
unordered_map<array<uint8_t,32>,vector<uint8_t>,GetMapHash> evmPool;
size_t evmPoolBytes=0;
void EraseEvmPool(const array<uint8_t,32>& hash) {
    auto found=evmPool.find(hash);
    if (found!=evmPool.end()) { evmPoolBytes-=found->second.size(); evmPool.erase(found); }
}
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
atomic<uint64_t> gapAcceptedTx{0},poolBusyTx{0};
atomic<uint64_t> signatureChecks{0},signatureCacheHits{0},poolRefreshNs{0};
atomic<uint64_t> signatureCacheEvictions{0},signatureWaitNs{0},signatureSharedChecks{0};
struct VerifiedTransactions {
    mutex lock;
    condition_variable condition;
    unordered_set<array<uint8_t,32>,GetMapHash> hashes;
    unordered_set<array<uint8_t,32>,GetMapHash> pending;
    size_t waiters=0;
    deque<array<uint8_t,32>> order;
};
array<VerifiedTransactions,16> verifiedTransactions;
size_t maxVerifiedPerBucket=32768;
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
        EraseEvmPool(pending.begin()->second);
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
        EraseEvmPool(it->second);
        it=item.txs.erase(it);
        invalidUserTx++;
    }
    item.readyNonce=min(item.readyNonce,nonce-1);
    if (item.txs.empty()) txUserPool.erase(found);
}

bool HasPoolCapacity(const array<uint8_t,32>& key,uint64_t nonce,uint64_t confirmed,size_t payloadBytes=0) {
    if (txpool.size()>=maxPoolTx||payloadBytes>64*1024*1024-evmPoolBytes) return false;
    auto found=txUserPool.find(key);
    auto ready=found==txUserPool.end()?confirmed:max(confirmed,found->second.readyNonce);
    bool gap=ready<UINT64_MAX&&nonce==ready+1;
    auto normal=maxPoolTx-max(size_t(1),maxPoolTx/10);
    if (!gap&&txpool.size()>=normal) return false;
    if (gap&&txpool.size()>=normal) gapAcceptedTx++;
    return true;
}
enum TxStatus:uint8_t { TxAccepted=0,TxKnown=1,TxBusy=2,TxInvalid=3,TxConflict=4 };
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
bool VerifyTransaction(const array<uint8_t,176>& data,span<const uint8_t> payload={}) {
    if (!payload.empty()&&(!ValidEvmPayload(payload)||memcmp(payload.data()+13,genesisRoot.data(),32))) return false;
    // 完整交易和合约输入共同标识验签结果，缓存独立于交易池和分支状态。
    array<uint8_t,32> fingerprint;
    crypto_generichash_state state;
    crypto_generichash_init(&state,nullptr,0,fingerprint.size());
    crypto_generichash_update(&state,data.data(),data.size());
    if (!payload.empty()) crypto_generichash_update(&state,payload.data(),payload.size());
    crypto_generichash_final(&state,fingerprint.data(),fingerprint.size());
    auto& cache=verifiedTransactions[fingerprint[0]%verifiedTransactions.size()];
    {
        unique_lock lock(cache.lock);
        if (cache.pending.count(fingerprint)) {
            auto start=GetSteadyTime(); signatureSharedChecks++;
            cache.waiters++;
            cache.condition.wait(lock,[&] { return !cache.pending.count(fingerprint); });
            cache.waiters--;
            signatureWaitNs+=GetSteadyTime()-start;
        }
        if (cache.hashes.count(fingerprint)) { signatureCacheHits++; return true; }
        cache.pending.insert(fingerprint);
    }
    struct Complete {
        VerifiedTransactions& cache; const array<uint8_t,32>& fingerprint;
        ~Complete() { lock_guard lock(cache.lock); cache.pending.erase(fingerprint); if (cache.waiters) cache.condition.notify_all(); }
    } complete{cache,fingerprint};
    array<uint8_t,32> hash;
    if (payload.empty()) crypto_generichash(hash.data(),32,data.data(),80,nullptr,0);
    else {
        hash=EvmTransactionHash(data,payload);
    }
    if (sodium_memcmp(hash.data(),data.data()+80,32)!=0) return false;
    signatureChecks++;
    if (crypto_sign_verify_detached(data.data()+112,hash.data(),32,data.data())!=0) return false;
    {
        lock_guard lock(cache.lock);
        if (!cache.hashes.count(fingerprint)) {
            while (cache.order.size()>=maxVerifiedPerBucket) {
                cache.hashes.erase(cache.order.front()); cache.order.pop_front(); signatureCacheEvictions++;
            }
            cache.hashes.insert(fingerprint); cache.order.push_back(fingerprint);
        }
    }
    return true;
}

// ------------------------------------------------交易处理入口------------------------------------------------
// 锁外批量验签，锁内检查账户状态并更新交易池。
size_t ProcessTxPackage(span<const uint8_t> data,vector<uint8_t>* results=nullptr) {
    if (data.empty()||data.size()%176!=0||data.size()/176>maxProtocolTx) return 0;
    if (results) results->assign(data.size()/176,TxInvalid);
    uint64_t expected=0;
    firstTxTime.compare_exchange_strong(expected,GetSteadyTime());
    receivedTx+=data.size()/176;
    struct CheckedTx { array<uint8_t,32> hash; array<uint8_t,176> tx; size_t index; };
    vector<CheckedTx> checked;
    checked.reserve(data.size()/176);
    auto start=GetSteadyTime();
    for (size_t offset=0;offset<data.size();offset+=176) {
        array<uint8_t,176> tx;
        memcpy(tx.data(),data.data()+offset,176);
        if (!VerifyTransaction(tx)) { invalidTx++; continue; }
        checked.push_back({GetTransactionHash(tx),tx,offset/176});
    }
    verifyNs+=GetSteadyTime()-start;
    start=GetSteadyTime();
    size_t added=0;
    // 账户镜像保持到本批交易入池完成，再允许提交发布新序号。
    shared_lock commitLock(poolUsersMutex);
    auto readStart=GetSteadyTime();
    txCommitWaitNs+=readStart-start;
    vector<CheckedTx> fresh;
    fresh.reserve(checked.size());
    for (auto& item:checked) {
        array<uint8_t,32> key;
        memcpy(key.data(),item.tx.data(),32);
        auto user=GetUser(poolUsers,key);
        auto amount=ReadU64(item.tx.data()+64),nonce=ReadU64(item.tx.data()+72);
        // 接收使用已发布账户镜像，写盘期间继续验证并更新池。
        if (nonce<=user.nonce&&DBHasTx(item.hash)) { duplicateTx++; if (results) (*results)[item.index]=TxKnown; }
        else if (!CheckUserTx(user,amount,nonce,true)) { invalidUserTx++; invalidTx++; }
        else fresh.push_back(std::move(item));
    }
    auto poolStart=GetSteadyTime();
    txReadNs+=poolStart-readStart;
    {
        lock_guard lock(txpoolMutex);
        txPoolWaitNs+=GetSteadyTime()-poolStart;
        for (auto& [hash,tx,index]:fresh) {
            if (txpool.count(hash)!=0) { duplicateTx++; if (results) (*results)[index]=TxKnown; continue; }
            array<uint8_t,32> key;
            memcpy(key.data(),tx.data(),32);
            auto nonce=ReadU64(tx.data()+72);
            auto confirmed=GetUser(poolUsers,key).nonce;
            PruneUserPool(key,confirmed);
            auto found=txUserPool.find(key);
            if (found!=txUserPool.end()&&found->second.txs.count(nonce)) { conflictTx++; if (results) (*results)[index]=TxConflict; continue; }
            if (!HasPoolCapacity(key,nonce,confirmed)) { rejectedTx++; poolBusyTx++; if (results) (*results)[index]=TxBusy; continue; }
            auto& pending=txUserPool[key];
            pending.txs.emplace(nonce,hash);
            pending.readyNonce=max(pending.readyNonce,confirmed);
            while (pending.readyNonce<UINT64_MAX&&pending.txs.count(pending.readyNonce+1)) { pending.readyNonce++; readyPoolTx++; }
            txpool.emplace(hash,std::move(tx));
            added++; if (results) (*results)[index]=TxAccepted;
        }
    }
    commitLock.unlock();
    validTx+=added;
    poolNs+=GetSteadyTime()-start;
    if (added) WakeMainLoop();
    return added;
}
void ProcessTx(const array<uint8_t,176>& tx) { ProcessTxPackage(span<const uint8_t>(tx)); }
bool ProcessEvmTx(span<const uint8_t> data) {
    if (data.size()<221||data.size()>176+45+maxEvmInput) return false;
    uint64_t expected=0; firstTxTime.compare_exchange_strong(expected,GetSteadyTime()); receivedTx++;
    array<uint8_t,176> tx; memcpy(tx.data(),data.data(),176);
    auto payload=data.subspan(176);
    auto start=GetSteadyTime();
    if (!VerifyTransaction(tx,payload)) { invalidTx++; return false; }
    verifyNs+=GetSteadyTime()-start;
    auto hash=GetTransactionHash(tx);
    array<uint8_t,32> key; memcpy(key.data(),tx.data(),32);
    shared_lock commitLock(poolUsersMutex);
    auto user=GetUser(poolUsers,key);
    if (!CheckEvmUserTx(user,tx,payload,true)) { invalidUserTx++; invalidTx++; return false; }
    {
        lock_guard lock(txpoolMutex);
        if (txpool.count(hash)) { duplicateTx++; return false; }
        auto nonce=ReadU64(tx.data()+72);
        PruneUserPool(key,user.nonce);
        auto found=txUserPool.find(key);
        if (found!=txUserPool.end()&&found->second.txs.count(nonce)) { conflictTx++; return false; }
        if (!HasPoolCapacity(key,nonce,user.nonce,payload.size())) { rejectedTx++; poolBusyTx++; return false; }
        auto& pending=txUserPool[key];
        pending.txs.emplace(nonce,hash);
        pending.readyNonce=max(pending.readyNonce,user.nonce);
        while (pending.readyNonce<UINT64_MAX&&pending.txs.count(pending.readyNonce+1)) { pending.readyNonce++; readyPoolTx++; }
        txpool.emplace(hash,tx);
        evmPool.emplace(hash,vector<uint8_t>(payload.begin(),payload.end())); evmPoolBytes+=payload.size();
    }
    validTx++; WakeMainLoop(); return true;
}
void RefreshTxPool() {
    auto start=GetSteadyTime();
    shared_lock commitLock(dbCommitMutex);
    lock_guard lock(txpoolMutex);
    txUserPool.clear(); readyPoolTx=0;
    for (auto it=txpool.begin();it!=txpool.end();) {
        const auto& tx=it->second;
        array<uint8_t,32> key; memcpy(key.data(),tx.data(),32);
        auto user=GetUser(users,key);
        auto nonce=ReadU64(tx.data()+72);
        auto payload=evmPool.find(it->first);
        bool valid=payload==evmPool.end()?CheckUserTx(user,ReadU64(tx.data()+64),nonce,true):CheckEvmUserTx(user,tx,payload->second,true);
        if (!valid) { EraseEvmPool(it->first); it=txpool.erase(it); continue; }
        auto& pending=txUserPool[key];
        pending.readyNonce=user.nonce;
        pending.txs.emplace(nonce,it->first);
        ++it;
    }
    for (auto& [key,pending]:txUserPool) {
        while (pending.readyNonce<UINT64_MAX&&pending.txs.count(pending.readyNonce+1)) { pending.readyNonce++; readyPoolTx++; }
    }
    poolRefreshNs+=GetSteadyTime()-start;
}
#endif
