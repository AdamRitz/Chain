#ifndef CHAIN_DB_H
#define CHAIN_DB_H
#include <atomic>
#include <charconv>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <vector>
#include <deque>
#include <unordered_set>
#include <rocksdb/db.h>
#include <rocksdb/options.h>
#include <rocksdb/write_batch.h>
#include <rocksdb/statistics.h>
#include <rocksdb/filter_policy.h>
#include <rocksdb/table.h>
#include <rocksdb/perf_context.h>
#include <rocksdb/perf_level.h>
#include <rocksdb/utilities/write_batch_with_index.h>
#include <spdlog/spdlog.h>
#include "../Tool/Tool.h"
#include "../Time/Time.h"
#include "../User/User.h"
#include "../EVM/Execute.h"
using namespace std;

unique_ptr<rocksdb::DB> db;
rocksdb::Options options;
shared_mutex dbCommitMutex;
bool dbSync=true;
bool dbLegacyKeys=false;
bool experimentIndex=false;
atomic<uint64_t> dbWriteNs{0},dbWriteCount{0};
atomic<uint64_t> dbPrepareNs{0},dbBatchBytes{0};
bool dbProfile=false;
atomic<uint64_t> dbProfileWrites{0},dbWalNs{0},dbMemtableNs{0},dbDelayNs{0},dbOtherNs{0};
struct ChainHead {
    uint64_t height=0,transactions=0,version=0,burned=0;
    size_t users=0,contracts=0;
    array<uint8_t,32> hash{};
};
ChainHead publishedHead;
struct ChainAnchor { uint64_t height=0,transactions=0; array<uint8_t,32> hash{}; };
struct PublishedChain { ChainHead head; vector<ChainAnchor> tail; int64_t atUs=0; };
PublishedChain publishedChain;
mutex chainHeadMutex;
ChainHead ReadChainHead() { lock_guard lock(chainHeadMutex); return publishedHead; }
PublishedChain ReadPublishedChain() { lock_guard lock(chainHeadMutex); return publishedChain; }
pair<ChainHead,int64_t> ReadPublishedHead() { lock_guard lock(chainHeadMutex); return {publishedHead,publishedChain.atUs}; }
void PublishChainHead();
UserMap users;
uint64_t userBurned=0;
UserMap poolUsers;
shared_mutex poolUsersMutex;
void PublishPoolUsers(const UserMap* changed=nullptr,const unordered_set<string>* keys=nullptr) {
    unique_lock lock(poolUsersMutex);
    if (changed) {
        for (const auto& [key,user]:*changed) poolUsers[key]=user;
    } else if (keys) {
        for (const auto& encoded:*keys) if (encoded.starts_with("user/")&&encoded.size()==37) {
            array<uint8_t,32> key; memcpy(key.data(),encoded.data()+5,32);
            if (auto found=users.find(key);found!=users.end()) poolUsers[key]=found->second;
            else poolUsers.erase(key);
        }
    } else poolUsers=users;
}
atomic<uint64_t> userCheckNs{0},userWriteCount{0},invalidUserBlocks{0};
// 分支切换使用临时批次；所有读取看到同一批次中的回滚和重放结果。
thread_local rocksdb::WriteBatchWithIndex* branchBatch=nullptr;
atomic<uint64_t> reorgCount{0};
atomic<uint64_t> maxReorgDepth{0};
uint64_t dbStateVersion=0;
struct CachedExecution {
    vector<uint8_t> data;
    array<uint8_t,32> parent{};
    uint64_t version=0,height=0;
    UserMap users;
    EvmExecution evm;
    size_t bytes=0;
};
mutex executionMutex;
deque<unique_ptr<CachedExecution>> executionCache;
size_t executionBytes=0;
constexpr size_t maxExecutionBytes=32*1024*1024;
atomic<uint64_t> executionCacheHits{0},executionCacheMisses{0},candidateExecuteNs{0},commitExecuteNs{0};
atomic<uint64_t> evmAccountWrites{0},evmSlotWrites{0},evmPersistNs{0};
atomic<uint64_t> commitLockWaitNs{0},candidateLockWaitNs{0};
void InvalidateExecutionCache() {
    dbStateVersion++;
    lock_guard lock(executionMutex); executionCache.clear(); executionBytes=0;
}
string DBEvmKey(const char* prefix,const evmc::address& address) {
    string key(prefix); key.append(reinterpret_cast<const char*>(address.bytes),20); return key;
}
string DBEvmSlotKey(const evmc::address& address,const evmc::bytes32& slot) {
    auto key=DBEvmKey("evm-slot/",address); key.append(reinterpret_cast<const char*>(slot.bytes),32); return key;
}

