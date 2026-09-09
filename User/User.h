#ifndef CHAIN_USER_H
#define CHAIN_USER_H
#include <fstream>
#include <map>
#include <span>
#include <unordered_map>
#include <nlohmann/json.hpp>
#include <sodium.h>
#include "../Tool/Tool.h"
using namespace std;

struct User {
    uint64_t balance=0;
    uint64_t nonce=0;
    bool operator==(const User&) const = default;
};
using UserMap=unordered_map<array<uint8_t,32>,User,GetMapHash>;
map<array<uint8_t,32>,User> genesisUsers;
uint64_t baseFee=0,genesisSupply=0;
array<uint8_t,32> genesisRoot{};
bool genesisConfigured=false;

uint64_t ReadUserNumber(const nlohmann::json& value) {
    if (value.is_number_unsigned()) return value.get<uint64_t>();
    if (value.is_number_integer()&&value.get<int64_t>()>=0) return uint64_t(value.get<int64_t>());
    throw invalid_argument("Expected a nonnegative 64-bit integer");
}
nlohmann::json GetGenesisConfig() {
    auto accounts=nlohmann::json::array();
    for (const auto& [key,user]:genesisUsers) accounts.push_back({{"public_key",U32ToHex(key)},{"balance",user.balance}});
    return {{"base_fee",baseFee},{"accounts",accounts}};
}
void SetGenesisUsers(const nlohmann::json& config) {
    if (!config.is_object()||!config.contains("accounts")||!config["accounts"].is_array()) throw invalid_argument("Genesis requires an accounts array");
    if (config["accounts"].size()>1000000) throw invalid_argument("Too many genesis accounts");
    map<array<uint8_t,32>,User> parsed;
    uint64_t supply=0;
    auto fee=config.contains("base_fee")?ReadUserNumber(config["base_fee"]):0;
    for (const auto& item:config["accounts"]) {
        auto key=HexToU32(item.at("public_key").get<string>());
        auto balance=ReadUserNumber(item.at("balance"));
        if (UINT64_MAX-supply<balance) throw invalid_argument("Genesis supply overflow");
        if (!parsed.emplace(key,User{balance,0}).second) throw invalid_argument("Duplicate genesis account");
        supply+=balance;
    }
    genesisUsers=std::move(parsed);
    genesisSupply=supply;
    baseFee=fee;
    // 固定字节格式，排序后的账户分配和基础费共同绑定到创世块。
    crypto_generichash_state state;
    crypto_generichash_init(&state,nullptr,0,32);
    const string domain="Chain accounts v3";
    crypto_generichash_update(&state,reinterpret_cast<const uint8_t*>(domain.data()),domain.size());
    array<uint8_t,16> numbers;
    WriteU64(numbers.data(),baseFee);
    WriteU64(numbers.data()+8,genesisUsers.size());
    crypto_generichash_update(&state,numbers.data(),numbers.size());
    for (const auto& [key,user]:genesisUsers) {
        crypto_generichash_update(&state,key.data(),key.size());
        WriteU64(numbers.data(),user.balance);
        crypto_generichash_update(&state,numbers.data(),8);
    }
    crypto_generichash_final(&state,genesisRoot.data(),32);
    genesisConfigured=true;
}
void LoadGenesisUsers(const string& path) {
    ifstream input(path);
    if (!input) throw runtime_error("Cannot open genesis configuration: "+path);
    nlohmann::json config;
    input>>config;
    SetGenesisUsers(config);
}
User GetUser(const UserMap& users,const array<uint8_t,32>& key) {
    auto found=users.find(key);
    return found==users.end()?User{}:found->second;
}
User GetUser(const UserMap& users,const UserMap& changed,const array<uint8_t,32>& key) {
    auto found=changed.find(key);
    return found==changed.end()?GetUser(users,key):found->second;
}
bool CheckUserTx(const User& user,uint64_t amount,uint64_t nonce,bool pending=false) {
    if (!amount||user.nonce==UINT64_MAX||nonce<=user.nonce) return false;
    if (pending) {
        if (nonce-user.nonce>4096) return false;
    } else if (nonce!=user.nonce+1) return false;
    return user.balance>=baseFee&&amount<=user.balance-baseFee;
}
// 调用方持有数据库提交锁；changed 只保存本次执行触及的账户。
bool ApplyUserTx(const UserMap& users,UserMap& changed,span<const uint8_t,176> tx) {
    array<uint8_t,32> sender,receiver;
    memcpy(sender.data(),tx.data(),32);
    memcpy(receiver.data(),tx.data()+32,32);
    auto amount=ReadU64(tx.data()+64),nonce=ReadU64(tx.data()+72);
    auto from=GetUser(users,changed,sender);
    if (!CheckUserTx(from,amount,nonce)) return false;
    if (sender==receiver) {
        from.balance-=baseFee;
        from.nonce=nonce;
        changed[sender]=from;
    } else {
        auto to=GetUser(users,changed,receiver);
        if (to.balance>UINT64_MAX-amount) return false;
        from.balance-=amount;
        from.balance-=baseFee;
        from.nonce=nonce;
        to.balance+=amount;
        changed[sender]=from;
        changed[receiver]=to;
    }
    return true;
}
#endif
