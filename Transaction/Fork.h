#ifndef CHAIN_FORK_H
#define CHAIN_FORK_H
using ForkEntry=pair<Block,vector<uint8_t>>;
map<array<uint8_t,32>,shared_ptr<const ForkEntry>> forkBlocks;
mutex forkMutex;
size_t forkBytes=0;
atomic<bool> forkReady{false};
constexpr size_t maxForkBytes=64*1024*1024,maxForkBlocks=256;
bool QueueForkBlock(Block block,vector<uint8_t> data) {
    {
        lock_guard lock(forkMutex);
        if (forkBlocks.count(block.hash)) return false;
        while (!forkBlocks.empty()&&(forkBlocks.size()>=maxForkBlocks||forkBytes+data.size()>maxForkBytes)) {
            auto oldest=min_element(forkBlocks.begin(),forkBlocks.end(),[](const auto& a,const auto& b) { return a.second->first.height<b.second->first.height; });
            if (oldest->second->first.height>block.height) return false;
            forkBytes-=oldest->second->second.size(); forkBlocks.erase(oldest);
        }
        auto hash=block.hash; forkBytes+=data.size();
        forkBlocks.emplace(hash,make_shared<ForkEntry>(std::move(block),std::move(data)));
        forkReady=true;
    }
    WakeMainLoop(); return true;
}
vector<uint8_t> FindForkBlock(const array<uint8_t,32>& hash) {
    auto saved=DBReadBlockByHash(hash);
    if (!saved.empty()) return saved;
    lock_guard lock(forkMutex);
    auto found=forkBlocks.find(hash);
    return found==forkBlocks.end()?vector<uint8_t>{}:found->second->second;
}
void RemoveForkBlock(const array<uint8_t,32>& hash) {
    lock_guard lock(forkMutex);
    auto found=forkBlocks.find(hash);
    if (found!=forkBlocks.end()) { forkBytes-=found->second->second.size(); forkBlocks.erase(found); }
}
array<uint8_t,32> FindMissingParent(array<uint8_t,32> hash) {
    for (size_t i=0;i<256;i++) {
        auto data=FindForkBlock(hash);
        if (data.empty()) return hash;
        auto height=ReadU64(data.data()+64);
        if (height<=1||DBReadBlockByHeight(to_string(height))==data) return {};
        memcpy(hash.data(),data.data(),32);
    }
    return {};
}
// 调用方持有提交锁；undo 同时恢复批次视图和内存状态。
void UndoForkBlock(const array<uint8_t,32>& hash) {
    string saved;
    if (!DBGet(DBHashKey("undo/",hash),saved)) throw runtime_error("Missing block undo data");
    rocksdb::WriteBatch undo(saved);
    struct Restore:rocksdb::WriteBatch::Handler {
        void Update(const rocksdb::Slice& key,const rocksdb::Slice* value) {
            if (key.starts_with("user/")&&key.size()==37) {
                array<uint8_t,32> address; memcpy(address.data(),key.data()+5,32);
                if (!value) users.erase(address);
                else {
                    if (value->size()!=16) throw runtime_error("Invalid account undo");
                    auto data=reinterpret_cast<const uint8_t*>(value->data());
                    users[address]={ReadU64(data),ReadU64(data+8)};
                }
            } else if (key.starts_with("evm/")&&key.size()==24) {
                evmc::address address; memcpy(address.bytes,key.data()+4,20);
                if (!value) evmState.get_accounts().erase(address);
                else evmState.get_accounts()[address]=UnserializeEvmAccount(value->ToString());
            }
        }
        void Put(const rocksdb::Slice& key,const rocksdb::Slice& value) override { branchBatch->Put(key,value); Update(key,&value); }
        void Delete(const rocksdb::Slice& key) override { branchBatch->Delete(key); Update(key,nullptr); }
    } restore;
    CheckDBStatus(undo.Iterate(&restore));
    userBurned=DBReadNumber("UserBurned");
}
// 更高的累计有效交易数优先；同数比较高度，最后选择链头哈希较小者。
// 返回 1 表示切换成功，0 表示等待父块/排名较低，-1 表示分支无效。
int AdoptFork(const ForkEntry& candidate,vector<vector<uint8_t>>& detached) {
    unique_lock lock(dbCommitMutex);
    vector<ForkEntry> branch;
    auto current=candidate;
    uint64_t transactions=0;
    while (true) {
        auto canonical=DBReadBlockByHeight(to_string(current.first.height));
        if (canonical==current.second) break;
        if (branch.size()>=256||current.first.height<=1||UINT64_MAX-transactions<current.first.txNum) return -1;
        transactions+=current.first.txNum; branch.push_back(std::move(current));
        auto parent=FindForkBlock(branch.back().first.previousHash);
        if (parent.empty()) return 0;
        Block parsed;
        if (!UnSerializeBlock(parent,parsed)||parsed.height+1!=branch.back().first.height||parsed.hash!=branch.back().first.previousHash) return -1;
        current={std::move(parsed),std::move(parent)};
    }
    if (branch.empty()) return -1;
    auto common=current.first.height,oldHeight=DBReadBlockHeight();
    if (oldHeight-common>256) return -1;
    auto prefix=DBReadNumber(DBHashKey("score/",current.first.hash));
    if (UINT64_MAX-prefix<transactions) return -1;
    auto score=prefix+transactions,oldScore=DBReadNumber("ChainTx");
    auto head=DBReadCurrentBlock();
    if (score<oldScore||(score==oldScore&&(candidate.first.height<oldHeight||(candidate.first.height==oldHeight&&candidate.first.hash>=head)))) return 0;
    auto savedUsers=users;
    auto savedEvm=CopyEvmState(evmState);
    auto savedBurned=userBurned;
    rocksdb::WriteBatchWithIndex changes(rocksdb::BytewiseComparator(),0,true);
    branchBatch=&changes;
    bool valid=true;
    try {
        while (DBReadBlockHeight()>common) {
            auto old=DBReadCurrentBlock();
            detached.push_back(DBReadBlockByHash(old));
            UndoForkBlock(old);
        }
        for (auto it=branch.rbegin();it!=branch.rend();++it) if (!DBCommitBlockLocked(it->first.hash,it->second)) { valid=false; break; }
        branchBatch=nullptr;
        if (valid) DBWriteBatch(*changes.GetWriteBatch());
    } catch (...) {
        branchBatch=nullptr; users=std::move(savedUsers); evmState=std::move(savedEvm); userBurned=savedBurned; detached.clear(); throw;
    }
    if (!valid) {
        users=std::move(savedUsers); evmState=std::move(savedEvm); userBurned=savedBurned; detached.clear(); return -1;
    }
    if (oldHeight>common) reorgCount++;
    committedTx+=score-oldScore; lastCommitTime=GetSteadyTime();
    return 1;
}
bool ProcessForks() {
    if (!forkReady.exchange(false)) return false;
    vector<shared_ptr<const ForkEntry>> candidates;
    { lock_guard lock(forkMutex); for (const auto& [hash,entry]:forkBlocks) candidates.push_back(entry); }
    bool adopted=false;
    for (const auto& entry:candidates) {
        vector<vector<uint8_t>> detached;
        auto result=AdoptFork(*entry,detached);
        if (result<0) { RemoveForkBlock(entry->first.hash); continue; }
        if (!result) continue;
        adopted=true;
        RemoveForkBlock(entry->first.hash);
        vector<vector<uint8_t>> pending;
        {
            shared_lock commitLock(dbCommitMutex);
            lock_guard lock(txpoolMutex);
            for (const auto& [hash,tx]:txpool) {
                vector<uint8_t> data(tx.begin(),tx.end());
                if (auto found=evmPool.find(hash);found!=evmPool.end()) data.insert(data.end(),found->second.begin(),found->second.end());
                pending.push_back(std::move(data));
            }
            txpool.clear(); txUserPool.clear(); evmPool.clear(); evmPoolBytes=0; readyPoolTx=0;
        }
        for (const auto& data:detached) {
            Block block=UnSerializeBlock(data);
            for (size_t i=0;i<block.txs.size();i++) {
                vector<uint8_t> tx(block.txs[i].begin(),block.txs[i].end());
                if (block.evm.count(i)) tx.insert(tx.end(),block.evm.at(i).begin(),block.evm.at(i).end());
                pending.push_back(std::move(tx));
            }
        }
        for (const auto& tx:pending) { if (tx.size()==176) ProcessTxPackage(tx); else ProcessEvmTx(tx); }
        {
            lock_guard lock(blockBufferLock);
            for (auto& slot:BlockBuffer) slot={};
            epoch=DBReadBlockHeight()+1; blockReady=false;
        }
        forkReady=true;
    }
    return adopted;
}
#endif