void CheckDBStatus(const rocksdb::Status& status) {
    if (!status.ok()) throw runtime_error("RocksDB: "+status.ToString());
}
string DBHashKey(const char* prefix,const array<uint8_t,32>& hash) {
    string key(prefix);
    key.append(reinterpret_cast<const char*>(hash.data()),hash.size());
    return key;
}
bool DBGet(const string& key,string& value) {
    auto status=branchBatch?branchBatch->GetFromBatchAndDB(db.get(),rocksdb::ReadOptions(),key,&value):db->Get(rocksdb::ReadOptions(),key,&value);
    if (status.IsNotFound()) return false;
    CheckDBStatus(status);
    return true;
}
void InitDB(const string& path="test",bool sync=true) {
    dbSync=sync;
    options=rocksdb::Options();
    options.create_if_missing=true;
    options.statistics=rocksdb::CreateDBStatistics();
    // 布隆过滤器加速历史查询中不存在的键，减少读取磁盘数据块。
    rocksdb::BlockBasedTableOptions tableOptions;
    tableOptions.filter_policy.reset(rocksdb::NewBloomFilterPolicy(10,false));
    options.table_factory.reset(rocksdb::NewBlockBasedTableFactory(tableOptions));
    rocksdb::DB* openedDB=nullptr;
    CheckDBStatus(rocksdb::DB::Open(options,path,&openedDB));
    db.reset(openedDB);
    string version;
    if (DBGet("SchemaVersion",version)) {
        if (version!="7") throw runtime_error("Height transaction indexes require schema 7. Use a NEW --data directory with --genesis.");
        string saved;
        if (!DBGet("GenesisConfig",saved)) throw runtime_error("Missing genesis account configuration");
        auto config=nlohmann::json::parse(saved);
        if (genesisConfigured&&GetGenesisConfig()!=config) throw runtime_error("Genesis configuration does not match this database");
        if (!genesisConfigured) SetGenesisUsers(config);
    } else {
        unique_ptr<rocksdb::Iterator> it(db->NewIterator(rocksdb::ReadOptions()));
        it->SeekToFirst();
        if (it->Valid()) throw runtime_error("Unversioned database: use a NEW account database directory");
        CheckDBStatus(it->status());
        if (!genesisConfigured) SetGenesisUsers({{"base_fee",0},{"accounts",nlohmann::json::array()}});
    }
    users.clear();
    userBurned=0;
    uint64_t balanceTotal=0;
    unique_ptr<rocksdb::Iterator> it(db->NewIterator(rocksdb::ReadOptions()));
    for (it->Seek("user/");it->Valid()&&it->key().starts_with("user/");it->Next()) {
        if (it->key().size()!=37||it->value().size()!=16) throw runtime_error("Invalid persisted account");
        array<uint8_t,32> key;
        memcpy(key.data(),it->key().data()+5,32);
        auto value=reinterpret_cast<const uint8_t*>(it->value().data());
        User user{ReadU64(value),ReadU64(value+8)};
        if (UINT64_MAX-balanceTotal<user.balance) throw runtime_error("Account supply overflow");
        balanceTotal+=user.balance;
        users.emplace(key,user);
    }
    CheckDBStatus(it->status());
    evmState=EvmState{};
    for (it->Seek("evm/");it->Valid()&&it->key().starts_with("evm/");it->Next()) {
        if (it->key().size()!=24) throw runtime_error("Invalid EVM account key");
        evmc::address address; memcpy(address.bytes,it->key().data()+4,20);
        auto account=UnserializeEvmAccount(it->value().ToString());
        if (account.balance>UINT64_MAX-balanceTotal) throw runtime_error("EVM supply overflow");
        balanceTotal+=static_cast<uint64_t>(account.balance);
        evmState.insert(address,std::move(account));
    }
    CheckDBStatus(it->status());
    for (it->Seek("evm-code/");it->Valid()&&it->key().starts_with("evm-code/");it->Next()) {
        if (it->key().size()!=29) throw runtime_error("Invalid EVM code key");
        evmc::address address; memcpy(address.bytes,it->key().data()+9,20);
        auto found=evmState.get_accounts().find(address);
        if (found==evmState.get_accounts().end()) throw runtime_error("Orphan EVM code");
        auto code=it->value(); found->second.code.assign(reinterpret_cast<const uint8_t*>(code.data()),code.size());
    }
    CheckDBStatus(it->status());
    for (it->Seek("evm-slot/");it->Valid()&&it->key().starts_with("evm-slot/");it->Next()) {
        if (it->key().size()!=61||it->value().size()!=32) throw runtime_error("Invalid EVM slot");
        evmc::address address; evmc::bytes32 key,value;
        memcpy(address.bytes,it->key().data()+9,20); memcpy(key.bytes,it->key().data()+29,32); memcpy(value.bytes,it->value().data(),32);
        auto found=evmState.get_accounts().find(address);
        if (found==evmState.get_accounts().end()||value==evmc::bytes32{}) throw runtime_error("Orphan or zero EVM slot");
        found->second.storage[key]={value,value,EVMC_ACCESS_COLD};
    }
    CheckDBStatus(it->status());
    if (!version.empty()) {
        string burned;
        if (!DBGet("UserBurned",burned)) throw runtime_error("Missing fee total");
        auto result=from_chars(burned.data(),burned.data()+burned.size(),userBurned);
        if (result.ec!=errc()||result.ptr!=burned.data()+burned.size()||userBurned>genesisSupply||balanceTotal!=genesisSupply-userBurned) throw runtime_error("Account supply mismatch");
    }
    spdlog::info("RocksDB Started. Path:{}, Sync:{}",path,sync);
    InvalidateExecutionCache();
    PublishPoolUsers();
    PublishChainHead();
}
void DBWriteBatch(rocksdb::WriteBatch& batch) {
    if (branchBatch) {
        struct Copy:rocksdb::WriteBatch::Handler {
            void Put(const rocksdb::Slice& key,const rocksdb::Slice& value) override { branchBatch->Put(key,value); }
            void Delete(const rocksdb::Slice& key) override { branchBatch->Delete(key); }
        } copy;
        CheckDBStatus(batch.Iterate(&copy));
        return;
    }
    auto start=GetSteadyTime();
    bool sampled=dbProfile&&dbWriteCount.load()%64==0;
    auto context=sampled?rocksdb::get_perf_context():nullptr;
    uint64_t wal=0,memtable=0,delay=0,other=0;
    struct PerfLevel {
        bool active; rocksdb::PerfLevel previous;
        ~PerfLevel() { if (active) rocksdb::SetPerfLevel(previous); }
    } level{sampled,sampled?rocksdb::GetPerfLevel():rocksdb::kDisable};
    if (sampled) {
        wal=context->write_wal_time; memtable=context->write_memtable_time;
        delay=context->write_delay_time; other=context->write_pre_and_post_process_time;
        rocksdb::SetPerfLevel(rocksdb::kEnableTimeExceptForMutex);
    }
    rocksdb::WriteOptions writeOptions;
    writeOptions.sync=dbSync;
    CheckDBStatus(db->Write(writeOptions,&batch));
    if (sampled) {
        dbWalNs+=context->write_wal_time-wal; dbMemtableNs+=context->write_memtable_time-memtable;
        dbDelayNs+=context->write_delay_time-delay; dbOtherNs+=context->write_pre_and_post_process_time-other; dbProfileWrites++;
    }
    dbWriteNs+=GetSteadyTime()-start;
    dbBatchBytes+=batch.Data().size();
    dbWriteCount++;
}
void DBWriteTx(const array<uint8_t,32>& key,const array<uint8_t,176>& value) {
    rocksdb::WriteBatch batch;
    batch.Put(DBHashKey("tx/",key),rocksdb::Slice(reinterpret_cast<const char*>(value.data()),value.size()));
    DBWriteBatch(batch);
}
bool DBHasTx(const array<uint8_t,32>& key) {
    string value;
    return DBGet(DBHashKey("tx/",key),value);
}
void DBAddUser(rocksdb::WriteBatch& batch,const array<uint8_t,32>& key,const User& user) {
    array<uint8_t,16> value;
    WriteU64(value.data(),user.balance);
    WriteU64(value.data()+8,user.nonce);
    batch.Put(DBHashKey("user/",key),rocksdb::Slice(reinterpret_cast<const char*>(value.data()),value.size()));
}
void ApplyMemoryKey(const rocksdb::Slice& key,const rocksdb::Slice* value) {
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
        else {
            auto header=UnserializeEvmAccount(value->ToString()); auto& account=evmState.get_accounts()[address];
            account.nonce=header.nonce; account.balance=header.balance;
        }
    } else if (key.starts_with("evm-code/")&&key.size()==29) {
        evmc::address address; memcpy(address.bytes,key.data()+9,20);
        if (value) evmState.get_accounts()[address].code.assign(reinterpret_cast<const uint8_t*>(value->data()),value->size());
        else if (auto it=evmState.get_accounts().find(address);it!=evmState.get_accounts().end()) it->second.code.clear();
    } else if (key.starts_with("evm-slot/")&&key.size()==61) {
        evmc::address address; evmc::bytes32 slot,word;
        memcpy(address.bytes,key.data()+9,20); memcpy(slot.bytes,key.data()+29,32);
        if (value) {
            if (value->size()!=32) throw runtime_error("Invalid EVM slot undo");
            memcpy(word.bytes,value->data(),32); evmState.get_accounts()[address].storage[slot]={word,word,EVMC_ACCESS_COLD};
        } else if (auto it=evmState.get_accounts().find(address);it!=evmState.get_accounts().end()) it->second.storage.erase(slot);
    }
}

