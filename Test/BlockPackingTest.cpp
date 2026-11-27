#include <filesystem>
#include <iostream>
#include "../Network/Server.h"
#include "../EVM/BuildTransaction.h"
using namespace std;

size_t checks=0;
void Require(bool value,const string& name) {
    if (!value) throw runtime_error("FAILED: "+name);
    ++checks;
}
void Commit(const vector<uint8_t>& bytes) {
    Require(!bytes.empty()&&VerifyBlock(bytes),"generated block has valid encoding, Merkle root and signatures");
    Require(ProcessBlock(bytes),"generated candidate executes successfully");
    Require(CommitBlock(UnSerializeBlock(bytes),bytes),"generated candidate commits");
}
void AddNative(const Wallet& wallet,const Wallet& receiver,uint64_t first,size_t count,uint64_t amount=1) {
    vector<uint8_t> package;
    package.reserve(count*176);
    for (size_t i=0;i<count;++i) {
        auto tx=GenerateTx(wallet.public_key,receiver.public_key,amount,first+i,wallet);
        package.insert(package.end(),tx.begin(),tx.end());
    }
    Require(ProcessTxPackage(package)==count,"all signed sequential native transactions admitted");
}
vector<uint8_t> MakeCandidate(const Wallet& sender,const Wallet& receiver,uint64_t amount) {
    Block block;
    block.previousHash=DBReadCurrentBlock(); block.height=DBReadBlockHeight()+1;
    block.txs.push_back(GenerateTx(sender.public_key,receiver.public_key,amount,
                                 GetUser(users,sender.public_key).nonce+1,sender));
    block.merkleRoot=MerkleCompute(vector<array<uint8_t,32>>{GetTransactionHash(block.txs[0])});
    return SerializeBlockALL(block);
}

