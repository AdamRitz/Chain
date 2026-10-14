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
        Require(DBGet(DBHashKey("tx/",hash),position)&&position.size()==36&&ReadU32(reinterpret_cast<const uint8_t*>(position.data()+32))==0,"compact transaction position");
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
        db.reset(); cout<<"Admission, compact indexes and changed-state fork checks passed\n"; return 0;
    } catch (const exception& e) { cerr<<e.what()<<'\n'; return 1; }
}