struct BranchMemory {
    rocksdb::WriteBatch saved;
    unordered_set<string> keys;
    void Save(const rocksdb::Slice& key) {
        if (!(key.starts_with("user/")||key.starts_with("evm/")||key.starts_with("evm-code/")||key.starts_with("evm-slot/"))) return;
        if (!keys.insert(key.ToString()).second) return;
        if (key.starts_with("user/")&&key.size()==37) {
            array<uint8_t,32> address; memcpy(address.data(),key.data()+5,32);
            if (auto found=users.find(address);found!=users.end()) DBAddUser(saved,address,found->second);
            else saved.Delete(key);
            return;
        }
        auto prefix=key.starts_with("evm/")?size_t(4):size_t(9);
        if (key.size()!=(key.starts_with("evm-slot/")?size_t(61):prefix+20)) throw runtime_error("Invalid branch state key");
        evmc::address address; memcpy(address.bytes,key.data()+prefix,20);
        auto found=evmState.get_accounts().find(address);
        if (found==evmState.get_accounts().end()) { saved.Delete(key); return; }
        if (key.starts_with("evm/")) saved.Put(key,SerializeEvmHeader(found->second));
        else if (key.starts_with("evm-code/")) {
            const auto& code=found->second.code;
            if (code.empty()) saved.Delete(key);
            else saved.Put(key,rocksdb::Slice(reinterpret_cast<const char*>(code.data()),code.size()));
        } else {
            evmc::bytes32 slot; memcpy(slot.bytes,key.data()+29,32); auto word=evmState.ReadStorage(address,slot);
            if (word==evmc::bytes32{}) saved.Delete(key);
            else saved.Put(key,rocksdb::Slice(reinterpret_cast<const char*>(word.bytes),32));
        }
    }
    void Restore() {
        struct Apply:rocksdb::WriteBatch::Handler {
            void Put(const rocksdb::Slice& key,const rocksdb::Slice& value) override { ApplyMemoryKey(key,&value); }
            void Delete(const rocksdb::Slice& key) override { ApplyMemoryKey(key,nullptr); }
        } apply;
        CheckDBStatus(saved.Iterate(&apply));
    }
};
thread_local BranchMemory* branchMemory=nullptr;
void SaveBranchMemory(rocksdb::WriteBatch& batch) {
    if (!branchMemory) return;
    struct Save:rocksdb::WriteBatch::Handler {
        void Put(const rocksdb::Slice& key,const rocksdb::Slice&) override { branchMemory->Save(key); }
        void Delete(const rocksdb::Slice& key) override { branchMemory->Save(key); }
    } save;
    CheckDBStatus(batch.Iterate(&save));
}

