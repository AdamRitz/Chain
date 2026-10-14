#ifndef CHAIN_BLOCK_H
#define CHAIN_BLOCK_H
#include "Transaction.h"
#include "../Crypto/MerkleTree.h"
#include <boost/asio/thread_pool.hpp>
#include <boost/asio/post.hpp>
#include <latch>
using namespace std;

struct Block {
    array<uint8_t,32> previousHash{};
    array<uint8_t,32> merkleRoot{};
    uint64_t height=0;
    uint64_t txNum=0;
    array<uint8_t,32> hash{};
    vector<array<uint8_t,176>> txs;
    EvmPayloads evm;
};
uint64_t epoch=2;
pair<Block,vector<uint8_t>> BlockBuffer[3];
mutex blockBufferLock;
atomic<uint64_t> blockBuildNs{0},blockVerifyNs{0},blockCount{0};
atomic<uint64_t> blockCommitNs{0};
atomic<uint64_t> duplicateBlocks{0};
atomic<bool> blockReady{false};
unique_ptr<boost::asio::thread_pool> blockVerifyThreads;
size_t blockVerifyNum=1;
atomic<size_t> blockVerifyJobs{0},peakBlockVerifyJobs{0};
atomic<uint64_t> coldSignatureNs{0},parallelSignatureBlocks{0};

bool VerifyBlockTransactions(const Block& block,const vector<bool>& verified) {
    vector<size_t> cold;
    for (size_t i=0;i<block.txs.size();i++) if (!verified[i]) cold.push_back(i);
    if (cold.empty()) return true;
    auto start=GetSteadyTime();
    atomic<bool> valid{true};
    exception_ptr failure; mutex failureMutex;
    auto verify=[&](size_t begin,size_t end) {
        try {
            for (size_t j=begin;j<end&&valid.load();j++) {
                auto i=cold[j]; auto input=block.evm.find(i);
                if (!VerifyTransaction(block.txs[i],input==block.evm.end()?span<const uint8_t>{}:span<const uint8_t>{input->second})) valid=false;
            }
        } catch (...) { lock_guard lock(failureMutex); failure=current_exception(); valid=false; }
    };
    size_t parts=blockVerifyThreads&&cold.size()>=128?min(blockVerifyNum,cold.size()/64):1;
    latch done(ptrdiff_t(parts-1));
    auto chunk=(cold.size()+parts-1)/parts;
    for (size_t part=1;part<parts;part++) {
        auto begin=min(part*chunk,cold.size()),end=min(begin+chunk,cold.size());
        auto jobs=blockVerifyJobs.load(); bool queued=false;
        while (jobs<blockVerifyNum*2) if (blockVerifyJobs.compare_exchange_weak(jobs,jobs+1)) { queued=true; break; }
        if (queued) {
            auto peak=peakBlockVerifyJobs.load(); while (peak<jobs+1&&!peakBlockVerifyJobs.compare_exchange_weak(peak,jobs+1)) {}
            try { boost::asio::post(*blockVerifyThreads,[&,begin,end] { verify(begin,end); blockVerifyJobs--; done.count_down(); }); }
            catch (...) { blockVerifyJobs--; verify(begin,end); done.count_down(); }
        } else { verify(begin,end); done.count_down(); }
    }
    verify(0,min(chunk,cold.size())); done.wait();
    coldSignatureNs+=GetSteadyTime()-start; if (parts>1) parallelSignatureBlocks++;
    if (failure) rethrow_exception(failure);
    return valid.load();
}

