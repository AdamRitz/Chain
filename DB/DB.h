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
#include <spdlog/spdlog.h>
#include "../Tool/Tool.h"
#include "../Time/Time.h"
#include "../User/User.h"
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

void CheckDBStatus(const rocksdb::Status& status) {
    if (!status.ok()) throw runtime_error("RocksDB: "+status.ToString());
}
string DBHashKey(const char* prefix,const array<uint8_t,32>& hash) {
    string key(prefix);
    key.append(reinterpret_cast<const char*>(hash.data()),hash.size());
    return key;
}
bool DBGet(const string& key,string& value) {
    auto status=db->Get(rocksdb::ReadOptions(),key,&value);
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
        if (version!="3") throw runtime_error("Account model requires schema 3. Keep the old database and use a NEW --data directory with --genesis.");
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
    if (!version.empty()) {
        string burned;
        if (!DBGet("UserBurned",burned)) throw runtime_error("Missing fee total");
        auto result=from_chars(burned.data(),burned.data()+burned.size(),userBurned);
        if (result.ec!=errc()||result.ptr!=burned.data()+burned.size()||userBurned>genesisSupply||balanceTotal!=genesisSupply-userBurned) throw runtime_error("Account supply mismatch");
    }
    spdlog::info("RocksDB Started. Path:{}, Sync:{}",path,sync);
}
void DBWriteBatch(rocksdb::WriteBatch& batch) {
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
bool DBApplyBlockUsers(const vector<uint8_t>& data,UserMap& changed) {
    if (data.size()<112) return false;
    auto num=ReadU64(data.data()+72);
    if (num>6000||data.size()!=112+num*176) return false;
    changed.reserve(min(size_t(num*2),users.size()+size_t(num)));
    auto start=GetSteadyTime();
    for (size_t i=0;i<num;i++) {
        if (!ApplyUserTx(users,changed,span<const uint8_t,176>(data.data()+112+i*176,176))) {
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
bool DBCommitBlock(const array<uint8_t,32>& hash,const vector<uint8_t>& data) {
    lock_guard lock(dbCommitMutex);
    if (data.size()<112) return false;
    auto height=ReadU64(data.data()+64);
    auto num=ReadU64(data.data()+72);
    if (num>(data.size()-112)/176||data.size()!=112+num*176) return false;
    auto currentHeight=DBReadBlockHeight();
    auto previousHash=DBReadCurrentBlock();
    if (currentHeight==UINT64_MAX||height!=currentHeight+1||memcmp(data.data(),previousHash.data(),32)!=0) return false;
    UserMap changed;
    if (!DBApplyBlockUsers(data,changed)) { invalidUserBlocks++; return false; }
    if (baseFee&&num>(genesisSupply-userBurned)/baseFee) return false;
    auto burned=userBurned+num*baseFee;
    // 写盘前准备哈希桶；提交后直接转移新增账户节点，减少状态发布时的分配。
    if (users.size()+changed.size()>users.bucket_count()*users.max_load_factor()) users.reserve(max(users.size()*2,users.size()+changed.size()));
    rocksdb::WriteBatch batch;
    DBAddBlock(batch,hash,data);
    for (size_t i=0;i<num;i++) {
        auto tx=data.data()+112+i*176;
        array<uint8_t,32> txHash;
        memcpy(txHash.data(),tx+80,32);
        // 连续账户序号已排除历史重放和块内重复，直接写交易索引。
        batch.Put(DBHashKey("tx/",txHash),rocksdb::Slice(reinterpret_cast<const char*>(tx),176));
    }
    for (const auto& [key,user]:changed) DBAddUser(batch,key,user);
    batch.Put("UserBurned",to_string(burned));
    batch.Put("CurrentBlock",rocksdb::Slice(reinterpret_cast<const char*>(hash.data()),32));
    batch.Put("BlockHeight",to_string(height));
    DBWriteBatch(batch);
    auto writeCount=changed.size();
    for (const auto& [key,user]:changed) {
        auto found=users.find(key);
        if (found!=users.end()) found->second=user;
    }
    users.merge(changed);
    userBurned=burned;
    userWriteCount+=writeCount;
    return true;
}
#endif