int main(int argc,char** argv) {
    try {
        if (sodium_init()<0) return 1;
        spdlog::set_level(spdlog::level::off);
        auto root=filesystem::path(argc>1?argv[1]:"block-packing-data")/to_string(GetSteadyTime());
        filesystem::create_directories(root);
        auto alice=GenerateWallet(),bob=GenerateWallet(),receiver=GenerateWallet();
        auto poor=GenerateWallet(),gasOwner=GenerateWallet(),bytesOwner=GenerateWallet();
        auto accounts=nlohmann::json::array();
        for (const auto& wallet:{alice,bob,gasOwner,bytesOwner})
            accounts.push_back({{"public_key",U32ToHex(wallet.public_key)},{"balance",1000000000000ULL}});
        accounts.push_back({{"public_key",U32ToHex(poor.public_key)},{"balance",4}});
        SetGenesisUsers({{"base_fee",1},{"accounts",accounts}});
        InitDB(root.string(),true); GenerateGenesisBlock();
        maxBlockTx=6000;
        AddNative(alice,receiver,1,2250);
        AddNative(bob,receiver,1,2250);
        auto partial=GenerateBlock();
        Require(UnSerializeBlock(partial).txNum==4500,"all 4500 ready transactions fit a 6000-transaction block");
        Require(GenerateBlock()==partial,"identical parent and pool produce an identical block");
        Require(txpool.size()==4500&&readyPoolTx==4500,"building preserves the pool until commit");
        Commit(partial);
        Require(txpool.empty()&&txUserPool.empty()&&readyPoolTx==0,"all 4500 transactions leave both indexes after commit");
        Require(GetUser(users,alice.public_key).nonce==2250&&GetUser(users,bob.public_key).nonce==2250,
                "both senders retain continuous nonces");

        maxBlockTx=6001;
        auto firstAlice=GetUser(users,alice.public_key).nonce+1;
        auto firstBob=GetUser(users,bob.public_key).nonce+1;
        AddNative(alice,receiver,firstAlice,3004);
        AddNative(bob,receiver,firstBob,3003);
        auto full=GenerateBlock();
        Require(UnSerializeBlock(full).txNum==6001,"capacity is used exactly when it is not a multiple of 2000");
        Commit(full);
        Require(txpool.size()==6&&readyPoolTx==6,"only transactions beyond capacity remain ready");
        auto remainder=GenerateBlock();
        Require(UnSerializeBlock(remainder).txNum==6,"the next block includes every remaining transaction");
        Commit(remainder);
        Require(GetUser(users,alice.public_key).nonce==firstAlice+3003&&
                GetUser(users,bob.public_key).nonce==firstBob+3002,"capacity boundaries preserve exact sender nonces");
        Require(txpool.empty()&&txUserPool.empty()&&readyPoolTx==0,"both indexes empty after the remainder commits");
        maxBlockTx=maxProtocolTx;

        // Admission checks individual affordability; selection must use its cumulative affordable count.
        AddNative(poor,receiver,1,501,3);
        Require(readyPoolTx==501,"ready count can exceed cumulative affordable count");
        auto affordable=GenerateBlock();
        Require(txpool.size()==1&&readyPoolTx==1,"unaffordable suffix is removed without deleting the affordable tail");
        Require(UnSerializeBlock(affordable).txNum==1,"block includes the complete affordable prefix");
        Commit(affordable);
        Require(GetUser(users,poor.public_key)==User{0,1},"affordable tail preserves exact balance and nonce");

        // Reserving transaction gas can limit the block below its transaction capacity.
        for (uint64_t nonce=1;nonce<=5;++nonce) {
            auto tx=MakeEvmTx(gasOwner,{},vector<uint8_t>{0},nonce,0,maxEvmGas,true);
            Require(ProcessEvmTx(tx),"gas-limited EVM transaction admitted");
        }
        Require(readyPoolTx==5,"all gas-limited transactions are ready");
        auto gasTail=GenerateBlock();
        auto gasBlock=UnSerializeBlock(gasTail);
        Require(gasBlock.txNum==3&&gasBlock.evm.size()==3,"selection observes total gas cap and preserves EVM payload indexes");
        Commit(gasTail);
        Require(readyPoolTx==2,"gas remainder remains ready for the next block");
        Commit(GenerateBlock());
        Require(GetUser(users,gasOwner.public_key).nonce==5,"all gas-limited transactions eventually commit");

        // Large signed inputs reach the encoded byte limit before the gas limit or count limit.
        vector<uint8_t> largeInput(maxEvmInput,0);
        for (uint64_t nonce=1;nonce<=64;++nonce)
            Require(ProcessEvmTx(MakeEvmTx(bytesOwner,{},largeInput,nonce,0,400000,true)),"large EVM input admitted");
        Require(readyPoolTx==64,"all byte-limited transactions are ready");
        auto byteTail=GenerateBlock();
        auto byteBlock=UnSerializeBlock(byteTail);
        Require(byteBlock.txNum>0&&byteBlock.txNum<64&&byteTail.size()<=maxBlockBytes,"selection respects encoded byte budget");
        Require(byteBlock.txNum*400000<maxBlockGas,"large-input scenario is constrained by bytes, not gas");
        Require(byteBlock.evm.size()==byteBlock.txNum,"every selected large input keeps its matching transaction index");
        Commit(byteTail);
        Require(readyPoolTx==64-byteBlock.txNum,"unselected large inputs remain in the pool");
        Commit(GenerateBlock());
        Require(GetUser(users,bytesOwner.public_key).nonce==64&&txpool.empty(),"large-input remainder commits with continuous nonce");

        // Packing changes preserve the protocol's same-count hash tie breaker.
        auto left=MakeCandidate(alice,receiver,2),right=MakeCandidate(alice,receiver,3);
        auto leftHash=UnSerializeBlock(left).hash,rightHash=UnSerializeBlock(right).hash;
        Require(leftHash!=rightHash,"different valid transaction contents yield distinct candidate hashes");
        const auto& larger=leftHash>rightHash?left:right;
        const auto& smaller=leftHash<rightHash?left:right;
        Require(ProcessBlock(larger),"first same-count candidate accepted");
        Require(ProcessBlock(smaller),"smaller hash still replaces same-count candidate");
        Require(BlockBuffer[0].second==smaller&&!ProcessBlock(larger),"same-count best candidate remains independent of arrival order");
        Require(CommitBlock(UnSerializeBlock(smaller),smaller),"hash-selected candidate commits normally");

        db.reset();
        cout<<"PASS "<<checks<<" block packing checks. Database: "<<root.string()<<'\n';
        return 0;
    } catch (const exception& error) {
        cerr<<error.what()<<'\n'; return 1;
    }
}