bool DBApplyBlockUsers(const vector<uint8_t>& data,UserMap& changed,EvmExecution* output=nullptr) {
    if (data.size()<112) return false;
    auto num=ReadU64(data.data()+72);
    EvmPayloads payloads;
    if (!ReadEvmPayloads(data,payloads)) return false;
    EvmExecution local;
    thread_local uint64_t knownVersion=UINT64_MAX;
    thread_local decltype(evmone::state::BlockInfo{}.known_block_hashes) knownHashes;
    auto& execution=output?*output:local;
    evmone::state::BlockInfo context{};
    context.number=ReadU64(data.data()+64); context.timestamp=context.number;
    context.gas_limit=maxBlockGas; context.base_fee=1;
    if (!payloads.empty()) {
        // 区块时间采用确定的逻辑轮次，所有节点取得相同环境。
        if (branchBatch||knownVersion!=dbStateVersion) {
            knownHashes.clear();
            for (int64_t height=max(int64_t(1),context.number-256);height<context.number;height++) {
                string hash;
                if (DBGet("height/"+to_string(height),hash)&&hash.size()==32) {
                    evmc::bytes32 value; memcpy(value.bytes,hash.data(),32); knownHashes.emplace(height,value);
                }
            }
            knownVersion=branchBatch?UINT64_MAX:dbStateVersion;
        }
        context.known_block_hashes=knownHashes;
    }
    changed.reserve(min(size_t(num*2),users.size()+size_t(num)));
    auto start=GetSteadyTime();
    for (size_t i=0;i<num;i++) {
        auto tx=span<const uint8_t,176>(data.data()+112+i*176,176);
        auto payload=payloads.find(i);
        bool accepted=payload==payloads.end()?ApplyUserTx(users,changed,tx):ExecuteEvmTx(changed,tx,payload->second,users,execution,context);
        if (!accepted) {
            userCheckNs+=GetSteadyTime()-start;
            return false;
        }
    }
    userCheckNs+=GetSteadyTime()-start;
    return true;
}