// ------------------------------------------------序列化和反序列化------------------------------------------------
array<uint8_t,80> SerializeBlockHeader(const Block& block) {
    array<uint8_t,80> header{};
    memcpy(header.data(),block.previousHash.data(),32);
    memcpy(header.data()+32,block.merkleRoot.data(),32);
    WriteU64(header.data()+64,block.height);
    WriteU64(header.data()+72,block.txNum);
    return header;
}
bool UnSerializeBlock(const vector<uint8_t>& data,Block& block) {
    if (data.size()<112) return false;
    auto num=ReadU64(data.data()+72);
    if (!ReadEvmPayloads(data,block.evm)) return false;
    memcpy(block.previousHash.data(),data.data(),32);
    memcpy(block.merkleRoot.data(),data.data()+32,32);
    block.height=ReadU64(data.data()+64);
    block.txNum=num;
    memcpy(block.hash.data(),data.data()+80,32);
    block.txs.resize(num);
    if (num) memcpy(block.txs.data(),data.data()+112,num*176);
    return true;
}
Block UnSerializeBlock(const vector<uint8_t>& data) {
    Block block;
    if (!UnSerializeBlock(data,block)) throw invalid_argument("Invalid block length");
    return block;
}
vector<uint8_t> SerializeBlockALL(Block& block) {
    block.txNum=block.txs.size();
    auto header=SerializeBlockHeader(block);
    crypto_generichash(block.hash.data(),32,header.data(),header.size(),nullptr,0);
    vector<uint8_t> data(112+block.txNum*176);
    memcpy(data.data(),header.data(),80);
    memcpy(data.data()+80,block.hash.data(),32);
    if (block.txNum) memcpy(data.data()+112,block.txs.data(),block.txNum*176);
    AppendEvmPayloads(data,block.evm);
    return data;
}

