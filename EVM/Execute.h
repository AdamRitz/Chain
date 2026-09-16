#ifndef CHAIN_EVM_EXECUTE_H
#define CHAIN_EVM_EXECUTE_H
#include <state/host.hpp>
#include <evmone/evmone.h>
#include "Encoding.h"
#include "../User/User.h"
using EvmState=evmone::state::State;
using EvmAccount=evmone::state::Account;
EvmState evmState;
constexpr uint64_t evmChainId=20260916;
EvmState CopyEvmState(const EvmState& state) {
    EvmState copy;
    copy.get_accounts()=state.get_accounts();
    return copy;
}
evmc::address GetEvmAddress(const array<uint8_t,32>& publicKey) {
    auto hash=ethash::keccak256(publicKey.data(),publicKey.size());
    evmc::address address;
    memcpy(address.bytes,hash.bytes+12,20);
    return address;
}
string EvmHex(span<const uint8_t> bytes) {
    string text(bytes.size()*2,'0');
    const char* digits="0123456789abcdef";
    for (size_t i=0;i<bytes.size();i++) { text[2*i]=digits[bytes[i]>>4]; text[2*i+1]=digits[bytes[i]&15]; }
    return text;
}
vector<uint8_t> EvmBytes(string text) {
    if (text.starts_with("0x")) text.erase(0,2);
    auto parsed=evmc::from_hex(text);
    if (!parsed) throw invalid_argument("Invalid hex bytes");
    return {parsed->begin(),parsed->end()};
}
string SerializeEvmAccount(const EvmAccount& account) {
    auto storage=nlohmann::json::object();
    for (const auto& [key,value]:account.storage) if (value.current!=evmc::bytes32{}) storage[EvmHex(key.bytes)]=EvmHex(value.current.bytes);
    return nlohmann::json({{"balance",intx::to_string(account.balance)}, {"nonce",account.nonce},
        {"code",EvmHex({account.code.data(),account.code.size()})},{"storage",storage}}).dump();
}
EvmAccount UnserializeEvmAccount(const string& value) {
    auto saved=nlohmann::json::parse(value);
    EvmAccount account;
    account.balance=intx::from_string<intx::uint256>(saved.at("balance").get<string>().c_str());
    account.nonce=saved.at("nonce").get<uint64_t>();
    auto code=EvmBytes(saved.at("code").get<string>());
    account.code.assign(code.data(),code.size());
    for (const auto& [key,text]:saved.at("storage").items()) {
        auto k=HexToU32(key),v=HexToU32(text.get<string>());
        evmc::bytes32 index,word;
        memcpy(index.bytes,k.data(),32); memcpy(word.bytes,v.data(),32);
        account.storage[index]={word,word,EVMC_ACCESS_COLD};
    }
    return account;
}
bool CheckEvmUserTx(const User& user,span<const uint8_t> tx,span<const uint8_t> payload,bool pending=false) {
    if (!ValidEvmPayload(payload)||memcmp(payload.data()+13,genesisRoot.data(),32)) return false;
    for (size_t i=32;i<44;i++) if (tx[i]) return false;
    if (payload[4]==0) for (size_t i=44;i<64;i++) if (tx[i]) return false;
    auto nonce=ReadU64(tx.data()+72),amount=ReadU64(tx.data()+64),gas=ReadU64(payload.data()+5);
    uint64_t intrinsic=payload[4]==0?53000:21000;
    for (size_t i=45;i<payload.size();i++) intrinsic+=payload[i]?16:4;
    if (payload[4]==0) intrinsic+=2*((payload.size()-45+31)/32);
    if (gas<intrinsic||(payload[4]==0&&payload.size()==45)) return false;
    if (user.nonce==UINT64_MAX||nonce<=user.nonce||nonce-user.nonce>(pending?4096:1)) return false;
    return user.balance>=baseFee&&gas<=user.balance-baseFee&&amount<=user.balance-baseFee-gas;
}
struct EvmExecution {
    EvmState state;
    map<array<uint8_t,32>,string> receipts;
    uint64_t gasUsed=0;
    bool active=false;
};
bool ExecuteEvmTx(UserMap& changed,span<const uint8_t> tx,span<const uint8_t> payload,
                  const UserMap& users,EvmExecution& execution,const evmone::state::BlockInfo& block) {
    array<uint8_t,32> key,hash;
    memcpy(key.data(),tx.data(),32); memcpy(hash.data(),tx.data()+80,32);
    auto user=GetUser(users,changed,key);
    if (!CheckEvmUserTx(user,tx,payload)) return false;
    if (!execution.active) { execution.state=CopyEvmState(evmState); execution.active=true; }
    auto origin=GetEvmAddress(key);
    auto& sender=execution.state.get_or_insert(origin);
    if (sender.balance>UINT64_MAX-user.balance) return false;
    sender.balance+=user.balance-baseFee;
    evmone::state::Transaction call{};
    call.sender=origin; call.nonce=sender.nonce; call.gas_limit=ReadU64(payload.data()+5);
    call.max_gas_price=1; call.max_priority_gas_price=0; call.chain_id=evmChainId;
    call.value=ReadU64(tx.data()+64);
    call.data.assign(payload.data()+45,payload.size()-45);
    if (payload[4]==1) { evmc::address target; memcpy(target.bytes,tx.data()+44,20); call.to=target; }
    auto contract=call.to.value_or(evmone::state::compute_create_address(origin,call.nonce));
    thread_local evmc::VM vm{evmc_create_evmone()};
    auto outcome=evmone::state::transition(execution.state,block,call,EVMC_SHANGHAI,vm,maxBlockGas-execution.gasUsed,0);
    if (holds_alternative<error_code>(outcome)) return false;
    const auto& receipt=get<evmone::state::TransactionReceipt>(outcome);
    if (receipt.status<0) throw runtime_error("EVM internal execution error");
    execution.gasUsed+=receipt.gas_used;
    auto& remaining=execution.state.get(origin);
    if (remaining.balance>UINT64_MAX) throw runtime_error("EVM supply overflow");
    user.balance=static_cast<uint64_t>(remaining.balance); remaining.balance=0;
    user.nonce=ReadU64(tx.data()+72); changed[key]=user;
    auto logs=nlohmann::json::array();
    for (const auto& log:receipt.logs) {
        auto topics=nlohmann::json::array();
        for (const auto& topic:log.topics) topics.push_back(EvmHex(topic.bytes));
        logs.push_back({{"address",EvmHex(log.addr.bytes)},{"data",EvmHex({log.data.data(),log.data.size()})},{"topics",topics}});
    }
    execution.receipts[hash]=nlohmann::json({{"status",int(receipt.status)}, {"gas_used",receipt.gas_used},
        {"contract",EvmHex(contract.bytes)}, {"height",block.number}, {"output",EvmHex({receipt.output.data(),receipt.output.size()})},{"logs",logs}}).dump();
    return true;
}
#endif
