#ifndef CHAIN_DB_H
#define CHAIN_DB_H
#include <atomic>
#include <charconv>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <vector>
#include <rocksdb/db.h>
#include <rocksdb/options.h>
#include <rocksdb/write_batch.h>
#include <rocksdb/statistics.h>
#include <rocksdb/filter_policy.h>
#include <rocksdb/table.h>
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
atomic<uint64_t> dbWriteNs{0},dbWriteCount{0};
UserMap users;
uint64_t userBurned=0;
atomic<uint64_t> userCheckNs{0},userWriteCount{0},invalidUserBlocks{0};
// 分支切换使用临时批次；所有读取看到同一批次中的回滚和重放结果。
thread_local rocksdb::WriteBatchWithIndex* branchBatch=nullptr;
atomic<uint64_t> reorgCount{0};

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
    CheckDBStatus(rocksdb::DB::Open(options,path,&db));
    string version;
    if (DBGet("SchemaVersion",version)) {
        if (version!="4") throw runtime_error("EVM and fork recovery require schema 4. Use a NEW --data directory with --genesis.");
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
    if (!version.empty()) {
        string burned;
        if (!DBGet("UserBurned",burned)) throw runtime_error("Missing fee total");
        auto result=from_chars(burned.data(),burned.data()+burned.size(),userBurned);
        if (result.ec!=errc()||result.ptr!=burned.data()+burned.size()||userBurned>genesisSupply||balanceTotal!=genesisSupply-userBurned) throw runtime_error("Account supply mismatch");
    }
    spdlog::info("RocksDB Started. Path:{}, Sync:{}",path,sync);
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
    rocksdb::WriteOptions writeOptions;
    writeOptions.sync=dbSync;
    CheckDBStatus(db->Write(writeOptions,&batch));
    dbWriteNs+=GetSteadyTime()-start;
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
bool DBApplyBlockUsers(const vector<uint8_t>& data,UserMap& changed,EvmExecution* output=nullptr) {
    if (data.size()<112) return false;
    auto num=ReadU64(data.data()+72);
    EvmPayloads payloads;
    if (!ReadEvmPayloads(data,payloads)) return false;
    EvmExecution local;
    auto& execution=output?*output:local;
    evmone::state::BlockInfo context{};
    context.number=ReadU64(data.data()+64); context.timestamp=context.number;
    context.gas_limit=maxBlockGas; context.base_fee=1;
    if (!payloads.empty()) {
        // 区块时间采用确定的逻辑轮次，所有节点取得相同环境。
        for (int64_t height=max(int64_t(1),context.number-256);height<context.number;height++) {
            string hash;
            if (DBGet("height/"+to_string(height),hash)&&hash.size()==32) {
                evmc::bytes32 value; memcpy(value.bytes,hash.data(),32); context.known_block_hashes.emplace(height,value);
            }
        }
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

void DBReadTx(const array<uint8_t,32>& key) {
    string value;
    if (!DBGet(DBHashKey("tx/",key),value)) DBGet(DBHashKey("",key),value);
    PrintHexForString(value);
}
void DBAddBlock(rocksdb::WriteBatch& batch,const array<uint8_t,32>& key,const vector<uint8_t>& value) {
    if (value.size()<112) throw invalid_argument("Short block");
    auto height=ReadU64(value.data()+64);
    batch.Put(DBHashKey("block/",key),rocksdb::Slice(reinterpret_cast<const char*>(value.data()),value.size()));
    batch.Put("height/"+to_string(height),rocksdb::Slice(reinterpret_cast<const char*>(key.data()),key.size()));
}
void DBWriteBlockALL(const array<uint8_t,32>& key,const vector<uint8_t>& value) {
    rocksdb::WriteBatch batch;
    DBAddBlock(batch,key,value);
    DBWriteBatch(batch);
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
void DBWriteCurrentBlock(const array<uint8_t,32>& hash) {
    CheckDBStatus(db->Put(rocksdb::WriteOptions(),"CurrentBlock",rocksdb::Slice(reinterpret_cast<const char*>(hash.data()),32)));
}
bool DBCheckBlockUsers(const vector<uint8_t>& data) {
    shared_lock lock(dbCommitMutex);
    auto current=DBReadBlockHeight();
    auto height=ReadU64(data.data()+64);
    // 未来高度先缓存内容；轮到提交时，使用它实际父块的状态完整执行。
    if (current<UINT64_MAX&&height>current+1) return true;
    auto previous=DBReadCurrentBlock();
    if (current==UINT64_MAX||height!=current+1||memcmp(data.data(),previous.data(),32)!=0) return false;
    UserMap changed;
    return DBApplyBlockUsers(data,changed);
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
    if (!DBApplyBlockUsers(data,changed,&execution)) { invalidUserBlocks++; return false; }
    if (baseFee&&num>(genesisSupply-userBurned)/baseFee) return false;
    auto burned=userBurned+num*baseFee;
    if (execution.gasUsed>genesisSupply-burned) return false;
    burned+=execution.gasUsed;
    // 写盘前准备哈希桶；提交后直接转移新增账户节点，减少状态发布时的分配。
    if (users.size()+changed.size()>users.bucket_count()*users.max_load_factor()) users.reserve(max(users.size()*2,users.size()+changed.size()));
    rocksdb::WriteBatch batch;
    for (size_t i=0;i<num;i++) {
        auto tx=data.data()+112+i*176;
        array<uint8_t,32> txHash;
        memcpy(txHash.data(),tx+80,32);
        // 连续账户序号已排除历史重放和块内重复，直接写交易索引。
        batch.Put(DBHashKey("tx/",txHash),rocksdb::Slice(reinterpret_cast<const char*>(tx),176));
        if (auto found=payloads.find(i);found!=payloads.end()) batch.Put(DBHashKey("input/",txHash),rocksdb::Slice(reinterpret_cast<const char*>(found->second.data()),found->second.size()));
    }
    for (const auto& [key,user]:changed) DBAddUser(batch,key,user);
    if (execution.active) {
        for (const auto& [address,account]:execution.state.get_accounts()) {
            string key="evm/"; key.append(reinterpret_cast<const char*>(address.bytes),20);
            auto value=SerializeEvmAccount(account);
            auto old=evmState.get_accounts().find(address);
            if (old==evmState.get_accounts().end()||SerializeEvmAccount(old->second)!=value) batch.Put(key,value);
        }
        for (const auto& [address,account]:evmState.get_accounts()) if (!execution.state.get_accounts().count(address)) {
            string key="evm/"; key.append(reinterpret_cast<const char*>(address.bytes),20); batch.Delete(key);
        }
        for (const auto& [key,value]:execution.receipts) batch.Put(DBHashKey("receipt/",key),value);
    }
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
            if (key.starts_with("tx/")||key.starts_with("input/")||key.starts_with("receipt/")||key.starts_with("height/")) { saved.Delete(key); return; }
            if (key.starts_with("user/")&&key.size()==37) {
                array<uint8_t,32> address; memcpy(address.data(),key.data()+5,32);
                auto found=users.find(address);
                if (found==users.end()) saved.Delete(key); else DBAddUser(saved,address,found->second);
                return;
            }
            if (key.starts_with("evm/")&&key.size()==24) {
                evmc::address address; memcpy(address.bytes,key.data()+4,20);
                auto found=evmState.get_accounts().find(address);
                if (found==evmState.get_accounts().end()) saved.Delete(key); else saved.Put(key,SerializeEvmAccount(found->second));
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
    DBWriteBatch(batch);
    auto writeCount=changed.size();
    for (const auto& [key,user]:changed) {
        auto found=users.find(key);
        if (found!=users.end()) found->second=user;
    }
    users.merge(changed);
    userBurned=burned;
    if (execution.active) evmState=std::move(execution.state);
    userWriteCount+=writeCount;
    return true;
}
bool DBCommitBlock(const array<uint8_t,32>& hash,const vector<uint8_t>& data) {
    lock_guard lock(dbCommitMutex);
    return DBCommitBlockLocked(hash,data);
}
#endif
