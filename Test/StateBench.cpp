// 同一笔真实合约调用，在不同规模的父状态上重复独立执行。
#include <iostream>
#include "../Transaction/Block.h"
#include "../Tool/Resource.h"
using namespace std;
int main(int argc,char** argv) {
    try {
        if (sodium_init()<0) return 1;
        size_t accounts=argc>1?stoull(argv[1]):10000,slots=argc>2?stoull(argv[2]):4,iterations=argc>3?stoull(argv[3]):200;
        if (!accounts||accounts>100000||slots>4096||!iterations||iterations>100000) throw invalid_argument("Invalid benchmark size");
        auto wallet=GenerateWallet();
        SetGenesisUsers({{"base_fee",0},{"accounts",nlohmann::json::array({{{"public_key",U32ToHex(wallet.public_key)},{"balance",1000000000}}})}});
        UserMap native; native[wallet.public_key]={1000000000,0};
        for (uint64_t i=1;i<=accounts;i++) {
            evmc::address address{};WriteU64(address.bytes,i);auto& account=evmState.insert(address);account.nonce=1;
            for (uint64_t j=1;j<=slots;j++) {
                evmc::bytes32 key{},word{};WriteU64(key.bytes,j);WriteU64(word.bytes,j);account.storage[key]={word,word,EVMC_ACCESS_COLD};
            }
        }
        evmc::address target{};WriteU64(target.bytes,1);
        auto code=EvmBytes("60005460010160005560005460005260206000f3");evmState.get(target).code.assign(code.data(),code.size());
        vector<uint8_t> payload(45);memcpy(payload.data(),"EVM1",4);payload[4]=1;WriteU64(payload.data()+5,100000);memcpy(payload.data()+13,genesisRoot.data(),32);
        array<uint8_t,176> tx{};memcpy(tx.data(),wallet.public_key.data(),32);memcpy(tx.data()+44,target.bytes,20);WriteU64(tx.data()+72,1);
        auto hash=EvmTransactionHash(tx,payload);memcpy(tx.data()+80,hash.data(),32);crypto_sign_detached(tx.data()+112,nullptr,hash.data(),32,wallet.private_key.data());
        evmone::state::BlockInfo context{};context.number=2;context.timestamp=2;context.base_fee=1;context.gas_limit=maxBlockGas;
        auto cpu=GetProcessResources().value("process_cpu_seconds",0.0);auto start=GetSteadyTime();uint64_t gas=0;
        for (size_t i=0;i<iterations;i++) {
            UserMap changed;EvmExecution execution;
            if (!ExecuteEvmTx(changed,tx,payload,native,execution,context)) throw runtime_error("Execution rejected");
            auto receipt=nlohmann::json::parse(execution.receipts.at(hash));
            if (receipt.at("status")!=0||receipt.at("output")!=string(63,'0')+"1") throw runtime_error("Incorrect contract result");
            gas+=execution.gasUsed;
        }
        auto seconds=(GetSteadyTime()-start)/1e9;
        auto cpuSeconds=GetProcessResources().value("process_cpu_seconds",0.0)-cpu;
        if (native.at(wallet.public_key).nonce||evmState.get(target).storage.count(evmc::bytes32{})) throw runtime_error("Parent state changed");
        cout<<nlohmann::json({{"accounts",accounts},{"slots_per_account",slots},{"calls",iterations},{"seconds",seconds},
             {"calls_per_second",iterations/seconds},{"cpu_seconds",cpuSeconds},{"gas",gas},{"parent_unchanged",true}}).dump()<<'\n';return 0;
    } catch (const exception& e) {cerr<<e.what()<<'\n';return 1;}
}
