#include <filesystem>
#include <iostream>
#include "../Transaction/Block.h"
using namespace std;
void Require(bool value,const char* label) { if (!value) throw runtime_error(label); }
pair<array<uint8_t,176>,vector<uint8_t>> Deploy(const Wallet& wallet,const string& hex) {
    auto input=EvmBytes(hex);
    vector<uint8_t> payload(45+input.size()); memcpy(payload.data(),"EVM1",4);
    WriteU64(payload.data()+5,100000); memcpy(payload.data()+13,genesisRoot.data(),32);
    memcpy(payload.data()+45,input.data(),input.size());
    array<uint8_t,176> tx{}; memcpy(tx.data(),wallet.public_key.data(),32); WriteU64(tx.data()+72,1);
    auto hash=EvmTransactionHash(tx,payload); memcpy(tx.data()+80,hash.data(),32);
    crypto_sign_detached(tx.data()+112,nullptr,hash.data(),32,wallet.private_key.data());
    return {tx,payload};
}
ForkEntry Candidate(const array<uint8_t,32>& parent,const pair<array<uint8_t,176>,vector<uint8_t>>& deployment,
                    vector<array<uint8_t,176>> extra={}) {
    Block block; block.height=2; block.previousHash=parent;
    block.txs.push_back(deployment.first); block.evm.emplace(0,deployment.second);
    block.txs.insert(block.txs.end(),extra.begin(),extra.end());
    vector<array<uint8_t,32>> hashes; for (const auto& tx:block.txs) hashes.push_back(GetTransactionHash(tx));
    block.merkleRoot=MerkleCompute(hashes); auto data=SerializeBlockALL(block);
    Require(VerifyBlock(data),"candidate signatures"); return {block,data};
}
int main(int argc,char** argv) {
    try {
        if (sodium_init()<0) return 1;
        auto root=filesystem::path(argc>1?argv[1]:"fork-state-data")/to_string(GetSteadyTime()); filesystem::create_directories(root);
        auto owner=GenerateWallet(),other=GenerateWallet(),third=GenerateWallet();
        SetGenesisUsers({{"base_fee",1},{"accounts",nlohmann::json::array({
            {{"public_key",U32ToHex(owner.public_key)},{"balance",1000000}},
            {{"public_key",U32ToHex(other.public_key)},{"balance",1000000}},
            {{"public_key",U32ToHex(third.public_key)},{"balance",1000000}}})}});
        InitDB(root.string()); GenerateGenesisBlock(); auto genesis=DBReadCurrentBlock();
        auto left=Deploy(owner,"6001600c60003960016000f300");
        auto right=Deploy(owner,"6002600c60003960026000f35f00");
        auto first=Candidate(genesis,left);
        auto transfer=GenerateTx(other.public_key,third.public_key,10,1,other);
        auto better=Candidate(genesis,right,{transfer});
        Require(CommitBlock(first.first,first.second),"initial deployment");
        auto address=evmone::state::compute_create_address(GetEvmAddress(owner.public_key),0);
        auto before=SerializeEvmAccount(evmState.get(address)); auto beforeUsers=users; auto beforeBurned=userBurned;
        auto jump=GenerateTx(other.public_key,third.public_key,1,9,other);
        auto invalid=Candidate(genesis,right,{jump}); vector<vector<uint8_t>> detached;
        Require(AdoptFork(invalid,detached)==-1,"invalid fork rejected");
        Require(DBReadCurrentBlock()==first.first.hash&&users==beforeUsers&&userBurned==beforeBurned&&SerializeEvmAccount(evmState.get(address))==before,"invalid fork is atomic");
        db.reset(); CheckDBStatus(rocksdb::DB::OpenForReadOnly(options,root.string(),&db));
        bool failed=false; try { AdoptFork(better,detached); } catch (const exception&) { failed=true; }
        Require(failed,"write failure surfaced");
        Require(DBReadCurrentBlock()==first.first.hash&&users==beforeUsers&&userBurned==beforeBurned&&SerializeEvmAccount(evmState.get(address))==before,"failed switch restores native and EVM caches");
        db.reset(); InitDB(root.string());
        Require(AdoptFork(better,detached)==1,"better branch adopted");
        Require(EvmHex({evmState.get(address).code.data(),evmState.get(address).code.size()})=="5f00","EVM code follows branch");
        string receipt;
        Require(!DBGet(DBHashKey("receipt/",GetTransactionHash(left.first)),receipt),"detached receipt removed");
        Require(!DBHasTx(GetTransactionHash(left.first)),"detached transaction index removed");
        Require(DBGet(DBHashKey("receipt/",GetTransactionHash(right.first)),receipt),"selected receipt persisted");
        auto supply=uint64_t(0); for (const auto& [key,user]:users) supply+=user.balance;
        for (const auto& [key,account]:evmState.get_accounts()) supply+=static_cast<uint64_t>(account.balance);
        Require(supply+userBurned==genesisSupply,"supply after reorganization");
        db.reset(); InitDB(root.string()); GenerateGenesisBlock();
        Require(DBReadCurrentBlock()==better.first.hash&&EvmHex({evmState.get(address).code.data(),evmState.get(address).code.size()})=="5f00","restart after reorganization");
        db.reset(); cout<<"Fork state and write-failure checks passed\n"; return 0;
    } catch (const exception& e) { cerr<<e.what()<<endl; return 1; }
}
