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
using namespace std;
std::unique_ptr<rocksdb::DB> db;
rocksdb::Options options;
void InitDB() {
    options.create_if_missing = true;
    rocksdb::Status s = rocksdb::DB::Open(options,"test",&db);
}
void DBWriteTx(std::array<uint8_t,32> key,std::array<uint8_t,176> value) {
    db->Put(rocksdb::WriteOptions(),rocksdb::Slice(reinterpret_cast<const char*>(key.data()), key.size()),rocksdb::Slice(reinterpret_cast<const char*>(value.data()), value.size()));
}

void DBReadTx(std::array<uint8_t,32> key) {
    string s ;
    db->Get(rocksdb::ReadOptions(),rocksdb::Slice(reinterpret_cast<const char*>(key.data()), key.size()),&s);
    PrintHexForString(s);
}
int DBReadBlockHeight() {
    string s;
    db->Get(rocksdb::ReadOptions(),"BlockHeight",&s);
    return stoi(s);
}
void DBWriteBlockHeight(int height) {
    db->Put(rocksdb::WriteOptions(),"BlockHeight",to_string(height));
}

array<uint8_t,32> DBReadCurrentBlock() {
    std::array<uint8_t, 32> hash{};
    string s;
    db->Get(rocksdb::ReadOptions(),"CurrentBlock",&s);
    memcpy(hash.data(),s.data(),32);
    return hash;
}

void DBWriteCurrentBlock(array<uint8_t,32> hash) {
    db->Put(rocksdb::WriteOptions(),"CurrentBlock",rocksdb::Slice(reinterpret_cast<const char*>(hash.data())));
}
#endif //CHAIN_DB_H