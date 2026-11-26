#ifndef CHAIN_TX_WORK_H
#define CHAIN_TX_WORK_H

// Included after TransactionRelay.h. The key is the locally computed hash of
// all transaction bytes, including its signature and complete EVM input.
constexpr size_t maxTxWorkRecords=65536,maxTxWorkBytes=48*1024*1024;
constexpr size_t maxTxWorkReplies=131072,maxTxWorkSources=16;
atomic<uint64_t> txWorkMerged{0},txWorkPoolKnown{0},txWorkCapacity{0};
atomic<size_t> txWorkWaiters{0};
mutex txWorkMutex;
struct TxWork;
unordered_map<array<uint8_t,32>,shared_ptr<TxWork>,GetMapHash> txWork;
size_t txWorkBytes=0;

struct TxWorkInput {
    size_t offset=0,bytes=0;
    array<uint8_t,32> fingerprint{};
};
bool ParseTxWork(uint8_t type,span<const uint8_t> data,vector<TxWorkInput>& inputs) {
    inputs.clear();
    auto append=[&](size_t offset,size_t bytes) {
        inputs.push_back({offset,bytes,GetTransactionFingerprint(data.subspan(offset,bytes))});
    };
    if (type==1||type==10||type==44) {
        if (data.empty()||data.size()%176||data.size()/176>maxProtocolTx) return false;
        for (size_t offset=0;offset<data.size();offset+=176) append(offset,176);
    } else if (type==17) {
        if (data.size()<221||data.size()>221+maxEvmInput) return false;
        append(0,data.size());
    } else if (type==18) {
        size_t offset=0;
        while (offset<data.size()) {
            if (data.size()-offset<4) return false;
            auto bytes=ReadU32(data.data()+offset);offset+=4;
            if (bytes<221||bytes>221+maxEvmInput||bytes>data.size()-offset||inputs.size()>=maxProtocolTx) return false;
            append(offset,bytes);offset+=bytes;
        }
        if (inputs.empty()) return false;
    } else return false;
    return true;
}

struct TxWorkReply {
    shared_ptr<Peer> peer;
    uint64_t order=0;
    mutex lock;
    vector<uint8_t> results;
    size_t remaining=0,charged=0;
    ~TxWorkReply() { txWorkWaiters-=charged; }
    void Set(size_t index,uint8_t status) {
        vector<uint8_t> complete;
        {
            lock_guard guard(lock);
            if (index>=results.size()||results[index]!=255) return;
            results[index]=status;
            if (--remaining==0) complete=std::move(results);
        }
        if (!complete.empty()) SendAdmission(peer,order,std::move(complete));
    }
};
shared_ptr<TxWorkReply> ReserveTxWorkReply(const shared_ptr<Peer>& peer,uint64_t order,size_t count) {
    auto reply=make_shared<TxWorkReply>();
    auto current=txWorkWaiters.load();
    do {
        if (count>maxTxWorkReplies||current>maxTxWorkReplies-count) { txWorkCapacity++;return {}; }
    } while (!txWorkWaiters.compare_exchange_weak(current,current+count));
    reply->charged=count;reply->peer=peer;reply->order=order;
    reply->results.assign(count,255);reply->remaining=count;
    return reply;
}
struct TxWorkWaiter {
    shared_ptr<TxWorkReply> reply;
    size_t index=0;
    bool owner=false;
};
struct TxWork {
    array<uint8_t,32> fingerprint{};
    TxInventoryKey key;
    size_t bytes=0;
    uint64_t merged=0;
    bool registered=false,hasFollowers=false;
    vector<TxWorkWaiter> waiters;
    vector<weak_ptr<Peer>> sources;
};

