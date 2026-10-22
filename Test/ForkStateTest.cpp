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
        experimentIndex=true;
        InitDB(root.string()); GenerateGenesisBlock(); auto genesis=DBReadCurrentBlock();
        auto left=Deploy(owner,"6001600c60003960016000f300");
        auto right=Deploy(owner,"6002600c60003960026000f35f00");
        auto first=Candidate(genesis,left);
        auto transfer=GenerateTx(other.public_key,third.public_key,10,1,other);
        auto better=Candidate(genesis,right,{transfer});
        Require(CommitBlock(first.first,first.second),"initial deployment");
        auto pendingOther=GenerateTx(other.public_key,third.public_key,12,2,other);
        auto pendingThird=GenerateTx(third.public_key,other.public_key,13,1,third);
        auto expensive=GenerateTx(other.public_key,third.public_key,999999,3,other);
        Require(ProcessTxPackage(transfer)==1&&ProcessTxPackage(pendingOther)==1&&ProcessTxPackage(pendingThird)==1&&ProcessTxPackage(expensive)==1,"pool before branch switch");
        auto future=Deploy(owner,"6001600c60003960016000f300");
        WriteU64(future.first.data()+72,2);
        auto futureHash=EvmTransactionHash(future.first,future.second); memcpy(future.first.data()+80,futureHash.data(),32);
        crypto_sign_detached(future.first.data()+112,nullptr,futureHash.data(),32,owner.private_key.data());
        vector<uint8_t> futureData(future.first.begin(),future.first.end()); futureData.insert(futureData.end(),future.second.begin(),future.second.end());
        Require(ProcessEvmTx(futureData),"pending contract accepted");
        auto tampered=future.second; tampered.back()^=1;
        Require(!VerifyTransaction(future.first,tampered),"signature cache binds complete contract input");
        auto savedRoot=genesisRoot; genesisRoot[0]^=1;
        Require(!VerifyTransaction(future.first,future.second),"cached contract signature still checks genesis"); genesisRoot=savedRoot;
        auto address=evmone::state::compute_create_address(GetEvmAddress(owner.public_key),0);
        auto before=SerializeEvmAccount(evmState.get(address)); auto beforeUsers=users; auto beforeBurned=userBurned;
        auto jump=GenerateTx(other.public_key,third.public_key,1,9,other);
        auto invalid=Candidate(genesis,right,{jump}); vector<vector<uint8_t>> detached;
        Require(AdoptFork(invalid,detached)==-1,"invalid fork rejected");
        Require(DBReadCurrentBlock()==first.first.hash&&users==beforeUsers&&userBurned==beforeBurned&&SerializeEvmAccount(evmState.get(address))==before,"invalid fork is atomic");
        db.reset();
        rocksdb::DB* readOnly=nullptr;
        CheckDBStatus(rocksdb::DB::OpenForReadOnly(options,root.string(),&readOnly));
        db.reset(readOnly);
        bool failed=false; try { AdoptFork(better,detached); } catch (const exception&) { failed=true; }
        Require(failed,"write failure surfaced");
        Require(ReadPublishedChain().head.hash==first.first.hash&&ReadPublishedChain().tail.front().hash==first.first.hash,"failed branch write cannot publish an uncommitted ancestry snapshot");
        Require(poolUsers==beforeUsers,"failed branch write preserves published admission accounts");
        Require(DBReadCurrentBlock()==first.first.hash&&users==beforeUsers&&userBurned==beforeBurned&&SerializeEvmAccount(evmState.get(address))==before,"failed switch restores native and EVM caches");
        db.reset(); InitDB(root.string());
        Require(AdoptFork(better,detached)==1,"better branch adopted");
        Require(ReadPublishedChain().tail.size()==2&&ReadPublishedChain().tail[0].hash==better.first.hash&&ReadPublishedChain().tail[1].hash==genesis,"published ancestry replaces detached branch atomically");
        auto checksBefore=signatureChecks.load();
        RefreshTxPool(lastBranchUsers);
        auto refreshed=txpool; auto refreshedInputs=evmPool; auto refreshedReady=readyPoolTx;
        RefreshTxPool();
        Require(txpool==refreshed&&evmPool==refreshedInputs&&readyPoolTx==refreshedReady,"affected-account refresh matches full native and EVM refresh");
        Require(signatureChecks==checksBefore,"pool refresh preserves signature verification");
        Require(txpool.size()==3&&readyPoolTx==3,"pool refresh restores executable native and contract prefixes");
        Require(!txpool.count(GetTransactionHash(transfer))&&!txpool.count(GetTransactionHash(expensive)),"pool refresh removes confirmed and unaffordable transactions");
        Require(evmPool.size()==1&&evmPoolBytes==future.second.size()&&evmPool.at(futureHash)==future.second,"pool refresh preserves contract payload accounting");
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
        db.reset();
        // 70 个满块超过旧的 64 MiB 缓存，验证长分支的父块保留及整体回放。
        SetGenesisUsers({{"base_fee",0},{"accounts",nlohmann::json::array({{{"public_key",U32ToHex(owner.public_key)},{"balance",2000000}}})}});
        InitDB((root/"large-fork").string()); GenerateGenesisBlock(); auto parent=DBReadCurrentBlock();
        Block old;old.height=2;old.previousHash=parent;old.txs={GenerateTx(owner.public_key,third.public_key,1,1,owner)};
        old.merkleRoot=MerkleCompute(vector<array<uint8_t,32>>{GetTransactionHash(old.txs[0])});auto oldData=SerializeBlockALL(old);
        Require(CommitBlock(old,oldData),"large-fork initial state");
        blockVerifyNum=4;blockVerifyThreads=make_unique<boost::asio::thread_pool>(4);
        array<uint8_t,32> target{};
        for (uint64_t i=0;i<70;i++) {
            Block incoming;incoming.previousHash=parent;incoming.height=i+2;vector<array<uint8_t,32>> hashes;
            for (uint64_t j=1;j<=6000;j++) {
                incoming.txs.push_back(GenerateTx(owner.public_key,third.public_key,2,i*6000+j,owner));
                hashes.push_back(GetTransactionHash(incoming.txs.back()));
            }
            incoming.merkleRoot=MerkleCompute(hashes);auto bytes=SerializeBlockALL(incoming);Require(VerifyBlock(bytes),"large branch signatures");
            parent=incoming.hash;target=parent;Require(QueueForkBlock(std::move(incoming),std::move(bytes),false),"retain large branch parent");
        }
        blockVerifyThreads->join();blockVerifyThreads.reset();
        Require(forkBytes>64*1024*1024&&forkBlocks.size()==70,"complete branch exceeds former byte budget");
        detached.clear();Require(AdoptFork(*forkBlocks.at(target),detached)==1,"large branch adopts atomically");
        Require(DBReadCurrentBlock()==target&&DBReadNumber("ChainTx")==420000&&GetUser(users,owner.public_key).nonce==420000,
                "large branch head and ordered execution");
        Require(GetUser(users,owner.public_key).balance==1160000&&GetUser(users,third.public_key).balance==840000,"large branch balances");
        auto published=ReadPublishedChain();
        Require(published.tail.size()==71&&published.tail[0].transactions==420000,"grouped branch publication contains every new ancestor");
        for (const auto& anchor:published.tail) Require(DBReadBlockHashByHeight(anchor.height)==anchor.hash&&DBReadNumber(DBHashKey("score/",anchor.hash))==anchor.transactions,"published tail matches persisted canonical indexes");
        db.reset();InitDB((root/"large-fork").string());Require(DBReadCurrentBlock()==target,"large branch restart");
        db.reset(); cout<<"Fork state, large-branch and write-failure checks passed\n"; return 0;
    } catch (const exception& e) { cerr<<e.what()<<endl; return 1; }
}
