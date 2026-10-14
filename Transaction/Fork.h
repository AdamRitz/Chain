#ifndef CHAIN_FORK_H
#define CHAIN_FORK_H
using ForkEntry=pair<Block,vector<uint8_t>>;
map<array<uint8_t,32>,shared_ptr<const ForkEntry>> forkBlocks;
mutex forkMutex;
struct ForkCheck {
    uint64_t score=0,version=0;
    array<uint8_t,32> missing{};
    bool ranked=false;
};
unordered_map<array<uint8_t,32>,ForkCheck,GetMapHash> forkChecks;
bool HasForkBlock(const array<uint8_t,32>& hash,const vector<uint8_t>& data) {
    lock_guard lock(forkMutex);auto found=forkBlocks.find(hash);
    return found!=forkBlocks.end()&&found->second->second==data;
}
size_t forkBytes=0;
atomic<bool> forkReady{false};
atomic<uint64_t> forkAttempts{0},forkWalkNs{0},forkLockWaitNs{0},forkBackupKeys{0},forkBackupBytes{0},replayedTx{0};
atomic<uint64_t> forkRankSkips{0},forkParentSkips{0},branchAppendTx{0},forkReexecuteTx{0},groupedCommitBlocks{0};
atomic<uint64_t> forkSchedules{0},forkSelections{0},forkInvalidCandidates{0},groupedCommitBatches{0};
vector<array<uint8_t,32>> lastBranchUsers;
constexpr size_t maxForkBytes=512*1024*1024,maxForkBlocks=256;
bool QueueForkBlock(Block block,vector<uint8_t> data,bool notify=true) {
    {
        lock_guard lock(forkMutex);
        if (forkBlocks.count(block.hash)) return false;
        while (!forkBlocks.empty()&&(forkBlocks.size()>=maxForkBlocks||forkBytes+data.size()>maxForkBytes)) {
            auto oldest=min_element(forkBlocks.begin(),forkBlocks.end(),[](const auto& a,const auto& b) { return a.second->first.height<b.second->first.height; });
            if (oldest->second->first.height>block.height) return false;
            forkBytes-=oldest->second->second.size(); forkChecks.erase(oldest->first); forkBlocks.erase(oldest);
        }
        auto hash=block.hash; forkBytes+=data.size();
        forkBlocks.emplace(hash,make_shared<ForkEntry>(std::move(block),std::move(data)));
        if (notify) forkReady=true;
    }
    if (notify) WakeMainLoop();
    return true;
}
shared_ptr<const ForkEntry> FindForkEntry(const array<uint8_t,32>& hash) {
    { lock_guard lock(forkMutex); auto found=forkBlocks.find(hash); if (found!=forkBlocks.end()) return found->second; }
    auto data=DBReadBlockByHash(hash); if (data.empty()) return {};
    Block block;
    if (!UnSerializeBlock(data,block)||block.hash!=hash) throw runtime_error("Invalid saved fork block");
    return make_shared<ForkEntry>(std::move(block),std::move(data));
}
bool KnowsForkBlock(const array<uint8_t,32>& hash) {
    { lock_guard lock(forkMutex); if (forkBlocks.count(hash)) return true; }
    return !DBReadBlockHeader(hash).empty();
}
vector<uint8_t> FindForkBlock(const array<uint8_t,32>& hash) {
    auto entry=FindForkEntry(hash); return entry?entry->second:vector<uint8_t>{};
}
void RemoveForkBlock(const array<uint8_t,32>& hash) {
    lock_guard lock(forkMutex);
    auto found=forkBlocks.find(hash);
    if (found!=forkBlocks.end()) { forkBytes-=found->second->second.size(); forkBlocks.erase(found); }
    forkChecks.erase(hash);
}
bool HasForkChild(const array<uint8_t,32>& hash) {
    lock_guard lock(forkMutex);
    for (const auto& [key,entry]:forkBlocks) if (entry->first.previousHash==hash) return true;
    return false;
}
array<uint8_t,32> FindMissingParent(array<uint8_t,32> hash) {
    shared_lock lock(dbCommitMutex);
    for (size_t i=0;i<256;i++) {
        shared_ptr<const ForkEntry> entry;
        { lock_guard forkLock(forkMutex); auto found=forkBlocks.find(hash); if (found!=forkBlocks.end()) entry=found->second; }
        auto header=entry?vector<uint8_t>{}:DBReadBlockHeader(hash);
        if (!entry&&header.empty()) return hash;
        auto height=entry?entry->first.height:ReadU64(header.data()+64);
        if (height<=1||DBReadBlockHashByHeight(height)==hash) return {};
        if (entry) hash=entry->first.previousHash; else memcpy(hash.data(),header.data(),32);
    }
    return {};
}
// 调用方持有提交锁；undo 同时恢复批次视图和内存状态。
void UndoForkBlock(const array<uint8_t,32>& hash) {
    string saved;
    if (!DBGet(DBHashKey("undo/",hash),saved)) throw runtime_error("Missing block undo data");
    rocksdb::WriteBatch undo(saved);
    struct Restore:rocksdb::WriteBatch::Handler {
        void Put(const rocksdb::Slice& key,const rocksdb::Slice& value) override { branchBatch->Put(key,value); ApplyMemoryKey(key,&value); }
        void Delete(const rocksdb::Slice& key) override { branchBatch->Delete(key); ApplyMemoryKey(key,nullptr); }
    } restore;
    SaveBranchMemory(undo);
    CheckDBStatus(undo.Iterate(&restore));
    InvalidateExecutionCache();
    userBurned=DBReadNumber("UserBurned");
}
struct RankedFork {
    shared_ptr<const ForkEntry> tip;
    vector<shared_ptr<const ForkEntry>> branch;
    uint64_t transactions=0,common=0,score=0,version=0;
    array<uint8_t,32> commonHash{};
};
// 先准备完整父链和累计排名，提交时复核同一状态版本。
int RankFork(shared_ptr<const ForkEntry> candidate,RankedFork& rank) {
    forkAttempts++; rank={}; rank.tip=std::move(candidate);
    auto& branch=rank.branch; auto& transactions=rank.transactions;
    struct WalkTime { uint64_t start=GetSteadyTime(); ~WalkTime() { forkWalkNs+=GetSteadyTime()-start; } } measured;
    shared_lock lock(dbCommitMutex);
    rank.version=dbStateVersion;
    auto current=rank.tip;
    while (true) {
        if (DBReadBlockHashByHeight(current->first.height)==current->first.hash) {
            rank.common=current->first.height; rank.commonHash=current->first.hash; break;
        }
        if (branch.size()>=256||current->first.height<=1||UINT64_MAX-transactions<current->first.txNum) return -1;
        transactions+=current->first.txNum; branch.push_back(current);
        // 先识别主链父块，查询共同祖先只需高度索引和小块头。
        auto parentHeight=current->first.height-1;
        if (DBReadBlockHashByHeight(parentHeight)==current->first.previousHash) {
            rank.common=parentHeight; rank.commonHash=current->first.previousHash; break;
        }
        auto parent=FindForkEntry(current->first.previousHash);
        if (!parent) {
            lock_guard forkLock(forkMutex);
            if (forkBlocks.count(rank.tip->first.hash)) forkChecks[rank.tip->first.hash]={0,rank.version,current->first.previousHash,false};
            return 0;
        }
        if (parent->first.height+1!=current->first.height) return -1;
        current=std::move(parent);
    }
    if (branch.empty()) return -1;
    auto oldHeight=DBReadBlockHeight(); if (oldHeight<rank.common||oldHeight-rank.common>256) return -1;
    auto prefix=DBReadNumber(DBHashKey("score/",rank.commonHash));
    if (UINT64_MAX-prefix<transactions) return -1;
    rank.score=prefix+transactions; auto oldScore=DBReadNumber("ChainTx"); auto head=DBReadCurrentBlock();
    { lock_guard forkLock(forkMutex);
      if (forkBlocks.count(rank.tip->first.hash)) forkChecks[rank.tip->first.hash]={rank.score,rank.version,{},true}; }
    if (rank.score<oldScore||(rank.score==oldScore&&(rank.tip->first.height<oldHeight||(rank.tip->first.height==oldHeight&&rank.tip->first.hash>=head)))) return 0;
    return 1;
}
// 更高的累计有效交易数优先；同数比较高度，最后选择链头哈希较小者。
int AdoptFork(const RankedFork& rank,vector<vector<uint8_t>>& detached) {
    const auto& branch=rank.branch;
    auto common=rank.common,transactions=rank.transactions,score=rank.score;
    auto waitStart=GetSteadyTime(); unique_lock lock(dbCommitMutex); forkLockWaitNs+=GetSteadyTime()-waitStart;
    if (rank.version!=dbStateVersion) { forkReady=true; return 0; }
    auto oldHeight=DBReadBlockHeight(),oldScore=DBReadNumber("ChainTx");
    if (oldHeight>common) InvalidateExecutionCache();
    BranchMemory backup; auto savedBurned=userBurned;
    rocksdb::WriteBatchWithIndex changes(rocksdb::BytewiseComparator(),0,true);
    branchBatch=&changes; branchMemory=&backup;
    bool valid=true;
    try {
        while (DBReadBlockHeight()>common) {
            auto old=DBReadCurrentBlock(); detached.push_back(DBReadBlockByHash(old)); UndoForkBlock(old);
        }
        for (auto it=branch.rbegin();it!=branch.rend();++it) {
            replayedTx+=(*it)->first.txNum;
            if (!DBCommitBlockLocked((*it)->first.hash,(*it)->second)) { valid=false; break; }
        }
        branchBatch=nullptr; branchMemory=nullptr;
        forkBackupKeys+=backup.keys.size(); forkBackupBytes+=backup.saved.Data().size();
        if (valid) DBWriteBatch(*changes.GetWriteBatch());
    } catch (...) {
        branchBatch=nullptr; branchMemory=nullptr; backup.Restore(); userBurned=savedBurned; detached.clear(); InvalidateExecutionCache(); throw;
    }
    if (!valid) { backup.Restore(); userBurned=savedBurned; detached.clear(); InvalidateExecutionCache(); return -1; }
    PublishPoolUsers(nullptr,&backup.keys);
    PublishChainHead();
    lastBranchUsers.clear();
    for (const auto& key:backup.keys) if (key.starts_with("user/")&&key.size()==37) {
        array<uint8_t,32> address; memcpy(address.data(),key.data()+5,32); lastBranchUsers.push_back(address);
    }
    if (oldHeight>common) {
        forkReexecuteTx+=transactions;
        reorgCount++; auto depth=oldHeight-common,previous=maxReorgDepth.load();
        while (previous<depth&&!maxReorgDepth.compare_exchange_weak(previous,depth)) {}
    } else {
        branchAppendTx+=transactions;
        if (branch.size()>1) { groupedCommitBlocks+=branch.size(); groupedCommitBatches++; }
    }
    committedTx+=score-oldScore; lastCommitTime=GetSteadyTime(); return 1;
}
int AdoptFork(const ForkEntry& candidate,vector<vector<uint8_t>>& detached) {
    RankedFork rank;
    auto result=RankFork(shared_ptr<const ForkEntry>(&candidate,[](const ForkEntry*){}),rank);
    return result==1?AdoptFork(rank,detached):result;
}
bool ProcessForks() {
    if (!forkReady.exchange(false)) return false;
    forkSchedules++;
    vector<shared_ptr<const ForkEntry>> candidates;
    { lock_guard lock(forkMutex); for (const auto& [hash,entry]:forkBlocks) candidates.push_back(entry); }
    unordered_set<array<uint8_t,32>,GetMapHash> parents;
    for (const auto& entry:candidates) parents.insert(entry->first.previousHash);
    vector<RankedFork> ranked;
    uint64_t height,score,version; array<uint8_t,32> head;
    auto readHead=[&] {
        shared_lock stateLock(dbCommitMutex);
        height=DBReadBlockHeight(); score=DBReadNumber("ChainTx"); version=dbStateVersion; head=DBReadCurrentBlock();
    };
    readHead();
    for (const auto& entry:candidates) {
        if (parents.count(entry->first.hash)) continue;
        {
            lock_guard forkLock(forkMutex); auto found=forkChecks.find(entry->first.hash);
            if (found!=forkChecks.end()) {
                const auto& checked=found->second;
                // 累计交易数属于不可变父链；链的排名只会提高，较低排名可直接复用。
                if (checked.ranked&&(checked.score<score||(checked.score==score&&
                    (entry->first.height<height||(entry->first.height==height&&entry->first.hash>=head))))) {
                    forkRankSkips++; continue;
                }
                if (!checked.ranked&&checked.version==version&&!forkBlocks.count(checked.missing)) { forkParentSkips++; continue; }
            }
        }
        RankedFork rank; auto result=RankFork(entry,rank);
        if (result<0) { RemoveForkBlock(entry->first.hash); forkReady=true; continue; }
        if (result) ranked.push_back(std::move(rank));
    }
    sort(ranked.begin(),ranked.end(),[](const auto& left,const auto& right) {
        if (left.score!=right.score) return left.score>right.score;
        if (left.tip->first.height!=right.tip->first.height) return left.tip->first.height>right.tip->first.height;
        return left.tip->first.hash<right.tip->first.hash;
    });
    bool refresh=false;
    for (auto& rank:ranked) {
        auto entry=rank.tip;
        if (refresh) {
            auto result=RankFork(entry,rank);
            if (result<0) { RemoveForkBlock(entry->first.hash); continue; }
            if (!result) continue;
        }
        vector<vector<uint8_t>> detached;
        auto result=AdoptFork(rank,detached);
        if (result<0) { forkInvalidCandidates++; RemoveForkBlock(entry->first.hash); forkReady=true; refresh=true; continue; }
        if (!result) return false;
        forkSelections++;
        RemoveForkBlock(entry->first.hash);
        if (!detached.empty()) RefreshTxPool(lastBranchUsers);
        else {
            shared_lock stateLock(dbCommitMutex); lock_guard poolLock(txpoolMutex);
            for (const auto& key:lastBranchUsers) PruneUserPool(key,GetUser(users,key).nonce);
        }
        for (const auto& data:detached) {
            Block block=UnSerializeBlock(data);
            vector<uint8_t> native;
            vector<vector<uint8_t>> contracts;
            {
                shared_lock lock(dbCommitMutex);
                for (size_t i=0;i<block.txs.size();i++) {
                    const auto& tx=block.txs[i]; array<uint8_t,32> key; memcpy(key.data(),tx.data(),32);
                    if (ReadU64(tx.data()+72)<=GetUser(users,key).nonce) continue;
                    if (auto payload=block.evm.find(i);payload!=block.evm.end()) {
                        contracts.emplace_back(tx.begin(),tx.end()); contracts.back().insert(contracts.back().end(),payload->second.begin(),payload->second.end());
                    } else native.insert(native.end(),tx.begin(),tx.end());
                }
            }
            if (!native.empty()) ProcessTxPackage(native);
            for (const auto& tx:contracts) ProcessEvmTx(tx);
        }
        {
            lock_guard lock(blockBufferLock);
            for (auto& slot:BlockBuffer) slot={};
            epoch=DBReadBlockHeight()+1; blockReady=false;
        }
        forkReady=true;
        return true;
    }
    return false;
}
#endif
