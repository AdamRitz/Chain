#ifndef CHAIN_BLOCK_H
#define CHAIN_BLOCK_H
#include "Transaction.h"
#include "../Crypto/MerkleTree.h"
using namespace std;

struct Block {
    array<uint8_t,32> previousHash{};
    array<uint8_t,32> merkleRoot{};
    uint64_t height=0;
    uint64_t txNum=0;
    array<uint8_t,32> hash{};
    vector<array<uint8_t,176>> txs;
};
uint64_t epoch=2;
pair<Block,vector<uint8_t>> BlockBuffer[3];
mutex blockBufferLock;
atomic<uint64_t> blockBuildNs{0},blockVerifyNs{0},blockCount{0};
atomic<uint64_t> blockCommitNs{0};
atomic<bool> blockReady{false};

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
    if (num>6000||num>(data.size()-112)/176||data.size()!=112+num*176) return false;
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
    return data;
}

// ------------------------------------------------区块生成和验证------------------------------------------------
vector<uint8_t> GenerateBlock() {
    auto start=GetSteadyTime();
    Block block;
    vector<pair<array<uint8_t,32>,array<uint8_t,176>>> selected;
    {
        shared_lock commitLock(dbCommitMutex);
        auto height=DBReadBlockHeight();
        if (height==UINT64_MAX) throw runtime_error("Block height overflow");
        block.height=height+1;
        block.previousHash=DBReadCurrentBlock();
        lock_guard lock(txpoolMutex);
        selected.reserve(min(maxBlockTx,txpool.size()));
        vector<pair<array<uint8_t,32>,uint64_t>> dropped;
        for (const auto& [key,entry]:txUserPool) {
            if (selected.size()==maxBlockTx) break;
            const auto& pending=entry.txs;
            auto available=GetUser(users,key);
            for (auto it=pending.upper_bound(available.nonce);it!=pending.end()&&selected.size()<maxBlockTx;++it) {
                const auto& tx=txpool.at(it->second);
                auto amount=ReadU64(tx.data()+64),nonce=ReadU64(tx.data()+72);
                if (!CheckUserTx(available,amount,nonce)) {
                    if (available.nonce<UINT64_MAX&&nonce==available.nonce+1) dropped.emplace_back(key,nonce);
                    break;
                }
                available.nonce=nonce;
                available.balance-=baseFee;
                if (memcmp(tx.data(),tx.data()+32,32)!=0) available.balance-=amount;
                selected.emplace_back(it->second,tx);
            }
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
    for (const auto& [hash,tx]:selected) { hashes.push_back(hash); block.txs.push_back(tx); }
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
        }
    }
    for (size_t i=0;i<block.txNum;i++) {
        const auto& tx=block.txs[i];
        if (!verified[i]&&!VerifyTransaction(tx)) return false;
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
    batch.Put("SchemaVersion","3");
    batch.Put("GenesisConfig",GetGenesisConfig().dump());
    batch.Put("UserBurned","0");
    for (const auto& [key,user]:genesisUsers) DBAddUser(batch,key,user);
    DBWriteBatch(batch);
    users.clear();
    users.reserve(genesisUsers.size());
    for (const auto& [key,user]:genesisUsers) users.emplace(key,user);
    dbLegacyKeys=false;
    {
        lock_guard lock(blockBufferLock);
        epoch=2;
    }
    spdlog::info("Genesis Block Generated. Height:1");
}
// 维护三个高度的候选窗口，按交易数量和哈希比较。
bool ProcessBlock(vector<uint8_t> data) {
    if (data.size()<112) return false;
    auto height=ReadU64(data.data()+64);
    {
        lock_guard lock(blockBufferLock);
        if (height<epoch||height-epoch>=3) return false;
    }
    Block block;
    if (!VerifyBlock(data,&block)) return false;
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
        for (const auto& tx:block.txs) {
            array<uint8_t,32> key;
            memcpy(key.data(),tx.data(),32);
            PruneUserPool(key,GetUser(users,key).nonce);
        }
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
#endif