void CompleteTxWorks(span<const shared_ptr<TxWork>> works,span<const uint8_t> statuses,
        const shared_ptr<Peer>& recordedSource={}) {
    if (works.size()!=statuses.size()) throw invalid_argument("Transaction completion count");
    if (works.empty()) return;
    struct Completed {
        shared_ptr<TxWork> work;
        uint8_t status;
        vector<TxWorkWaiter> waiters;
        vector<weak_ptr<Peer>> sources;
    };
    vector<Completed> completed;completed.reserve(works.size());
    {
        lock_guard lock(txWorkMutex);
        for (size_t i=0;i<works.size();++i) {
            const auto& work=works[i];if (!work->registered) continue;
            auto found=txWork.find(work->fingerprint);
            if (found==txWork.end()||found->second.get()!=work.get()) continue;
            work->registered=false;txWorkBytes-=work->bytes;
            completed.push_back({work,statuses[i],std::move(work->waiters),std::move(work->sources)});
            txWork.erase(found);
        }
    }
    // No registry lock is held while updating possession or sending receipts.
    if (completed.empty()) return;
    {
        lock_guard lock(txInventoryMutex);auto now=GetSteadyTime();
        for (const auto& item:completed) if (item.status==TxAccepted||item.status==TxKnown) {
            duplicateTx+=item.work->merged;
            for (const auto& weak:item.sources) if (auto peer=weak.lock();peer&&peer!=recordedSource)
                TxRememberPeerLocked(peer,item.work->key,now);
        }
    }
    for (auto& item:completed) for (auto& waiter:item.waiters)
        waiter.reply->Set(waiter.index,item.status==TxAccepted&&!waiter.owner?uint8_t(TxKnown):item.status);
}
void CompleteTxWork(const shared_ptr<TxWork>& work,uint8_t status) {
    CompleteTxWorks(span<const shared_ptr<TxWork>>(&work,1),span<const uint8_t>(&status,1));
}
struct TxWorkBatch {
    vector<shared_ptr<TxWork>> owners;
    vector<size_t> indices;
    ~TxWorkBatch() {
        // Cancellation, post failure, and an unwound worker all release the
        // active entries; Busy allows a submitter to retry on another source.
        try { vector<uint8_t> cancelled(owners.size(),TxBusy);CompleteTxWorks(owners,cancelled); }
        catch (...) { nodeFailed=true;nodeRunning=false; }
    }
    bool CancelIfUnshared() {
        vector<TxWorkWaiter> cancelled;
        {
            lock_guard lock(txWorkMutex);
            for (const auto& owner:owners) if (owner->hasFollowers) return false;
            // Remove entries under the same lock as the follower check. A new
            // sender then retains its own body instead of joining a cancelled task.
            for (const auto& owner:owners) if (owner->registered) {
                auto found=txWork.find(owner->fingerprint);
                if (found==txWork.end()||found->second.get()!=owner.get()) continue;
                cancelled.insert(cancelled.end(),make_move_iterator(owner->waiters.begin()),make_move_iterator(owner->waiters.end()));
                owner->waiters.clear();owner->sources.clear();owner->registered=false;
                txWorkBytes-=owner->bytes;txWork.erase(found);
            }
        }
        for (auto& waiter:cancelled) waiter.reply->Set(waiter.index,TxBusy);
        return true;
    }
};
bool IsVerifiedPoolTransaction(span<const uint8_t> body,const array<uint8_t,32>& fingerprint,bool evm) {
    auto& cache=verifiedTransactions[fingerprint[0]%verifiedTransactions.size()];
    {
        unique_lock lock(cache.lock,try_to_lock);
        if (!lock.owns_lock()||!cache.hashes.count(fingerprint)) return false;
    }
    array<uint8_t,32> hash;memcpy(hash.data(),body.data()+80,32);
    unique_lock lock(txpoolMutex,try_to_lock);
    return lock.owns_lock()&&txpool.count(hash)&&((evmPool.count(hash)!=0)==evm);
}

