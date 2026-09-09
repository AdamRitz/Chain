#include <filesystem>
#include <iostream>
#include "../Network/Server.h"
#include "../Crypto/VRF.h"
using namespace std;

int tests=0;
void Require(bool value,const string& name) {
    if (!value) throw runtime_error("FAILED: "+name);
    tests++;
}
template<class F> void RequireThrow(F action,const string& name) {
    bool caught=false;
    try { action(); } catch (const exception&) { caught=true; }
    Require(caught,name);
}
vector<uint8_t> MakeBlock(const vector<array<uint8_t,176>>& txs,uint64_t height=2) {
    Block block;
    block.previousHash=DBReadCurrentBlock();
    block.height=height;
    block.txs=txs;
    vector<array<uint8_t,32>> hashes;
    for (const auto& tx:txs) hashes.push_back(GetTransactionHash(tx));
    block.merkleRoot=MerkleCompute(hashes);
    return SerializeBlockALL(block);
}
int main(int argc,char* argv[]) {
    try {
        if (sodium_init()<0) return 1;
        spdlog::set_level(spdlog::level::off);
        string root=argc>1?argv[1]:"test-data";
        auto path=(filesystem::path(root)/to_string(GetSteadyTime())).string();
        filesystem::create_directories(path);
        auto wallet=GenerateWallet();
        auto receiver=GenerateWallet();
        SetGenesisUsers({{"base_fee",1},{"accounts",nlohmann::json::array({
            {{"public_key",U32ToHex(wallet.public_key)},{"balance",1000000}}
        })}});
        InitDB(path,true);
        Require(DBReadBlockHeight()==0,"new database height");
        Require(DBReadCurrentBlock()==array<uint8_t,32>{},"missing head");
        Require(DBReadBlockByHeight("99").empty(),"missing block");
        GenerateGenesisBlock();
        auto genesis=DBReadCurrentBlock();
        GenerateGenesisBlock();
        Require(DBReadCurrentBlock()==genesis&&DBReadBlockHeight()==1,"idempotent genesis");
        Require(!dbLegacyKeys,"new database uses prefixed keys");
        auto tx=GenerateTx(wallet.public_key,receiver.public_key,100,1,wallet);
        auto tx2=GenerateTx(wallet.public_key,receiver.public_key,200,2,wallet);
        Require(VerifyTransaction(tx),"valid transaction");
        auto value=UnserializeTx(tx);
        Require(value.amount==100&&value.nonce==1&&value.receiver==receiver.public_key,"roundtrip");
        auto high=GenerateTx(wallet.public_key,receiver.public_key,UINT64_MAX,UINT64_MAX,wallet);
        Require(UnserializeTx(high).amount==UINT64_MAX&&VerifyTransaction(high),"uint64 encoding");
        RequireThrow([&]{GenerateTx(receiver.public_key,receiver.public_key,1,1,wallet);},"wallet mismatch");
        for (size_t offset: {size_t(0),size_t(32),size_t(64),size_t(72),size_t(80),size_t(112)}) {
            auto broken=tx;
            broken[offset]^=1;
            Require(!VerifyTransaction(broken),"tamper field "+to_string(offset));
        }
        auto invalid=tx;
        invalid[112]^=1;
        Require(!VerifyBlock(MakeBlock({invalid})),"invalid signature block rejected");
        Require(!VerifyBlock(MakeBlock({tx,tx})),"duplicate tx inside block rejected");
        vector<uint8_t> package(352);
        memcpy(package.data(),tx.data(),176);
        memcpy(package.data()+176,tx2.data(),176);
        Require(ProcessTxPackage(package)==2&&txpool.size()==2,"all package entries processed");
        Require(ProcessTxPackage(package)==0&&txpool.size()==2,"mempool duplicate");
        Require(ProcessTxPackage(span<const uint8_t>(invalid))==0,"invalid package signature");
        Require(ProcessTxPackage(span<const uint8_t>(package).first(351))==0,"partial tx package rejected");
        auto data=GenerateBlock();
        Require(txpool.size()==2,"proposal preserves pending transactions");
        Require(VerifyBlock(data),"generated block valid");
        auto changed=data;
        changed[112+64]^=1;
        Require(!VerifyBlock(changed),"cache cannot hide payload tampering");
        changed=data;
        changed[32]^=1;
        Require(!VerifyBlock(changed),"merkle/header tampering");
        for (size_t length=0;length<112;length++) Require(!VerifyBlock(vector<uint8_t>(length)),"short block");
        changed=data;
        WriteU64(changed.data()+72,UINT64_MAX);
        Require(!VerifyBlock(changed),"overflowing count");
        changed=data;
        changed.push_back(0);
        Require(!VerifyBlock(changed),"trailing bytes");
        auto block=UnSerializeBlock(data);
        Require(ProcessBlock(data),"candidate accepted");
        Require(!ProcessBlock(data),"duplicate candidate rejected");
        auto wrong=block;
        wrong.previousHash[0]^=1;
        auto wrongData=SerializeBlockALL(wrong);
        Require(!CommitBlock(wrong,wrongData)&&txpool.size()==2&&DBReadBlockHeight()==1,"wrong parent preserves pool");
        Require(CommitBlock(block,data),"atomic block commit");
        Require(txpool.empty()&&DBReadBlockHeight()==2,"commit removes pool transactions");
        Require(DBReadBlockByHeight("2")==data,"height resolves full block");
        Require(DBHasTx(GetTransactionHash(tx))&&DBHasTx(GetTransactionHash(tx2)),"transaction index persisted");
        Require(ProcessTxPackage(package)==0&&txpool.empty(),"committed replay rejected");
        Require(!ProcessBlock(data),"old block rejected");
        Require(BlockBuffer[2].second.empty()&&epoch==3,"candidate window advances and clears");
        db.reset();
        InitDB(path,true);
        GenerateGenesisBlock();
        Require(DBReadBlockHeight()==2&&DBReadCurrentBlock()==block.hash,"restart restores head");
        auto tx3=GenerateTx(wallet.public_key,receiver.public_key,1,3,wallet);
        ProcessTx(tx3);
        auto data3=GenerateBlock();
        auto block3=UnSerializeBlock(data3);
        db.reset();
        CheckDBStatus(rocksdb::DB::OpenForReadOnly(options,path,&db));
        RequireThrow([&]{CommitBlock(block3,data3);},"write failure reported");
        Require(DBReadBlockHeight()==2&&txpool.size()==1&&epoch==3,"failed commit preserves metadata and pool");
        db.reset();
        InitDB(path,true);
        Require(CommitBlock(block3,data3),"retry after write failure");
        auto hash=DBReadCurrentBlock();
        CheckDBStatus(db->Put(rocksdb::WriteOptions(),"CurrentBlock","bad"));
        RequireThrow([]{DBReadCurrentBlock();},"invalid stored hash rejected");
        DBWriteCurrentBlock(hash);
        CheckDBStatus(db->Put(rocksdb::WriteOptions(),"BlockMaxHeight","18446744073709551615"));
        Require(DBReadBlockMaxHeight()==UINT64_MAX,"64-bit DB height");
        CheckDBStatus(db->Put(rocksdb::WriteOptions(),"BlockMaxHeight","not-a-number"));
        RequireThrow([]{DBReadBlockMaxHeight();},"invalid DB number rejected");

        auto frame=GenerateNewBlockMessage(data);
        Require(frame.size()==data.size()+5&&ReadU32(frame.data()+1)==data.size(),"block frame payload length");
        auto height=GenerateHeightMessage();
        Require(height.size()==13&&ReadU32(height.data()+1)==8,"height frame");
        auto discover=GenerateDiscoverMessage();
        Require(discover.size()==5&&ReadU32(discover.data()+1)==0,"empty discovery frame");
        auto time=GetTime();
        auto ack=GenerateTxTimeACKMessage(time);
        Require(ack.size()==21&&ReadU32(ack.data()+1)==16&&memcmp(ack.data()+5,time.data(),8)==0,"timestamp bytes");
        this_thread::sleep_for(milliseconds(2));
        Require(ReadU64(GetTime().data())>ReadU64(time.data()),"clock updates");
        for (uint8_t type: {1,2,4,5,6,7,8,9,10,11,12,13,14}) Require(!VerifyMessageSize(type,UINT32_MAX),"oversize frame rejected");
        Require(!VerifyMessageSize(255,0),"unknown type");
        Require(!VerifyMessageSize(1,175)&&!VerifyMessageSize(10,177),"type-specific lengths");
        Require(GetPeerKey({127,0,0,1},8089)!=GetPeerKey({127,0,0,1},8090),"peer key includes port");
        RequireThrow([]{GenerateMessage(1,vector<uint8_t>(maxMessageSize+1));},"outbound bound");
        maxPendingVerify=6000;
        Require(ReserveVerify(6000)&&!ReserveVerify(1)&&pendingVerify==6000,"bounded validation queue");
        pendingVerify-=6000;
        Require(!ReserveVerify(6001)&&pendingVerify==0,"oversized queue reservation");

        InitVRFWallet();
        vector<uint8_t> message{1,2,3,4};
        auto proof=VRFGen(message);
        Require(VRFVerify(proof,message,myVRFWallet.pk),"VRF normal proof");
        auto oldBase=g;
        g={};
        Require(VRFVerify(proof,message,myVRFWallet.pk),"VRF verify independent base initialization");
        g=oldBase;
        Require(!VRFVerify(vector<uint8_t>(127),message,myVRFWallet.pk),"VRF short proof");
        Require(!VRFVerify(proof,message,array<uint8_t,32>{}),"VRF invalid point");
        auto brokenProof=proof;
        brokenProof[0]^=1;
        Require(!VRFVerify(brokenProof,message,myVRFWallet.pk),"VRF altered output");
        brokenProof=proof;
        fill(brokenProof.begin()+64,brokenProof.begin()+96,255);
        Require(!VRFVerify(brokenProof,message,myVRFWallet.pk),"VRF noncanonical scalar");

        // 并发验证与打包同时运行，最后检查每笔交易都恰好落库。
        vector<array<uint8_t,176>> many;
        for (size_t i=0;i<2048;i++) many.push_back(GenerateTx(wallet.public_key,receiver.public_key,1,4+i,wallet));
        auto before=committedTx.load();
        maxBlockTx=512;
        blockInterval=1;
        nodeRunning=true;
        thread producer(MainLoop);
        vector<thread> workers;
        for (size_t t=0;t<8;t++) workers.emplace_back([&,t] {
            for (size_t round=0;round<3;round++) {
                for (size_t i=t*256;i<(t+1)*256;i+=64) ProcessTxPackage(span<const uint8_t>(reinterpret_cast<const uint8_t*>(many.data()+i),64*176));
            }
        });
        for (auto& worker:workers) worker.join();
        nodeRunning=false;
        WakeMainLoop();
        producer.join();
        Require(!nodeFailed&&txpool.empty()&&committedTx-before==many.size(),"concurrent validation and commit no loss");
        for (const auto& item:many) Require(DBHasTx(GetTransactionHash(item)),"concurrent tx persisted");
        Require(GetUser(users,wallet.public_key).nonce==2051,"account nonce survives concurrent processing");
        Require(userBurned==2051,"one fixed fee per committed transaction");
        auto savedUsers=users;
        db.reset();
        genesisConfigured=false;
        InitDB(path,true);
        GenerateGenesisBlock();
        Require(users==savedUsers&&userBurned==2051,"account state restored from database");
        CheckDBStatus(db->Put(rocksdb::WriteOptions(),"SchemaVersion","999"));
        db.reset();
        RequireThrow([&]{InitDB(path,true);},"unknown schema rejected");
        db.reset();
        cout<<"PASS "<<tests<<" assertions. Database: "<<path<<endl;
        return 0;
    } catch (const exception& e) {
        cerr<<e.what()<<endl;
        return 1;
    }
}