// ------------------------------------------------区块生成和验证------------------------------------------------
vector<uint8_t> GenerateBlock() {
    auto start=GetSteadyTime();
    Block block;
    vector<pair<array<uint8_t,32>,array<uint8_t,176>>> selected;
    unordered_map<array<uint8_t,32>,vector<uint8_t>,GetMapHash> payloads;
    size_t blockBytes=120;
    uint64_t gasBudget=0;
    {
        shared_lock commitLock(dbCommitMutex);
        auto height=DBReadBlockHeight();
        if (height==UINT64_MAX) throw runtime_error("Block height overflow");
        block.height=height+1;
        block.previousHash=DBReadCurrentBlock();
        lock_guard lock(txpoolMutex);
        selected.reserve(min(maxBlockTx,txpool.size()));
        vector<pair<array<uint8_t,32>,uint64_t>> dropped;
        struct ReadyUser {
            array<uint8_t,32> key; User available;
            map<uint64_t,array<uint8_t,32>>::const_iterator next,end;
        };
        vector<ReadyUser> ready;
        ready.reserve(txUserPool.size());
        for (const auto& [key,entry]:txUserPool) {
            auto user=GetUser(users,key); auto next=entry.txs.upper_bound(user.nonce);
            if (next!=entry.txs.end()) ready.push_back({key,user,next,entry.txs.end()});
        }
        sort(ready.begin(),ready.end(),[](const auto& a,const auto& b) { return a.key<b.key; });
        // 公钥顺序固定，每轮每账户取一笔，连续序号在块满之前公平推进。
        while (!ready.empty()&&selected.size()<maxBlockTx) {
            for (auto& item:ready) {
                if (selected.size()==maxBlockTx) break;
                auto& available=item.available; const auto& key=item.key;
                const auto& tx=txpool.at(item.next->second);
                auto amount=ReadU64(tx.data()+64),nonce=ReadU64(tx.data()+72);
                auto evm=evmPool.find(item.next->second);
                bool contract=evm!=evmPool.end();
                auto extra=contract?evm->second.size()+8:0;
                auto gas=contract?ReadU64(evm->second.data()+5):0;
                if (blockBytes+176+extra>maxBlockBytes||gas>maxBlockGas-gasBudget) { item.next=item.end; continue; }
                if (!(contract?CheckEvmUserTx(available,tx,evm->second):CheckUserTx(available,amount,nonce))) {
                    if (available.nonce<UINT64_MAX&&nonce==available.nonce+1) dropped.emplace_back(key,nonce);
                    item.next=item.end; continue;
                }
                available.nonce=nonce;
                available.balance-=baseFee;
                if (contract||memcmp(tx.data(),tx.data()+32,32)!=0) available.balance-=amount;
                if (contract) { available.balance-=gas; payloads.emplace(item.next->second,evm->second); }
                gasBudget+=gas; blockBytes+=176+extra;
                selected.emplace_back(item.next->second,tx); ++item.next;
            }
            erase_if(ready,[](const auto& item) { return item.next==item.end; });
        }
        for (const auto& [key,nonce]:dropped) DropUserPoolFrom(key,nonce);
    }
    if (selected.empty()) return {};
    // 先按序号、再按发送者排序，保证每个账户的交易严格依次执行。
    sort(selected.begin(),selected.end(),[](const auto& a,const auto& b){
        auto left=ReadU64(a.second.data()+72),right=ReadU64(b.second.data()+72);
        if (left!=right) return left<right;
        return lexicographical_compare(a.second.begin(),a.second.begin()+32,b.second.begin(),b.second.begin()+32);
    });
    vector<array<uint8_t,32>> hashes;
    hashes.reserve(selected.size());
    block.txs.reserve(selected.size());
    for (const auto& [hash,tx]:selected) {
        if (auto found=payloads.find(hash);found!=payloads.end()) block.evm.emplace(block.txs.size(),std::move(found->second));
        hashes.push_back(hash); block.txs.push_back(tx);
    }
    // 提议时不删除交易；写库成功后再移除。
    block.merkleRoot=MerkleCompute(hashes);
    auto data=SerializeBlockALL(block);
    blockBuildNs+=GetSteadyTime()-start;
    return data;
}
bool VerifyBlock(const vector<uint8_t>& data,Block* result=nullptr) {
    auto start=GetSteadyTime();
    Block block;
    if (!UnSerializeBlock(data,block)) return false;
    auto header=SerializeBlockHeader(block);
    array<uint8_t,32> hash;
    crypto_generichash(hash.data(),32,header.data(),header.size(),nullptr,0);
    if (hash!=block.hash) return false;
    vector<array<uint8_t,32>> hashes;
    hashes.reserve(block.txNum);
    unordered_set<array<uint8_t,32>,GetMapHash> unique;
    unique.reserve(block.txNum);
    vector<bool> verified(block.txNum,false);
    {
        lock_guard lock(txpoolMutex);
        for (size_t i=0;i<block.txNum;i++) {
            const auto& tx=block.txs[i];
            auto txHash=GetTransactionHash(tx);
            auto found=txpool.find(txHash);
            // 只复用字节完全一致的已验证交易。
            verified[i]=found!=txpool.end()&&found->second==tx;
            if (block.evm.count(i)) verified[i]=verified[i]&&evmPool.count(txHash)&&evmPool.at(txHash)==block.evm.at(i);
            else verified[i]=verified[i]&&!evmPool.count(txHash);
        }
    }
    if (!VerifyBlockTransactions(block,verified)) return false;
    for (size_t i=0;i<block.txNum;i++) {
        const auto& tx=block.txs[i];
        auto txHash=GetTransactionHash(tx);
        if (!unique.insert(txHash).second) return false;
        hashes.push_back(txHash);
    }
    if (block.height==0||(block.txNum==0&&block.height!=1)) return false;
    if (block.height==1) {
        if (block.txNum||block.previousHash!=array<uint8_t,32>{}||block.merkleRoot!=genesisRoot) return false;
    } else if (MerkleCompute(hashes)!=block.merkleRoot) return false;
    if (result) *result=std::move(block);
    blockVerifyNs+=GetSteadyTime()-start;
    return true;
}
void GenerateGenesisBlock() {
    lock_guard commitLock(dbCommitMutex);
    auto height=DBReadBlockHeight();
    if (height!=0) {
        Block genesis;
        if (!UnSerializeBlock(DBReadBlockByHeight("1"),genesis)||genesis.height!=1||genesis.merkleRoot!=genesisRoot||!VerifyBlock(DBReadBlockByHeight("1"))) throw runtime_error("Genesis state does not match database");
        auto hash=DBReadCurrentBlock();
        auto data=DBReadBlockByHash(hash);
        Block current;
        if (!UnSerializeBlock(data,current)||!VerifyBlock(data)||current.hash!=hash||current.height!=height||DBReadBlockByHeight(to_string(height))!=data) throw runtime_error("Invalid existing chain head");
        lock_guard lock(blockBufferLock);
        if (height==UINT64_MAX) throw runtime_error("Block height overflow");
        epoch=height+1;
        spdlog::info("Existing chain restored. Height:{}",height);
        return;
    }
    if (DBReadCurrentBlock()!=array<uint8_t,32>{}) throw runtime_error("Incomplete chain metadata");
    Block block;
    block.height=1;
    block.merkleRoot=genesisRoot;
    auto data=SerializeBlockALL(block);
    rocksdb::WriteBatch batch;
    DBAddBlock(batch,block.hash,data);
    batch.Put("CurrentBlock",rocksdb::Slice(reinterpret_cast<const char*>(block.hash.data()),32));
    batch.Put("BlockHeight","1");
    batch.Put("SchemaVersion","7");
    batch.Put("ChainTx","0");
    batch.Put(DBHashKey("score/",block.hash),"0");
    batch.Put("GenesisConfig",GetGenesisConfig().dump());
    batch.Put("UserBurned","0");
    for (const auto& [key,user]:genesisUsers) DBAddUser(batch,key,user);
    DBWriteBatch(batch);
    InvalidateExecutionCache();
    users.clear();
    users.reserve(genesisUsers.size());
    for (const auto& [key,user]:genesisUsers) users.emplace(key,user);
    PublishPoolUsers();
    PublishChainHead();
    dbLegacyKeys=false;
    {
        lock_guard lock(blockBufferLock);
        epoch=2;
    }
    spdlog::info("Genesis Block Generated. Height:1");
}
// 维护三个高度的候选窗口，按交易数量和哈希比较。
bool QueueForkBlock(Block block,vector<uint8_t> data,bool notify);
bool HasForkBlock(const array<uint8_t,32>& hash,const vector<uint8_t>& data);
bool ProcessBlock(vector<uint8_t> data) {
    if (data.size()<112) return false;
    auto height=ReadU64(data.data()+64);
    uint64_t current;
    array<uint8_t,32> head;
    {
        shared_lock lock(dbCommitMutex);
        current=DBReadBlockHeight(); head=DBReadCurrentBlock();
        if (height>=2&&height<=current&&current-height<=256&&DBReadBlockByHeight(to_string(height))==data) {
            duplicateBlocks++; return false;
        }
    }
    if (height<2||height>current+256||current>height+256) return false;
    {
        lock_guard lock(blockBufferLock);
        if (height>=epoch&&height-epoch<3&&BlockBuffer[height-epoch].second==data) { duplicateBlocks++; return false; }
    }
    array<uint8_t,32> hash;memcpy(hash.data(),data.data()+80,32);
    if (HasForkBlock(hash,data)) { duplicateBlocks++; return false; }
    Block block;
    if (!VerifyBlock(data,&block)) return false;
    if (height!=current+1||block.previousHash!=head) {
        auto canonical=DBReadBlockByHeight(to_string(height));
        if (canonical==data) return false;
        return QueueForkBlock(std::move(block),std::move(data),true);
    }
    if (!DBCheckBlockUsers(data)) { invalidUserBlocks++; return false; }
    {
        lock_guard lock(blockBufferLock);
        if (height<epoch||height-epoch>=3) return false;
        auto& slot=BlockBuffer[height-epoch];
        if (!slot.second.empty()&&(block.txNum<slot.first.txNum||(block.txNum==slot.first.txNum&&block.hash>=slot.first.hash))) return false;
        slot={std::move(block),std::move(data)};
        blockReady=!BlockBuffer[0].second.empty();
    }
    WakeMainLoop();
    return true;
}
bool CommitBlock(const Block& block,const vector<uint8_t>& data) {
    auto start=GetSteadyTime();
    if (!DBCommitBlock(block.hash,data)) return false;
    blockCommitNs+=GetSteadyTime()-start;
    {
        shared_lock commitLock(dbCommitMutex);
        lock_guard lock(txpoolMutex);
        unordered_set<array<uint8_t,32>,GetMapHash> senders;
        for (const auto& tx:block.txs) { array<uint8_t,32> key; memcpy(key.data(),tx.data(),32); senders.insert(key); }
        for (const auto& key:senders) PruneUserPool(key,GetUser(users,key).nonce);
    }
    {
        lock_guard lock(blockBufferLock);
        if (epoch==block.height) {
            BlockBuffer[0]=std::move(BlockBuffer[1]);
            BlockBuffer[1]=std::move(BlockBuffer[2]);
            BlockBuffer[2]={};
            epoch=block.height+1;
        }
        blockReady=!BlockBuffer[0].second.empty();
    }
    committedTx+=block.txNum;
    blockCount++;
    lastCommitTime=GetSteadyTime();
    return true;
}
#include "Fork.h"
#endif
