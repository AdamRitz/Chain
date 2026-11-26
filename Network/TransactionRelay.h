#ifndef CHAIN_TRANSACTION_RELAY_H
#define CHAIN_TRANSACTION_RELAY_H
#include <array>
#include <deque>
#include <unordered_map>
#include <unordered_set>

// Included by Client.h after Peer, the send functions, and Blocks.h.
bool txInventory=true;
size_t txPushPeers=3;
constexpr size_t maxTxInventoryItems=1024,maxTxPending=16384,maxTxSources=4;
constexpr size_t maxTxKnownPeers=256,maxTxKnownPerPeer=4096,maxTxKnownItems=131072;
constexpr size_t maxTxRecentBytes=32*1024*1024,maxTxRecentCount=131072;
constexpr uint64_t txRequestRetryNs=200000000ULL,txRequestLifetimeNs=5000000000ULL;
constexpr uint64_t txBodyWaitNs=2000000000ULL;
constexpr size_t maxTxBodyRecords=65536,maxTxBodyBytes=48*1024*1024,maxTxBodyVariants=4;
constexpr size_t maxTxRequestReplyBytes=1024*1024;
constexpr uint64_t txReplyRateBytes=4*1024*1024,txReplyBurstBytes=4*1024*1024;
atomic<uint64_t> txInventoryAnnounced{0},txInventoryRequests{0},txInventoryRetries{0};
atomic<uint64_t> txInventoryExpired{0},txInventoryDropped{0},txInventoryReplies{0};
atomic<uint64_t> txInventoryBodySent{0},txInventoryHashSent{0},txInventoryKnownSkipped{0};
atomic<uint64_t> txBodyWaitSkips{0},txBodyWaitDuplicates{0},txBodyWaitTimeouts{0},txBodyWaitReleased{0},txBodyWaitCapacity{0};

struct TxInventoryKey {
    uint8_t kind=0; // 0: native; 1: EVM, whose hash binds the complete input.
    array<uint8_t,32> hash{};
    bool operator==(const TxInventoryKey&) const=default;
};
struct TxInventoryHash {
    size_t operator()(const TxInventoryKey& key) const {
        // Announced hashes are chosen by an untrusted sender. Hash all bytes
        // with a process-local secret, instead of using their first machine word.
        static const array<uint8_t,16> salt=[] {array<uint8_t,16> value{};randombytes_buf(value.data(),value.size());return value;}();
        array<uint8_t,8> result{};crypto_shorthash_siphash24(result.data(),key.hash.data(),key.hash.size(),salt.data());
        size_t value=0;memcpy(&value,result.data(),sizeof(value));return value^(size_t(key.kind)<<1);
    }
};
bool DecodeTxInventory(span<const uint8_t> data,vector<TxInventoryKey>& keys) {
    keys.clear();
    if (data.empty()||data.size()%33||data.size()/33>maxTxInventoryItems) return false;
    // Validate the entire message before exposing any item to callers.
    for (size_t i=0;i<data.size();i+=33) if (data[i]>1) return false;
    unordered_set<TxInventoryKey,TxInventoryHash> unique;
    keys.reserve(data.size()/33);
    for (size_t i=0;i<data.size();i+=33) {
        TxInventoryKey key; key.kind=data[i]; memcpy(key.hash.data(),data.data()+i+1,32);
        if (unique.insert(key).second) keys.push_back(key);
    }
    return true;
}
vector<uint8_t> EncodeTxInventory(span<const TxInventoryKey> keys) {
    if (keys.empty()||keys.size()>maxTxInventoryItems) throw invalid_argument("Transaction inventory size");
    vector<uint8_t> data(keys.size()*33);
    for (size_t i=0;i<keys.size();++i) {
        if (keys[i].kind>1) throw invalid_argument("Transaction inventory kind");
        data[i*33]=keys[i].kind; memcpy(data.data()+i*33+1,keys[i].hash.data(),32);
    }
    return data;
}
TxInventoryKey TransactionInventoryKey(span<const uint8_t> body,bool evm=false) {
    TxInventoryKey key; key.kind=evm?1:0;
    if ((!evm&&body.size()!=176)||(evm&&(body.size()<221||body.size()>221+maxEvmInput)))
        throw invalid_argument("Transaction body size");
    memcpy(key.hash.data(),body.data()+80,32); return key;
}
struct TxPeerKnown {
    weak_ptr<Peer> peer;
    unordered_set<TxInventoryKey,TxInventoryHash> hashes;
    deque<TxInventoryKey> order;
    unordered_set<TxInventoryKey,TxInventoryHash> notices;
    deque<TxInventoryKey> noticeOrder;
    uint64_t touched=0,replyAt=0,replyTokens=txReplyBurstBytes;
};
struct PendingTransaction {
    vector<weak_ptr<Peer>> sources;
    uint64_t created=0,lastRequest=0,lastSource=0;
    unsigned attempts=0;
    size_t nextSource=0;
};
mutex txInventoryMutex;
unordered_map<uint64_t,TxPeerKnown> txPeerKnown;
unordered_map<TxInventoryKey,PendingTransaction,TxInventoryHash> txPending;
unordered_map<TxInventoryKey,shared_ptr<const vector<uint8_t>>,TxInventoryHash> txRecentBodies;
deque<TxInventoryKey> txRecentOrder;
size_t txRecentBytes=0,txKnownItems=0;

