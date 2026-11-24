#ifndef CHAIN_EVM_ENCODING_H
#define CHAIN_EVM_ENCODING_H
#include <map>
#include <span>
#include <vector>
#include <sodium.h>
#include "../Tool/Tool.h"
#include "../Consensus/VdfRules.h"
using namespace std;
constexpr size_t maxBlockBytes=2*1024*1024;
constexpr size_t maxProtocolTx=10000;
constexpr size_t maxEvmInput=49152;
constexpr uint64_t maxEvmGas=10000000,maxBlockGas=30000000;
using EvmPayloads=map<size_t,vector<uint8_t>>;
// Walk forward from the declared transaction boundary. Signature bytes and
// EVM input bytes may contain VDB1; neither is a delimiter for this parser.
bool ReadBlockPayloads(span<const uint8_t> block,EvmPayloads* evm,
                       optional<chain::vdf_consensus::Extension>* vdf) {
    if (evm) evm->clear();
    if (vdf) vdf->reset();
    if (block.size()<112||block.size()>maxBlockBytes) return false;
    auto count=ReadU64(block.data()+72);
    if (count>maxProtocolTx||count>(block.size()-112)/176) return false;
    size_t offset=112+count*176;
    if (offset==block.size()) return true;
    EvmPayloads parsed;
    if (block.size()-offset>=4&&!memcmp(block.data()+offset,"EVT1",4)) {
        if (block.size()-offset<8) return false;
        auto entries=ReadU32(block.data()+offset+4); offset+=8;
        if (!entries||entries>count) return false;
        uint32_t previous=0;
        for (size_t i=0;i<entries;i++) {
            if (block.size()-offset<8) return false;
            auto index=ReadU32(block.data()+offset),length=ReadU32(block.data()+offset+4); offset+=8;
            if (index>=count||length<45||length>45+maxEvmInput||length>block.size()-offset||(i&&index<=previous)) return false;
            if (evm) parsed.emplace(index,vector<uint8_t>(block.begin()+offset,block.begin()+offset+length));
            previous=index; offset+=length;
        }
    }
    optional<chain::vdf_consensus::Extension> extension;
    if (offset<block.size()) {
        chain::vdf_consensus::Extension decoded;
        if (!chain::vdf_consensus::Parse(block.subspan(offset),decoded)) return false;
        extension=std::move(decoded);
    }
    if (evm) *evm=std::move(parsed);
    if (vdf) *vdf=std::move(extension);
    return true;
}
bool ReadEvmPayloads(span<const uint8_t> block,EvmPayloads& result) {
    return ReadBlockPayloads(block,&result,nullptr);
}
bool ReadVdfExtension(span<const uint8_t> block,optional<chain::vdf_consensus::Extension>& result) {
    return ReadBlockPayloads(block,nullptr,&result);
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
