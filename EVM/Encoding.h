#ifndef CHAIN_EVM_ENCODING_H
#define CHAIN_EVM_ENCODING_H
#include <map>
#include <span>
#include <vector>
#include <sodium.h>
#include "../Tool/Tool.h"
using namespace std;
constexpr size_t maxBlockBytes=2*1024*1024;
constexpr size_t maxEvmInput=49152;
constexpr uint64_t maxEvmGas=10000000,maxBlockGas=30000000;
using EvmPayloads=map<size_t,vector<uint8_t>>;
bool ReadEvmPayloads(span<const uint8_t> block,EvmPayloads& result) {
    result.clear();
    if (block.size()<112||block.size()>maxBlockBytes) return false;
    auto count=ReadU64(block.data()+72);
    if (count>6000||count>(block.size()-112)/176) return false;
    size_t offset=112+count*176;
    if (offset==block.size()) return true;
    if (block.size()-offset<8||memcmp(block.data()+offset,"EVT1",4)) return false;
    auto entries=ReadU32(block.data()+offset+4); offset+=8;
    if (!entries||entries>count) return false;
    for (size_t i=0;i<entries;i++) {
        if (block.size()-offset<8) return false;
        auto index=ReadU32(block.data()+offset),length=ReadU32(block.data()+offset+4); offset+=8;
        if (index>=count||length<45||length>45+maxEvmInput||length>block.size()-offset||(!result.empty()&&index<=result.rbegin()->first)) return false;
        result.emplace(index,vector<uint8_t>(block.begin()+offset,block.begin()+offset+length)); offset+=length;
    }
    return offset==block.size();
}
void AppendEvmPayloads(vector<uint8_t>& block,const EvmPayloads& payloads) {
    if (payloads.empty()) return;
    auto offset=block.size(); block.resize(offset+8);
    memcpy(block.data()+offset,"EVT1",4); WriteU32(block.data()+offset+4,uint32_t(payloads.size()));
    for (const auto& [index,data]:payloads) {
        offset=block.size(); block.resize(offset+8+data.size());
        WriteU32(block.data()+offset,uint32_t(index)); WriteU32(block.data()+offset+4,uint32_t(data.size()));
        memcpy(block.data()+offset+8,data.data(),data.size());
    }
}
bool ValidEvmPayload(span<const uint8_t> payload) {
    return payload.size()>=45&&payload.size()<=45+maxEvmInput&&memcmp(payload.data(),"EVM1",4)==0&&payload[4]<=1&&ReadU64(payload.data()+5)>=21000&&ReadU64(payload.data()+5)<=maxEvmGas;
}
array<uint8_t,32> EvmTransactionHash(span<const uint8_t> tx,span<const uint8_t> payload) {
    array<uint8_t,32> hash;
    crypto_generichash_state state;
    crypto_generichash_init(&state,nullptr,0,32);
    crypto_generichash_update(&state,tx.data(),80);
    crypto_generichash_update(&state,payload.data(),payload.size());
    crypto_generichash_final(&state,hash.data(),32);
    return hash;
}
#endif
