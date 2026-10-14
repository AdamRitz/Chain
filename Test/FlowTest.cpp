#include <filesystem>
#include <future>
#include <iostream>
#include "../Network/Server.h"
using namespace std;
void Require(bool ok,const char* label) { if (!ok) throw runtime_error(label); }
ForkEntry Candidate(const Wallet& wallet,const Wallet& receiver,const array<uint8_t,32>& parent,size_t count,uint64_t amount,bool invalid=false) {
    Block block; block.height=2; block.previousHash=parent; vector<array<uint8_t,32>> hashes;
    for (size_t i=1;i<=count;i++) {
        block.txs.push_back(GenerateTx(wallet.public_key,receiver.public_key,amount,invalid&&i==count?99:i,wallet));
        hashes.push_back(GetTransactionHash(block.txs.back()));
    }
    block.merkleRoot=MerkleCompute(hashes); auto data=SerializeBlockALL(block);
    Require(VerifyBlock(data),"candidate signatures"); return {std::move(block),std::move(data)};
}
int main(int argc,char** argv) {
    try {
        if (sodium_init()<0) return 1;
        auto root=filesystem::path(argc>1?argv[1]:"flow-test-data")/to_string(GetSteadyTime());
        filesystem::create_directories(root); auto wallet=GenerateWallet(),receiver=GenerateWallet();
        SetGenesisUsers({{"base_fee",1},{"accounts",nlohmann::json::array({{{"public_key",U32ToHex(wallet.public_key)},{"balance",10000}}})}});
        InitDB(root.string()); GenerateGenesisBlock(); auto genesis=DBReadCurrentBlock();
        auto first=GenerateTx(wallet.public_key,receiver.public_key,1,1,wallet);
        unique_lock stateLock(dbCommitMutex);
        auto task=async(launch::async,[&] { return ProcessTxPackage(first); });
        bool concurrent=task.wait_for(std::chrono::seconds(2))==future_status::ready;
        stateLock.unlock(); auto added=task.get();
        Require(concurrent&&added==1,"published admission accounts remain readable during commit lock");
        txpool.clear(); txUserPool.clear(); readyPoolTx=0; maxPoolTx=10;
        for (uint64_t nonce=2;nonce<=10;nonce++) Require(ProcessTxPackage(GenerateTx(wallet.public_key,receiver.public_key,1,nonce,wallet))==1,"future capacity");
        vector<uint8_t> results;
        auto distant=GenerateTx(wallet.public_key,receiver.public_key,1,11,wallet);
        Require(ProcessTxPackage(distant,&results)==0&&results==vector<uint8_t>{TxBusy},"future transactions preserve gap capacity");
        Require(ProcessTxPackage(first,&results)==1&&results==vector<uint8_t>{TxAccepted}&&readyPoolTx==10,"missing nonce uses reserved capacity");
        Require(ProcessTxPackage(first,&results)==0&&results==vector<uint8_t>{TxKnown},"duplicate admission status");
        auto bytes=GenerateBlock(); auto block=UnSerializeBlock(bytes);
        Require(ProcessBlock(bytes)&&CommitBlock(block,bytes),"gap-filled block commits");
        string position; auto hash=GetTransactionHash(first); array<uint8_t,176> loaded{};
        Require(DBGet(DBHashKey("tx/",hash),position)&&position.size()==12&&ReadU64(reinterpret_cast<const uint8_t*>(position.data()))==2&&ReadU32(reinterpret_cast<const uint8_t*>(position.data()+8))==0,"height transaction position");
        string extra; Require(!DBGet(DBHashKey("txheight/",hash),extra),"inclusion queries reuse the transaction position");
        Require(DBReadTx(hash,loaded)&&loaded==first,"indexed transaction lookup");
        Require(poolUsers==users&&poolUsers.at(wallet.public_key).nonce==10,"published accounts follow commit");
        evmc::address unrelated{}; unrelated.bytes[19]=70;
        auto& contract=evmState.insert(unrelated); evmc::bytes32 slot{},word{}; word.bytes[31]=5;
        for (uint64_t i=0;i<10000;i++) { WriteU64(slot.bytes,i); contract.storage[slot]={word,word,EVMC_ACCESS_COLD}; }
        contract.nonce=7; contract.code={0x60,0x01};
        BranchMemory removed; rocksdb::WriteBatch deletes;
        deletes.Delete(DBEvmKey("evm/",unrelated)); deletes.Delete(DBEvmKey("evm-code/",unrelated));
        for (const auto& [key,value]:contract.storage) deletes.Delete(DBEvmSlotKey(unrelated,key));
        branchMemory=&removed; SaveBranchMemory(deletes); branchMemory=nullptr;
        evmState.get_accounts().erase(unrelated); removed.Restore();
        Require(evmState.get(unrelated).nonce==7&&evmState.get(unrelated).code==evmc::bytes{0x60,0x01}&&
                evmState.get(unrelated).storage.size()==10000&&evmState.ReadStorage(unrelated,slot)==word,"deleted account code and storage restore together");
        evmc::address created{}; created.bytes[19]=71; BranchMemory fresh; rocksdb::WriteBatch creates;
        creates.Put(DBEvmKey("evm/",created),"new"); creates.Put(DBEvmSlotKey(created,slot),"new");
        branchMemory=&fresh; SaveBranchMemory(creates); branchMemory=nullptr;
        evmState.insert(created).storage[slot]={word,word,EVMC_ACCESS_COLD}; fresh.Restore();
        Require(!evmState.get_accounts().count(created),"new account and slots removed on failed branch");
        auto before=users; auto burned=userBurned; auto backed=forkBackupKeys.load();
        auto invalid=Candidate(wallet,receiver,genesis,13,3,true); vector<vector<uint8_t>> detached;
        Require(AdoptFork(invalid,detached)==-1,"invalid branch rejected");
        Require(users==before&&poolUsers==before&&userBurned==burned&&detached.empty(),"changed-key rollback preserves published state");
        Require(evmState.get(unrelated).storage.size()==10000&&evmState.ReadStorage(unrelated,slot)==word,"unrelated large contract state preserved");
        Require(forkBackupKeys.load()-backed==2,"branch backups contain affected native accounts only");
        auto better=Candidate(wallet,receiver,genesis,11,2);
        Require(QueueForkBlock(std::move(better.first),std::move(better.second))&&ProcessForks(),"better branch converges");
        Require(!DBReadTx(hash,loaded)&&poolUsers==users,"detached index removed and accounts published");
        auto selected=GenerateTx(wallet.public_key,receiver.public_key,2,11,wallet);
        Require(DBReadTx(GetTransactionHash(selected),loaded)&&loaded==selected,"new branch transaction lookup");
        db.reset(); InitDB(root.string()); GenerateGenesisBlock();
        Require(DBReadTx(GetTransactionHash(selected),loaded)&&loaded==selected&&poolUsers==users,"compact indexes survive restart");
        maxPendingVerify=1;
        Require(ReserveVerify(1)&&!ReserveVerify(1)&&ReserveVerify(6000,true),"block validation retains its own capacity");
        ReleaseVerify(1); ReleaseVerify(6000,true);
        Require(pendingVerify==0&&pendingTxVerify==0&&pendingBlockVerify==0,"both validation lanes release reservations");
        maxPendingVerify=32768; maxPoolTx=200000;
        db.reset(); SetGenesisUsers({{"base_fee",0},{"accounts",nlohmann::json::array({
            {{"public_key",U32ToHex(wallet.public_key)},{"balance",10000}},
            {{"public_key",U32ToHex(receiver.public_key)},{"balance",10000}}})}});
        InitDB((root/"deterministic").string()); GenerateGenesisBlock();
        txpool.clear(); txUserPool.clear(); readyPoolTx=0; maxBlockTx=5;
        vector<array<uint8_t,176>> incoming;
        for (uint64_t nonce=1;nonce<=8;nonce++) {
            incoming.push_back(GenerateTx(wallet.public_key,receiver.public_key,1,nonce,wallet));
            incoming.push_back(GenerateTx(receiver.public_key,wallet.public_key,1,nonce,receiver));
        }
        for (const auto& tx:incoming) ProcessTx(tx);
        auto ordered=GenerateBlock(); auto chosen=UnSerializeBlock(ordered);
        size_t left=0,right=0;
        for (const auto& tx:chosen.txs) if (memcmp(tx.data(),wallet.public_key.data(),32)==0) left++; else right++;
        Require(chosen.txNum==5&&min(left,right)==2,"round-robin packing advances both accounts");
        txpool.clear(); txUserPool.clear(); readyPoolTx=0; txUserPool.rehash(193);
        for (auto it=incoming.rbegin();it!=incoming.rend();++it) ProcessTx(*it);
        Require(GenerateBlock()==ordered,"same parent and ready transactions produce identical blocks after reversed arrivals");
        maxBlockTx=10000; Require(UnSerializeBlock(GenerateBlock()).txNum==16,"larger block setting retains all ready transactions");
        auto local=UnSerializeBlock(GenerateBlock()); auto localBytes=SerializeBlockALL(local);
        Require(ProcessBlock(localBytes),"cache the first execution before contiguous append");
        Block successor; successor.height=3; successor.previousHash=local.hash;
        successor.txs.push_back(GenerateTx(wallet.public_key,receiver.public_key,1,9,wallet));
        successor.merkleRoot=MerkleCompute(vector<array<uint8_t,32>>{GetTransactionHash(successor.txs[0])});
        auto successorBytes=SerializeBlockALL(successor); auto writes=dbWriteCount.load(),hits=executionCacheHits.load();
        Require(QueueForkBlock(local,localBytes)&&QueueForkBlock(successor,successorBytes)&&ProcessForks(),"prepared contiguous blocks commit together");
        Require(dbWriteCount-writes==1&&executionCacheHits-hits==1&&groupedCommitBlocks>=2,"grouped append reuses the first execution and writes once");
        Require(DBReadBlockHeight()==3&&GetUser(users,wallet.public_key).nonce==9,"grouped state published after persistence");
        auto losing=Candidate(wallet,receiver,local.previousHash,1,1);
        QueueForkBlock(losing.first,losing.second); ProcessForks(); auto attempts=forkAttempts.load();
        forkReady=true; ProcessForks();
        Require(forkAttempts==attempts&&forkRankSkips>0,"lower immutable branch rank skips repeated ancestor walks");
        Block absent; absent.height=4; absent.previousHash.fill(77); absent.txs={incoming[0]};
        absent.merkleRoot=MerkleCompute(vector<array<uint8_t,32>>{GetTransactionHash(incoming[0])});
        auto absentBytes=SerializeBlockALL(absent); QueueForkBlock(absent,absentBytes); ProcessForks(); attempts=forkAttempts.load();
        forkReady=true; ProcessForks();
        Require(forkAttempts==attempts&&forkParentSkips>0,"missing ancestors retry after parent or head changes");
        maxBlockTx=6000;
        db.reset(); cout<<"Admission, compact indexes and changed-state fork checks passed\n"; return 0;
    } catch (const exception& e) { cerr<<e.what()<<'\n'; return 1; }
}