// Receipt is separate from validity. Only normal transaction processing may
// insert a body in txRecentBodies or erase its missing-body request.
struct ReceivingTransaction {
    uint64_t token=0,created=0;
    bool retryReleased=false;
    map<array<uint8_t,32>,size_t> bodies; // Complete-body fingerprints, including signatures.
};
unordered_map<TxInventoryKey,ReceivingTransaction,TxInventoryHash> txReceiving;
size_t txReceivingBytes=0;
uint64_t nextTxBodyToken=0;
void EraseTxReceivingLocked(const TxInventoryKey& key) {
    txReceiving.erase(key);
}
bool TxBodyWaitingLocked(const TxInventoryKey& key,uint64_t now) {
    auto found=txReceiving.find(key);
    if (found==txReceiving.end()||found->second.retryReleased||found->second.bodies.empty()) return false;
    auto& receiving=found->second;
    if (now<receiving.created||now-receiving.created>=txBodyWaitNs) {
        receiving.retryReleased=true;txBodyWaitTimeouts++;return false;
    }
    return true;
}
struct TxBodyReceipt {
    TxInventoryKey key;
    array<uint8_t,32> fingerprint{};
    uint64_t token=0,source=0;
    size_t bytes=0;
};
struct TxBodyReservation {
    vector<TxBodyReceipt> receipts;
    ~TxBodyReservation() {
        lock_guard lock(txInventoryMutex);
        for (const auto& receipt:receipts) {
            // Keep bytes reserved until the actual task releases its lease,
            // even if its map record expired or was completed by another task.
            txReceivingBytes-=receipt.bytes;
            auto found=txReceiving.find(receipt.key);
            if (found==txReceiving.end()||found->second.token!=receipt.token) continue;
            auto body=found->second.bodies.find(receipt.fingerprint);
            if (body==found->second.bodies.end()) continue;
            found->second.bodies.erase(body);
            // A failed/cancelled owner releases the pause even if another
            // signature variant is queued. Duplicates cannot renew this pause.
            found->second.retryReleased=true;txBodyWaitReleased++;
            if (auto pending=txPending.find(receipt.key);pending!=txPending.end()) {
                pending->second.lastRequest=0;
                for (size_t i=0;i<pending->second.sources.size();++i)
                    if (auto peer=pending->second.sources[i].lock();peer&&peer->key==receipt.source)
                        pending->second.nextSource=i+1;
            }
        }
    }
};
shared_ptr<TxBodyReservation> ReserveTxBodies(const shared_ptr<Peer>& peer,uint8_t type,span<const uint8_t> data,span<const array<uint8_t,32>> fingerprints={}) {
    if (!txInventory) return {};
    vector<pair<span<const uint8_t>,bool>> bodies;
    if (type==1||type==10||type==44) {
        if (data.empty()||data.size()%176) return {};
        for (size_t offset=0;offset<data.size();offset+=176) bodies.emplace_back(data.subspan(offset,176),false);
    } else if (type==17) {
        if (data.size()<221||data.size()>221+maxEvmInput) return {};
        bodies.emplace_back(data,true);
    } else if (type==18) {
        size_t offset=0;
        while (offset<data.size()) {
            if (data.size()-offset<4) return {};
            auto length=ReadU32(data.data()+offset);offset+=4;
            if (length<221||length>221+maxEvmInput||length>data.size()-offset) return {};
            bodies.emplace_back(data.subspan(offset,length),true);offset+=length;
        }
    } else return {};
    if (!fingerprints.empty()&&fingerprints.size()!=bodies.size()) throw invalid_argument("Transaction body fingerprint count");
    auto reservation=make_shared<TxBodyReservation>();
    for (size_t index=0;index<bodies.size();++index) {
        const auto& [body,evm]=bodies[index];
        array<uint8_t,32> hash{},fingerprint{};
        if (evm) {
            auto payload=body.subspan(176);
            if (!ValidEvmPayload(payload)||memcmp(payload.data()+13,genesisRoot.data(),32)) continue;
            hash=EvmTransactionHash(body.first(176),payload);
        } else crypto_generichash(hash.data(),32,body.data(),80,nullptr,0);
        // A forged hash field cannot suspend requests for unrelated content.
        if (sodium_memcmp(hash.data(),body.data()+80,32)) continue;
        if (fingerprints.empty()) fingerprint=GetTransactionFingerprint(body);
        else fingerprint=fingerprints[index];
        TxInventoryKey key{uint8_t(evm),hash};auto now=GetSteadyTime();
        lock_guard lock(txInventoryMutex);
        if (txRecentBodies.count(key)) continue;
        auto found=txReceiving.find(key);
        if (found!=txReceiving.end()&&now>=found->second.created&&now-found->second.created>=txRequestLifetimeNs) {
            EraseTxReceivingLocked(key);found=txReceiving.end();
        }
        if (found==txReceiving.end()) {
            if (txReceiving.size()>=maxTxBodyRecords||nextTxBodyToken==UINT64_MAX) {txBodyWaitCapacity++;continue;}
            found=txReceiving.try_emplace(key).first;
            found->second.token=++nextTxBodyToken;found->second.created=now;
        }
        auto& receiving=found->second;
        if (receiving.bodies.count(fingerprint)) {txBodyWaitDuplicates++;continue;}
        if (receiving.bodies.size()>=maxTxBodyVariants||body.size()>maxTxBodyBytes-txReceivingBytes) {txBodyWaitCapacity++;continue;}
        auto inserted=receiving.bodies.emplace(fingerprint,body.size()).first;
        try { reservation->receipts.push_back({key,fingerprint,receiving.token,peer?peer->key:0,body.size()}); }
        catch (...) {receiving.bodies.erase(inserted);throw;}
        txReceivingBytes+=body.size();
    }
    return reservation;
}
nlohmann::json TxBodyWaitStats() {
    lock_guard lock(txInventoryMutex);
    return {{"tx_body_records",txReceiving.size()},{"tx_body_record_bytes",txReceivingBytes},
        {"tx_body_wait_skips",txBodyWaitSkips.load()},{"tx_body_wait_duplicates",txBodyWaitDuplicates.load()},
        {"tx_body_wait_timeouts",txBodyWaitTimeouts.load()},{"tx_body_wait_released",txBodyWaitReleased.load()},
        {"tx_body_wait_capacity",txBodyWaitCapacity.load()}};
}