// Register the whole batch at once. A batch waiting for registry capacity owns
// no entries, so two oversized batches cannot retain each other's free space.
shared_ptr<TxWorkBatch> TryReserveTxWork(const shared_ptr<Peer>& peer,bool evm,
        span<const TxWorkInput> inputs,span<const uint8_t> data,const shared_ptr<TxWorkReply>& reply={}) {
    auto batch=make_shared<TxWorkBatch>();
    vector<bool> known(inputs.size(),false),owner(inputs.size(),false);
    vector<shared_ptr<TxWork>> entries(inputs.size());
    unordered_map<array<uint8_t,32>,shared_ptr<TxWork>,GetMapHash> fresh;
    for (size_t i=0;i<inputs.size();++i) {
        const auto& input=inputs[i];
        known[i]=IsVerifiedPoolTransaction(data.subspan(input.offset,input.bytes),input.fingerprint,evm);
    }
    {
        lock_guard lock(txWorkMutex);
        size_t bytes=0;
        for (size_t i=0;i<inputs.size();++i) {
            if (known[i]) continue;
            const auto& input=inputs[i];
            auto found=txWork.find(input.fingerprint);
            if (found!=txWork.end()) entries[i]=found->second;
            else if (auto existing=fresh.find(input.fingerprint);existing!=fresh.end()) entries[i]=existing->second;
            else {
                auto work=make_shared<TxWork>();work->fingerprint=input.fingerprint;work->bytes=input.bytes;
                work->key.kind=evm;memcpy(work->key.hash.data(),data.data()+input.offset+80,32);
                entries[i]=work;fresh.emplace(input.fingerprint,work);bytes+=input.bytes;owner[i]=true;
                batch->owners.push_back(work);batch->indices.push_back(i);
            }
        }
        if (fresh.size()>maxTxWorkRecords-txWork.size()||bytes>maxTxWorkBytes-txWorkBytes) {
            txWorkCapacity++;return {};
        }
        for (auto& work:batch->owners) {
            txWork.emplace(work->fingerprint,work);work->registered=true;txWorkBytes+=work->bytes;
        }
        for (size_t i=0;i<inputs.size();++i) {
            if (known[i]) continue;
            auto& work=entries[i];
            if (!owner[i]) {
                work->hasFollowers=true;work->merged++;txWorkMerged++;receivedTx++;
                if (!produceBlocks||relayTransactions) skippedRelayTransactions++;
            }
            if (reply) work->waiters.push_back({reply,i,owner[i]});
            if (peer) {
                bool present=false;
                for (const auto& weak:work->sources) if (auto source=weak.lock();source&&source->key==peer->key) {present=true;break;}
                if (!present&&work->sources.size()<maxTxWorkSources) work->sources.push_back(peer);
            }
        }
    }
    // A verified fingerprint plus a current pool entry is sufficient for
    // Known. A verified transaction absent from the pool still checks state.
    {
        lock_guard lock(txInventoryMutex);auto now=GetSteadyTime();
        for (size_t i=0;i<inputs.size();++i) if (known[i]) {
            const auto& input=inputs[i];auto body=data.subspan(input.offset,input.bytes);
            TxRememberBodyLocked(peer,TransactionInventoryKey(body,evm),body,now);
            txWorkPoolKnown++;receivedTx++;duplicateTx++;
            if (!produceBlocks||relayTransactions) skippedRelayTransactions++;
        }
    }
    if (reply) for (size_t i=0;i<inputs.size();++i) if (known[i]) reply->Set(i,TxKnown);
    return batch;
}
nlohmann::json TxWorkStats() {
    lock_guard lock(txWorkMutex);
    return {{"tx_work_active",txWork.size()},{"tx_work_bytes",txWorkBytes},{"tx_work_waiters",txWorkWaiters.load()},
        {"tx_work_merged",txWorkMerged.load()},{"tx_work_pool_known",txWorkPoolKnown.load()},
        {"tx_work_capacity",txWorkCapacity.load()}};
}
#endif