bool DBReadTxPosition(const array<uint8_t,32>& key,uint64_t& height,array<uint8_t,32>& block,uint32_t& index) {
    string value;
    if (!DBGet(DBHashKey("tx/",key),value)) return false;
    if (value.size()!=12) throw runtime_error("Invalid transaction position");
    auto encoded=reinterpret_cast<const uint8_t*>(value.data());
    height=ReadU64(encoded); index=ReadU32(encoded+8);
    string hash;
    if (!height||!DBGet("height/"+to_string(height),hash)||hash.size()!=32) throw runtime_error("Missing transaction height");
    memcpy(block.data(),hash.data(),32); return true;
}
bool DBReadTx(const array<uint8_t,32>& key,array<uint8_t,176>& tx) {
    string value;
    if (!DBGet(DBHashKey("tx/",key),value)) return false;
    // 原始写入微基准仍可读取完整交易；业务格式 7 保存高度和位置。
    if (value.size()==176) { memcpy(tx.data(),value.data(),176); return true; }
    if (value.size()!=12) throw runtime_error("Invalid transaction position");
    auto encoded=reinterpret_cast<const uint8_t*>(value.data());
    auto height=ReadU64(encoded); auto index=ReadU32(encoded+8);
    string hash;
    if (!height||!DBGet("height/"+to_string(height),hash)||hash.size()!=32) throw runtime_error("Missing indexed height");
    array<uint8_t,32> block; memcpy(block.data(),hash.data(),32);
    string data;
    if (!DBGet(DBHashKey("block/",block),data)||data.size()<112) throw runtime_error("Missing indexed block");
    if (ReadU64(reinterpret_cast<const uint8_t*>(data.data()+64))!=height||memcmp(data.data()+80,block.data(),32)) throw runtime_error("Indexed block height mismatch");
    auto count=ReadU64(reinterpret_cast<const uint8_t*>(data.data()+72));
    if (index>=count||count>(data.size()-112)/176) throw runtime_error("Invalid transaction offset");
    memcpy(tx.data(),data.data()+112+size_t(index)*176,176);
    if (memcmp(tx.data()+80,key.data(),32)) throw runtime_error("Transaction position hash mismatch");
    return true;
}
void DBReadTx(const array<uint8_t,32>& key) {
    array<uint8_t,176> tx{};
    if (DBReadTx(key,tx)) PrintHexForString(string(reinterpret_cast<const char*>(tx.data()),tx.size()));
}
void DBAddBlock(rocksdb::WriteBatch& batch,const array<uint8_t,32>& key,const vector<uint8_t>& value) {
    if (value.size()<112) throw invalid_argument("Short block");
    auto height=ReadU64(value.data()+64);
    batch.Put(DBHashKey("block/",key),rocksdb::Slice(reinterpret_cast<const char*>(value.data()),value.size()));
    batch.Put(DBHashKey("header/",key),rocksdb::Slice(reinterpret_cast<const char*>(value.data()),112));
    batch.Put("height/"+to_string(height),rocksdb::Slice(reinterpret_cast<const char*>(key.data()),key.size()));
}
void DBWriteBlockALL(const array<uint8_t,32>& key,const vector<uint8_t>& value) {
    rocksdb::WriteBatch batch;
    DBAddBlock(batch,key,value);
    DBWriteBatch(batch);
}
array<uint8_t,32> DBReadBlockHashByHeight(uint64_t height) {
    string value; array<uint8_t,32> hash{};
    if (!DBGet("height/"+to_string(height),value)) return hash;
    if (value.size()!=32) throw runtime_error("Invalid height index");
    memcpy(hash.data(),value.data(),32); return hash;
}
vector<uint8_t> DBReadBlockHeader(const array<uint8_t,32>& hash) {
    string value;
    if (!DBGet(DBHashKey("header/",hash),value)) return {};
    if (value.size()!=112||memcmp(value.data()+80,hash.data(),32)) throw runtime_error("Invalid saved block header");
    return {value.begin(),value.end()};
}
vector<uint8_t> DBReadBlockByHash(const array<uint8_t,32>& key) {
    string value;
    if (!DBGet(DBHashKey("block/",key),value)) DBGet(DBHashKey("",key),value);
    return vector<uint8_t>(value.begin(),value.end());
}
vector<uint8_t> DBReadBlockByHeight(const string& height) {
    string value;
    if (!DBGet("height/"+height,value)&&!DBGet(height,value)) return {};
    if (value.size()!=32) throw runtime_error("Invalid height index");
    array<uint8_t,32> hash;
    memcpy(hash.data(),value.data(),32);
    return DBReadBlockByHash(hash);
}
uint64_t DBReadNumber(const string& key) {
    string value;
    if (!DBGet(key,value)) return 0;
    uint64_t number=0;
    auto result=from_chars(value.data(),value.data()+value.size(),number);
    if (result.ec!=errc()||result.ptr!=value.data()+value.size()) throw runtime_error("Invalid DB number: "+key);
    return number;
}
uint64_t DBReadBlockHeight() { return DBReadNumber("BlockHeight"); }
uint64_t DBReadBlockMaxHeight() { return DBReadNumber("BlockMaxHeight"); }
void DBWriteBlockHeight(uint64_t height) { CheckDBStatus(db->Put(rocksdb::WriteOptions(),"BlockHeight",to_string(height))); }
void DBWriteBlockMaxHeight(uint64_t height) { CheckDBStatus(db->Put(rocksdb::WriteOptions(),"BlockMaxHeight",to_string(height))); }
array<uint8_t,32> DBReadCurrentBlock() {
    string value;
    array<uint8_t,32> hash{};
    if (!DBGet("CurrentBlock",value)) return hash;
    if (value.size()!=32) throw runtime_error("Invalid current block hash");
    memcpy(hash.data(),value.data(),32);
    return hash;
}
// 调用方持有提交锁，在原子写入和账户发布完成后更新。
void PublishChainHead() {
    ChainHead head{DBReadBlockHeight(),DBReadNumber("ChainTx"),dbStateVersion,userBurned,users.size(),evmState.get_accounts().size(),DBReadCurrentBlock()};
    PublishedChain next; next.head=head; next.atUs=int64_t(GetSteadyTime()/1000);
    if (experimentIndex||nodeClock.candidateLifecycle||nodeClock.candidateValue||nodeClock.candidateMarginal) {
        auto previous=ReadPublishedChain();
        auto hash=head.hash; auto height=head.height;
        while (height&&next.tail.size()<256) {
            auto old=find_if(previous.tail.begin(),previous.tail.end(),[&](const auto& a) { return a.height==height&&a.hash==hash; });
            if (old!=previous.tail.end()) {
                // Previous tail is newest first. Reuse unchanged ancestors after
                // locating the common parent, including grouped appends/reorgs.
                for (;old!=previous.tail.end()&&next.tail.size()<256;++old) next.tail.push_back(*old);
                break;
            }
            auto total=height==head.height?head.transactions:DBReadNumber(DBHashKey("score/",hash));
            next.tail.push_back({height,total,hash});
            if (height==1) break;
            string header;
            if (!DBGet(DBHashKey("header/",hash),header)||header.size()!=112) break;
            memcpy(hash.data(),header.data(),32); --height;
        }
    }
    lock_guard lock(chainHeadMutex); publishedHead=head; publishedChain=std::move(next);
}
void DBWriteCurrentBlock(const array<uint8_t,32>& hash) {
    CheckDBStatus(db->Put(rocksdb::WriteOptions(),"CurrentBlock",rocksdb::Slice(reinterpret_cast<const char*>(hash.data()),32)));
}
bool DBCheckBlockUsers(const vector<uint8_t>& data) {
    if (data.size()<112) return false;
    auto waitStart=GetSteadyTime();
    shared_lock lock(dbCommitMutex);
    candidateLockWaitNs+=GetSteadyTime()-waitStart;
    auto current=DBReadBlockHeight();
    auto height=ReadU64(data.data()+64);
    // 未来高度先缓存内容；轮到提交时，使用它实际父块的状态完整执行。
    if (current<UINT64_MAX&&height>current+1) return true;
    auto previous=DBReadCurrentBlock();
    if (current==UINT64_MAX||height!=current+1||memcmp(data.data(),previous.data(),32)!=0) return false;
    auto result=make_unique<CachedExecution>(); result->data=data; result->parent=previous;
    result->version=dbStateVersion; result->height=height;
    auto start=GetSteadyTime();
    if (!DBApplyBlockUsers(data,result->users,&result->evm)) return false;
    candidateExecuteNs+=GetSteadyTime()-start;
    result->bytes=data.size()+result->users.size()*96;
    for (const auto& [address,account]:result->evm.state.get_accounts()) result->bytes+=256+account.code.size()+account.storage.size()*128;
    for (const auto& [hash,receipt]:result->evm.receipts) result->bytes+=96+receipt.size();
    if (result->bytes<=maxExecutionBytes) {
        lock_guard cacheLock(executionMutex);
        for (auto it=executionCache.begin();it!=executionCache.end();) {
            if ((*it)->data==data) { executionBytes-=(*it)->bytes; it=executionCache.erase(it); } else ++it;
        }
        while (!executionCache.empty()&&(executionCache.size()>=8||executionBytes+result->bytes>maxExecutionBytes)) {
            executionBytes-=executionCache.front()->bytes; executionCache.pop_front();
        }
        executionBytes+=result->bytes; executionCache.push_back(std::move(result));
    }
    return true;
}
// 一块的块体、交易、索引、当前高度和链头在同一个 batch 中提交。
bool DBCommitBlockLocked(const array<uint8_t,32>& hash,const vector<uint8_t>& data) {
    if (data.size()<112) return false;
    auto height=ReadU64(data.data()+64);
    auto num=ReadU64(data.data()+72);
    EvmPayloads payloads;
    if (!ReadEvmPayloads(data,payloads)) return false;
    auto currentHeight=DBReadBlockHeight();
    auto previousHash=DBReadCurrentBlock();
    if (currentHeight==UINT64_MAX||height!=currentHeight+1||memcmp(data.data(),previousHash.data(),32)!=0) return false;
    UserMap changed;
    EvmExecution execution;
    unique_ptr<CachedExecution> cached;
    {
        lock_guard cacheLock(executionMutex);
        for (auto it=executionCache.begin();it!=executionCache.end();++it) {
            if ((*it)->version==dbStateVersion&&(*it)->parent==previousHash&&(*it)->height==height&&(*it)->data==data) {
                executionBytes-=(*it)->bytes; cached=std::move(*it); executionCache.erase(it); break;
            }
        }
    }
    if (cached) { changed=std::move(cached->users); execution=std::move(cached->evm); executionCacheHits++; }
    else {
        executionCacheMisses++; auto start=GetSteadyTime();
        auto valid=DBApplyBlockUsers(data,changed,&execution); commitExecuteNs+=GetSteadyTime()-start;
        if (!valid) { invalidUserBlocks++; return false; }
    }
    if (baseFee&&num>(genesisSupply-userBurned)/baseFee) return false;
    auto burned=userBurned+num*baseFee;
    if (execution.gasUsed>genesisSupply-burned) return false;
    burned+=execution.gasUsed;
    auto prepareStart=GetSteadyTime();
    // 写盘前准备哈希桶；提交后直接转移新增账户节点，减少状态发布时的分配。
    if (users.size()+changed.size()>users.bucket_count()*users.max_load_factor()) users.reserve(max(users.size()*2,users.size()+changed.size()));
    rocksdb::WriteBatch batch;
    for (size_t i=0;i<num;i++) {
        auto tx=data.data()+112+i*176;
        array<uint8_t,32> txHash;
        memcpy(txHash.data(),tx+80,32);
        // 连续账户序号已排除历史重放和块内重复，直接写交易索引。
        array<uint8_t,12> position; WriteU64(position.data(),height); WriteU32(position.data()+8,uint32_t(i));
        batch.Put(DBHashKey("tx/",txHash),rocksdb::Slice(reinterpret_cast<const char*>(position.data()),position.size()));
        if (auto found=payloads.find(i);found!=payloads.end()) batch.Put(DBHashKey("input/",txHash),rocksdb::Slice(reinterpret_cast<const char*>(found->second.data()),found->second.size()));
    }
    for (const auto& [key,user]:changed) DBAddUser(batch,key,user);
    uint64_t accountWrites=0,slotWrites=0; auto persistStart=GetSteadyTime();
    if (execution.active) {
        for (const auto& address:execution.state.GetCleared()) {
            auto old=evmState.get_accounts().find(address);
            if (old!=evmState.get_accounts().end()) {
                for (const auto& [slot,value]:old->second.storage) if (value.current!=evmc::bytes32{}) {
                    batch.Delete(DBEvmSlotKey(address,slot)); slotWrites++;
                }
            }
        }
        for (const auto& address:execution.state.GetDeleted()) {
            batch.Delete(DBEvmKey("evm/",address)); batch.Delete(DBEvmKey("evm-code/",address)); accountWrites++;
        }
        for (const auto& [address,account]:execution.state.get_accounts()) {
            auto old=evmState.get_accounts().find(address);
            bool fresh=old==evmState.get_accounts().end(),cleared=execution.state.GetCleared().count(address);
            if (fresh||old->second.balance!=account.balance||old->second.nonce!=account.nonce) {
                batch.Put(DBEvmKey("evm/",address),SerializeEvmHeader(account)); accountWrites++;
            }
            if (fresh||old->second.code!=account.code) {
                auto key=DBEvmKey("evm-code/",address);
                if (account.code.empty()) batch.Delete(key);
                else batch.Put(key,rocksdb::Slice(reinterpret_cast<const char*>(account.code.data()),account.code.size()));
            }
            for (const auto& [slot,value]:account.storage) {
                auto previous=cleared?evmc::bytes32{}:evmState.ReadStorage(address,slot);
                if (previous==value.current) continue;
                auto key=DBEvmSlotKey(address,slot);
                if (value.current==evmc::bytes32{}) batch.Delete(key);
                else batch.Put(key,rocksdb::Slice(reinterpret_cast<const char*>(value.current.bytes),32));
                slotWrites++;
            }
        }
        for (const auto& [key,value]:execution.receipts) batch.Put(DBHashKey("receipt/",key),value);
    }
    evmPersistNs+=GetSteadyTime()-persistStart;
    batch.Put("UserBurned",to_string(burned));
    batch.Put("CurrentBlock",rocksdb::Slice(reinterpret_cast<const char*>(hash.data()),32));
    batch.Put("BlockHeight",to_string(height));
    batch.Put("height/"+to_string(height),rocksdb::Slice(reinterpret_cast<const char*>(hash.data()),32));
    auto total=DBReadNumber("ChainTx");
    if (UINT64_MAX-total<num) return false;
    batch.Put("ChainTx",to_string(total+num));
    // 记录本次改写的旧值，回滚与新分支重放共用一个原子写批次。
    struct Undo:rocksdb::WriteBatch::Handler {
        rocksdb::WriteBatch saved;
        void Save(const rocksdb::Slice& key) {
            // 已连续执行的账户序号保证这些索引是新键；直接记录删除即可。
            if (key.starts_with("tx/")||key.starts_with("txheight/")||key.starts_with("input/")||key.starts_with("receipt/")||key.starts_with("height/")) { saved.Delete(key); return; }
            if (key.starts_with("user/")&&key.size()==37) {
                array<uint8_t,32> address; memcpy(address.data(),key.data()+5,32);
                auto found=users.find(address);
                if (found==users.end()) saved.Delete(key); else DBAddUser(saved,address,found->second);
                return;
            }
            string value;
            if (DBGet(key.ToString(),value)) saved.Put(key,value); else saved.Delete(key);
        }
        void Put(const rocksdb::Slice& key,const rocksdb::Slice&) override { Save(key); }
        void Delete(const rocksdb::Slice& key) override { Save(key); }
    } undo;
    CheckDBStatus(batch.Iterate(&undo));
    batch.Put(DBHashKey("undo/",hash),undo.saved.Data());
    batch.Put(DBHashKey("score/",hash),to_string(total+num));
    batch.Put(DBHashKey("block/",hash),rocksdb::Slice(reinterpret_cast<const char*>(data.data()),data.size()));
    batch.Put(DBHashKey("header/",hash),rocksdb::Slice(reinterpret_cast<const char*>(data.data()),112));
    SaveBranchMemory(batch);
    dbPrepareNs+=GetSteadyTime()-prepareStart;
    DBWriteBatch(batch);
    if (!branchBatch) PublishPoolUsers(&changed);
    auto writeCount=changed.size();
    for (const auto& [key,user]:changed) {
        auto found=users.find(key);
        if (found!=users.end()) found->second=user;
    }
    users.merge(changed);
    userBurned=burned;
    if (execution.active) ApplyEvmChanges(execution.state);
    evmAccountWrites+=accountWrites; evmSlotWrites+=slotWrites;
    InvalidateExecutionCache();
    userWriteCount+=writeCount;
    if (!branchBatch) PublishChainHead();
    return true;
}
bool DBCommitBlock(const array<uint8_t,32>& hash,const vector<uint8_t>& data) {
    auto waitStart=GetSteadyTime();
    lock_guard lock(dbCommitMutex);
    commitLockWaitNs+=GetSteadyTime()-waitStart;
    return DBCommitBlockLocked(hash,data);
}
#endif