// Caller holds txInventoryMutex. These are remote possession hints only; they
// never establish validity of local transaction bytes or satisfy a request.
TxPeerKnown& TxPeerStateLocked(const shared_ptr<Peer>& peer,uint64_t now) {
    auto found=txPeerKnown.find(peer->key);
    if (found==txPeerKnown.end()) {
        if (txPeerKnown.size()>=maxTxKnownPeers) {
            auto oldest=txPeerKnown.begin();
            for (auto it=txPeerKnown.begin();it!=txPeerKnown.end();++it)
                if (it->second.touched<oldest->second.touched) oldest=it;
            txKnownItems-=oldest->second.hashes.size()+oldest->second.notices.size();txPeerKnown.erase(oldest);
        }
        found=txPeerKnown.try_emplace(peer->key).first;
        found->second.peer=peer;
    }
    found->second.touched=now; return found->second;
}
void TxRememberPeerLocked(const shared_ptr<Peer>& peer,const TxInventoryKey& key,uint64_t now,bool notice=false) {
    if (!peer) return;
    auto& state=TxPeerStateLocked(peer,now);
    auto& hashes=notice?state.notices:state.hashes;auto& order=notice?state.noticeOrder:state.order;
    if (hashes.count(key)) return;
    if (order.size()>=maxTxKnownPerPeer) {hashes.erase(order.front());order.pop_front();txKnownItems--;}
    if (txKnownItems>=maxTxKnownItems) return;
    hashes.insert(key);order.push_back(key);txKnownItems++;
}
bool TxPeerKnowsLocked(const shared_ptr<Peer>& peer,const TxInventoryKey& key) {
    auto found=txPeerKnown.find(peer->key);
    return found!=txPeerKnown.end()&&found->second.hashes.count(key);
}
bool TxPeerNotifiedLocked(const shared_ptr<Peer>& peer,const TxInventoryKey& key) {
    auto found=txPeerKnown.find(peer->key);
    return found!=txPeerKnown.end()&&(found->second.hashes.count(key)||found->second.notices.count(key));
}
bool TxPeerKnowsTransaction(const shared_ptr<Peer>& peer,const array<uint8_t,32>& hash) {
    if (!peer) return false;
    lock_guard lock(txInventoryMutex);
    return TxPeerKnowsLocked(peer,TxInventoryKey{0,hash})||TxPeerKnowsLocked(peer,TxInventoryKey{1,hash});
}
void TxRememberBodyLocked(const shared_ptr<Peer>& source,const TxInventoryKey& key,span<const uint8_t> body,uint64_t now) {
    txPending.erase(key); EraseTxReceivingLocked(key); TxRememberPeerLocked(source,key,now);
    if (txRecentBodies.count(key)) return;
    while (!txRecentOrder.empty()&&(txRecentBodies.size()>=maxTxRecentCount||body.size()>maxTxRecentBytes-txRecentBytes)) {
        auto found=txRecentBodies.find(txRecentOrder.front());
        if (found!=txRecentBodies.end()) { txRecentBytes-=found->second->size();txRecentBodies.erase(found); }
        txRecentOrder.pop_front();
    }
    if (body.size()>maxTxRecentBytes) return;
    auto stored=make_shared<const vector<uint8_t>>(body.begin(),body.end());
    txRecentBytes+=stored->size(); txRecentBodies.emplace(key,std::move(stored)); txRecentOrder.push_back(key);
}
// These hooks must be called only after ProcessTxPackage/ProcessEvmTx accepted
// the bytes. TxKnown is safe because ProcessTxPackage verified the signature.
void RecordVerifiedNativeTransactions(const shared_ptr<Peer>& source,span<const uint8_t> data,span<const uint8_t> results) {
    if (data.size()%176||results.size()!=data.size()/176) throw invalid_argument("Transaction status count");
    auto now=GetSteadyTime(); lock_guard lock(txInventoryMutex);
    for (size_t i=0;i<results.size();++i) if (results[i]==TxAccepted||results[i]==TxKnown) {
        auto body=data.subspan(i*176,176); TxRememberBodyLocked(source,TransactionInventoryKey(body),body,now);
    }
}
void RecordVerifiedEvmTransaction(const shared_ptr<Peer>& source,span<const uint8_t> data) {
    auto key=TransactionInventoryKey(data,true); lock_guard lock(txInventoryMutex);
    TxRememberBodyLocked(source,key,data,GetSteadyTime());
}
shared_ptr<const vector<uint8_t>> ReadTxRelayBody(const TxInventoryKey& key) {
    {
        lock_guard lock(txInventoryMutex);
        auto found=txRecentBodies.find(key); if (found!=txRecentBodies.end()) return found->second;
    }
    lock_guard lock(txpoolMutex);
    auto found=txpool.find(key.hash); if (found==txpool.end()) return {};
    auto payload=evmPool.find(key.hash);
    if ((payload!=evmPool.end())!=(key.kind==1)) return {};
    auto body=make_shared<vector<uint8_t>>(found->second.begin(),found->second.end());
    if (payload!=evmPool.end()) body->insert(body->end(),payload->second.begin(),payload->second.end());
    return body;
}
// ProcessEvmTx returns false for both duplicates and invalid input. Comparing
// every byte with a previously verified body keeps those cases distinct.
bool ObserveKnownEvmTransaction(const shared_ptr<Peer>& source,span<const uint8_t> data) {
    if (data.size()<221||data.size()>221+maxEvmInput) return false;
    auto key=TransactionInventoryKey(data,true); auto known=ReadTxRelayBody(key);
    if (!known||known->size()!=data.size()||!equal(known->begin(),known->end(),data.begin())) return false;
    RecordVerifiedEvmTransaction(source,data); return true;
}
vector<bool> KnownTxInventory(span<const TxInventoryKey> keys) {
    vector<bool> known(keys.size(),false);
    {
        lock_guard lock(txInventoryMutex);
        for (size_t i=0;i<keys.size();++i) known[i]=txRecentBodies.count(keys[i]);
    }
    {
        lock_guard lock(txpoolMutex);
        for (size_t i=0;i<keys.size();++i) if (!known[i])
            known[i]=txpool.count(keys[i].hash)&&((evmPool.count(keys[i].hash)!=0)==(keys[i].kind==1));
    }
    return known;
}
void TxRememberSourceLocked(PendingTransaction& pending,const shared_ptr<Peer>& peer) {
    for (const auto& weak:pending.sources) if (auto source=weak.lock();source&&source->key==peer->key) return;
    for (auto& weak:pending.sources) {
        auto source=weak.lock(); if (!source||source->closePosted) { weak=peer;return; }
    }
    if (pending.sources.size()<maxTxSources) pending.sources.push_back(peer);
}
struct TxRequestBatch { shared_ptr<Peer> peer;vector<TxInventoryKey> keys; };
using TxRequestBatches=map<uint64_t,TxRequestBatch>;
void SelectTxRequestLocked(const TxInventoryKey& key,PendingTransaction& pending,uint64_t now,TxRequestBatches& requests) {
    if (TxBodyWaitingLocked(key,now)) {txBodyWaitSkips++;return;}
    if (pending.attempts>=6||(pending.lastRequest&&
        (now<pending.lastRequest||now-pending.lastRequest<txRequestRetryNs))) return;
    shared_ptr<Peer> chosen;
    for (size_t i=0;i<pending.sources.size();++i) {
        auto index=(pending.nextSource+i)%pending.sources.size();
        auto source=pending.sources[index].lock();if (!source||source->closePosted) continue;
        chosen=source;pending.nextSource=index+1;break;
    }
    if (!chosen) return;
    if (pending.attempts) txInventoryRetries++;
    pending.attempts++; pending.lastSource=chosen->key; pending.lastRequest=now;
    auto& batch=requests[chosen->key];batch.peer=chosen;batch.keys.push_back(key);
}
void SendTxRequestBatches(TxRequestBatches& requests) {
    for (auto& [id,batch]:requests) {
        // 128 items per request bounds each receiver's lookup work. Native and
        // EVM responses share the normal send-queue and reply-byte limits.
        for (size_t offset=0;offset<batch.keys.size();offset+=128) {
            auto keys=span<const TxInventoryKey>(batch.keys).subspan(offset,min(size_t(128),batch.keys.size()-offset));
            auto message=make_shared<const vector<uint8_t>>(GenerateMessage(54,EncodeTxInventory(keys)));
            if (TrySendGossip(batch.peer,std::move(message))) txInventoryRequests+=keys.size();
        }
    }
}
bool ReceiveTxInventory(const shared_ptr<Peer>& peer,span<const uint8_t> data) {
    vector<TxInventoryKey> keys;if (!DecodeTxInventory(data,keys)) return false;
    if (!txInventory||!peer||!peer->port||peer->closePosted) return true;
    peer->txInventory=true; // A valid inventory frame also proves protocol support.
    auto known=KnownTxInventory(keys);auto now=GetSteadyTime();TxRequestBatches requests;
    {
        lock_guard lock(txInventoryMutex);
        for (size_t i=0;i<keys.size();++i) {
            const auto& key=keys[i];TxRememberPeerLocked(peer,key,now);txInventoryAnnounced++;
            // Recheck the body cache under this lock: receipt may have raced the
            // pool snapshot and must not recreate a completed request.
            if (known[i]||txRecentBodies.count(key)) { txPending.erase(key);continue; }
            auto found=txPending.find(key);
            if (found==txPending.end()) {
                if (txPending.size()>=maxTxPending) {txInventoryDropped++;continue;}
                found=txPending.try_emplace(key).first;found->second.created=now;
            }
            TxRememberSourceLocked(found->second,peer);SelectTxRequestLocked(key,found->second,now,requests);
        }
    }
    SendTxRequestBatches(requests);return true;
}
void TxInventoryMaintenance() {
    auto now=GetSteadyTime();TxRequestBatches requests;
    {
        lock_guard lock(txInventoryMutex);
        for (auto it=txPeerKnown.begin();it!=txPeerKnown.end();) {
            auto peer=it->second.peer.lock();
            if (!peer||peer->closePosted) {txKnownItems-=it->second.hashes.size()+it->second.notices.size();it=txPeerKnown.erase(it);}else ++it;
        }
        for (auto it=txReceiving.begin();it!=txReceiving.end();) {
            if (now>=it->second.created&&now-it->second.created>=txRequestLifetimeNs) {
                auto key=it->first;++it;EraseTxReceivingLocked(key);
            } else ++it;
        }
        // Only 1024 due requests are scheduled on one tick, even when all
        // 16384 entries were announced simultaneously.
        size_t scheduled=0;
        for (auto it=txPending.begin();it!=txPending.end();) {
            auto& pending=it->second;
            if (now>=pending.created&&now-pending.created>=txRequestLifetimeNs) {
                it=txPending.erase(it);txInventoryExpired++;continue;
            }
            if (scheduled<maxTxInventoryItems) {
                auto attempts=pending.attempts;SelectTxRequestLocked(it->first,pending,now,requests);
                scheduled+=pending.attempts!=attempts;
            }
            ++it;
        }
    }
    SendTxRequestBatches(requests);
}
bool ReserveTxReplyBytes(const shared_ptr<Peer>& peer,size_t bytes,uint64_t now) {
    lock_guard lock(txInventoryMutex);auto& state=TxPeerStateLocked(peer,now);
    if (!state.replyAt) state.replyAt=now;
    if (now>=state.replyAt) {
        auto elapsed=min<uint64_t>(now-state.replyAt,1000000000ULL);
        state.replyTokens=min<uint64_t>(txReplyBurstBytes,state.replyTokens+elapsed*txReplyRateBytes/1000000000ULL);
        state.replyAt=now;
    }
    if (bytes>state.replyTokens) return false;
    state.replyTokens-=bytes;return true;
}
bool ServeTxRequests(const shared_ptr<Peer>& peer,span<const uint8_t> data) {
    vector<TxInventoryKey> keys;if (!DecodeTxInventory(data,keys)) return false;
    if (!txInventory||!peer||!peer->port||peer->closePosted) return true;
    peer->txInventory=true;
    size_t bytes=0;vector<uint8_t> native,evm;vector<TxInventoryKey> returned;
    for (const auto& key:keys) {
        auto body=ReadTxRelayBody(key);if (!body) continue;
        auto size=body->size()+(key.kind?4:0);
        if (size>maxTxRequestReplyBytes-bytes) break;
        if (!ReserveTxReplyBytes(peer,size,GetSteadyTime())) break;
        if (!key.kind) native.insert(native.end(),body->begin(),body->end());
        else {auto offset=evm.size();evm.resize(offset+4);WriteU32(evm.data()+offset,uint32_t(body->size()));evm.insert(evm.end(),body->begin(),body->end());}
        bytes+=size;returned.push_back(key);
    }
    bool nativeSent=false,evmSent=false;
    if (!native.empty()) nativeSent=TrySendGossip(peer,make_shared<const vector<uint8_t>>(GenerateMessage(10,native)));
    if (!evm.empty()) evmSent=TrySendGossip(peer,make_shared<const vector<uint8_t>>(GenerateMessage(18,evm)));
    if (nativeSent||evmSent) {
        lock_guard lock(txInventoryMutex);auto now=GetSteadyTime();
        for (const auto& key:returned) if (key.kind?evmSent:nativeSent) {TxRememberPeerLocked(peer,key,now);txInventoryReplies++;}
    }
    return true;
}
// New accepted native transactions are passed as a contiguous 176-byte batch;
// one complete EVM transaction is passed with evm=true. The caller still owns
// signature, account, nonce and pool-capacity validation.
void RelayAcceptedTransactions(const shared_ptr<Peer>& source,span<const uint8_t> data,bool evm=false) {
    if (data.empty()||(!evm&&data.size()%176)) return;
    vector<TxInventoryKey> keys;vector<span<const uint8_t>> bodies;
    if (evm) {keys.push_back(TransactionInventoryKey(data,true));bodies.push_back(data);}
    else for (size_t offset=0;offset<data.size();offset+=176) {auto body=data.subspan(offset,176);keys.push_back(TransactionInventoryKey(body));bodies.push_back(body);}
    {
        lock_guard lock(txInventoryMutex);auto now=GetSteadyTime();
        for (size_t i=0;i<keys.size();++i) TxRememberBodyLocked(source,keys[i],bodies[i],now);
    }
    array<uint8_t,32> selection{};crypto_generichash(selection.data(),32,data.data(),data.size(),nullptr,0);
    auto peers=SelectBroadcastPeers(source?PeerId(source):0,selection,txRelayPeers);
    // Rank the selected upgraded peers for full-body push. The payload changes
    // the choice between batches, and legacy full-body traffic is independent.
    static const array<uint8_t,32> pushSalt=[] {array<uint8_t,32> value{};randombytes_buf(value.data(),value.size());return value;}();
    vector<pair<array<uint8_t,16>,uint64_t>> pushRank;
    if (txInventory&&txPushPeers) for (const auto& peer:peers) if (peer->txInventory) {
        array<uint8_t,40> input{};copy(selection.begin(),selection.end(),input.begin());WriteU64(input.data()+32,peer->key);
        array<uint8_t,16> score{};crypto_generichash(score.data(),score.size(),input.data(),input.size(),pushSalt.data(),pushSalt.size());
        pushRank.emplace_back(score,peer->key);
    }
    auto pushCount=min(txPushPeers,pushRank.size());partial_sort(pushRank.begin(),pushRank.begin()+pushCount,pushRank.end());
    unordered_set<uint64_t> pushIds;for (size_t i=0;i<pushCount;++i) pushIds.insert(pushRank[i].second);
    for (const auto& peer:peers) {
        vector<TxInventoryKey> selected;vector<uint8_t> full;
        bool inventory=txInventory&&peer->txInventory&&!pushIds.count(peer->key);
        {
            lock_guard lock(txInventoryMutex);
            for (size_t i=0;i<keys.size();++i) {
                if (TxPeerNotifiedLocked(peer,keys[i])) {txInventoryKnownSkipped++;continue;}
                selected.push_back(keys[i]);if (!inventory) full.insert(full.end(),bodies[i].begin(),bodies[i].end());
            }
        }
        if (selected.empty()) continue;
        if (!inventory) {
            if (TrySendGossip(peer,make_shared<const vector<uint8_t>>(GenerateMessage(evm?17:10,full)))) {
                txInventoryBodySent+=selected.size();lock_guard lock(txInventoryMutex);auto now=GetSteadyTime();
                for (const auto& key:selected) TxRememberPeerLocked(peer,key,now);
            }
        } else {
            for (size_t offset=0;offset<selected.size();offset+=maxTxInventoryItems) {
                auto batch=span<const TxInventoryKey>(selected).subspan(offset,min(maxTxInventoryItems,selected.size()-offset));
                if (TrySendGossip(peer,make_shared<const vector<uint8_t>>(GenerateMessage(53,EncodeTxInventory(batch))))) {
                    txInventoryHashSent+=batch.size();
                    // Sending a hash does not establish remote possession.
                    // Keep this separate from the prefilled-block callback.
                    lock_guard lock(txInventoryMutex);auto now=GetSteadyTime();
                    for (const auto& key:batch) TxRememberPeerLocked(peer,key,now,true);
                }
            }
        }
    }
}
#endif
