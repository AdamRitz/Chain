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
using namespace std;

unique_ptr<rocksdb::DB> db;
rocksdb::Options options;
shared_mutex dbCommitMutex;
bool dbSync=true;
bool dbLegacyKeys=true;
atomic<uint64_t> dbWriteNs{0},dbWriteCount{0};

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
    // 新交易绝大多数不在库中，Bloom filter 避免每次都读 SST 数据块。
    rocksdb::BlockBasedTableOptions tableOptions;
    tableOptions.filter_policy.reset(rocksdb::NewBloomFilterPolicy(10,false));
    options.table_factory.reset(rocksdb::NewBlockBasedTableFactory(tableOptions));
    CheckDBStatus(rocksdb::DB::Open(options,path,&db));
    string version;
    dbLegacyKeys=!DBGet("SchemaVersion",version);
    if (!dbLegacyKeys&&version!="2") throw runtime_error("Unsupported DB schema");
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
    if (DBGet(DBHashKey("tx/",key),value)) return true;
    // 兼容旧版本未加前缀的交易键。
    return dbLegacyKeys&&DBGet(DBHashKey("",key),value)&&value.size()==176;
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
    rocksdb::WriteBatch batch;
    DBAddBlock(batch,hash,data);
    for (size_t i=0;i<num;i++) {
        auto tx=data.data()+112+i*176;
        array<uint8_t,32> txHash;
        memcpy(txHash.data(),tx+80,32);
        if (DBHasTx(txHash)) return false;
        batch.Put(DBHashKey("tx/",txHash),rocksdb::Slice(reinterpret_cast<const char*>(tx),176));
    }
    batch.Put("CurrentBlock",rocksdb::Slice(reinterpret_cast<const char*>(hash.data()),32));
    batch.Put("BlockHeight",to_string(height));
    DBWriteBatch(batch);
    return true;
}
#endif
