//
// Created by DonQuixote on 2026/5/5.
//

#ifndef CHAIN_DB_H
#define CHAIN_DB_H
#include <cassert>
#include <iostream>
#include <memory>
#include <string>
#include "../Tool/Tool.h"
#include "rocksdb/db.h"
#include "rocksdb/options.h"
#include <vector>
#include <spdlog/spdlog.h>
using namespace std;
std::unique_ptr<rocksdb::DB> db;
rocksdb::Options options;
//---------------------------------------------------初始化数据库--------------------------------------------------------------------------------------------------------------------------
void InitDB() {
    options.create_if_missing = true;
    rocksdb::Status s = rocksdb::DB::Open(options,"test",&db);
    spdlog::info("RocksDB Started.");
}
//---------------------------------------------------交易读写--------------------------------------------------------------------------------------------------------------------------
void DBWriteTx(const std::array<uint8_t,32>& key,const std::array<uint8_t,176>& value) {
    db->Put(rocksdb::WriteOptions(),rocksdb::Slice(reinterpret_cast<const char*>(key.data()), key.size()),rocksdb::Slice(reinterpret_cast<const char*>(value.data()), value.size()));
}

void DBReadTx(const array<uint8_t,32>& key) {
    string s ;
    db->Get(rocksdb::ReadOptions(),rocksdb::Slice(reinterpret_cast<const char*>(key.data()), key.size()),&s);
    PrintHexForString(s);
}

//---------------------------------------------------区块读写--------------------------------------------------------------------------------------------------------------------------

void DBWriteBlock(const array<uint8_t,32>& key,const vector<uint8_t>& value) {
    uint64_t height=0;
    memcpy(&height,value.data()+64,8);
    // 存 Hash - > 区块
    db->Put(rocksdb::WriteOptions(),rocksdb::Slice(reinterpret_cast<const char*>(key.data()),key.size()),rocksdb::Slice(reinterpret_cast<const char*>(value.data()),value.size()));
    // 存高度 - > Hash
    db->Put(rocksdb::WriteOptions(),to_string(height),rocksdb::Slice(reinterpret_cast<const char*>(key.data()),key.size()));
}

vector<uint8_t> DBReadBlockByHash(const array<uint8_t,32>& key) {
    vector<uint8_t> result;
    string s;
    db->Get(rocksdb::ReadOptions(),rocksdb::Slice(reinterpret_cast<const char*>(key.data()),key.size()),&s);
    result.resize(s.size());
    memcpy(result.data(),s.data(),s.size());
    return result;
}
vector<uint8_t> DBReadBlockByHeight(const string& key) {
    vector<uint8_t> result;
    string s;
    db->Get(rocksdb::ReadOptions(),key,&s);
    result.resize(s.size());
    memcpy(result.data(),s.data(),s.size());
    return result;
}

//---------------------------------------------------------区块链参数读写：高度，上个块的 Hash---------------------------------------------------------------------------------------------------
uint64_t DBReadBlockHeight() {
    string s;
    db->Get(rocksdb::ReadOptions(),"BlockHeight",&s);
    return stoi(s);
}
void DBWriteBlockHeight(uint64_t height) {
    db->Put(rocksdb::WriteOptions(),"BlockHeight",to_string(height));
}
uint64_t DBReadBlockMaxHeight() {
    string s;
    db->Get(rocksdb::ReadOptions(),"BlockMaxHeight",&s);
    return stoi(s);
}
void DBWriteBlockMaxHeight(uint64_t height) {
    db->Put(rocksdb::WriteOptions(),"BlockMaxHeight",to_string(height));
}
array<uint8_t,32> DBReadCurrentBlock() {
    std::array<uint8_t, 32> hash{};
    string s;
    db->Get(rocksdb::ReadOptions(),"CurrentBlock",&s);
    memcpy(hash.data(),s.data(),32);
    return hash;
}
void DBWriteCurrentBlock(const array<uint8_t,32>& hash) {
    db->Put(rocksdb::WriteOptions(),"CurrentBlock",rocksdb::Slice(reinterpret_cast<const char*>(hash.data()),hash.size()));
}

#endif //CHAIN_DB_H