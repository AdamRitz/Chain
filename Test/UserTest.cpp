#include <filesystem>
#include <iostream>
#include "../Network/Server.h"
using namespace std;

int checks=0;
void Require(bool value,const string& name) {
    if (!value) throw runtime_error("FAILED: "+name);
    checks++;
}
template<class F> void RequireThrow(F action,const string& name) {
    bool caught=false;
    try { action(); } catch (const exception&) { caught=true; }
    Require(caught,name);
}
vector<uint8_t> MakeBlock(const vector<array<uint8_t,176>>& txs) {
    Block block;
    block.height=DBReadBlockHeight()+1;
    block.previousHash=DBReadCurrentBlock();
    block.txs=txs;
    vector<array<uint8_t,32>> hashes;
    for (const auto& tx:txs) hashes.push_back(GetTransactionHash(tx));
    block.merkleRoot=MerkleCompute(hashes);
    return SerializeBlockALL(block);
}
void Commit(const vector<uint8_t>& data) {
    Require(ProcessBlock(data),"candidate state accepted");
    Require(CommitBlock(UnSerializeBlock(data),data),"state committed");
}
int main(int argc,char* argv[]) {
    try {
        if (sodium_init()<0) return 1;
        spdlog::set_level(spdlog::level::off);
        auto wallet=GenerateWallet(),receiver=GenerateWallet(),stranger=GenerateWallet();
        auto config=nlohmann::json{{"base_fee",2},{"accounts",nlohmann::json::array({
            {{"public_key",U32ToHex(wallet.public_key)},{"balance",100}},
            {{"public_key",U32ToHex(receiver.public_key)},{"balance",0}}
        })}};
        SetGenesisUsers(config);
        auto expectedRoot=genesisRoot;
        reverse(config["accounts"].begin(),config["accounts"].end());
        SetGenesisUsers(config);
        Require(genesisRoot==expectedRoot,"genesis independent of JSON account order");
        auto broken=config;
        broken["accounts"].push_back(broken["accounts"][0]);
        RequireThrow([&]{SetGenesisUsers(broken);},"duplicate genesis account");
        broken=config; broken["base_fee"]=-1;
        RequireThrow([&]{SetGenesisUsers(broken);},"negative fee");
        broken=config; broken["accounts"][0]["balance"]=UINT64_MAX;
        broken["accounts"][1]["balance"]=1;
        RequireThrow([&]{SetGenesisUsers(broken);},"genesis supply overflow");
        auto root=filesystem::path(argc>1?argv[1]:"user-test-data")/to_string(GetSteadyTime());
        auto path=(root/"chain").string();
        filesystem::create_directories(root);
        InitDB(path,true);
        GenerateGenesisBlock();
        Require(GetUser(users,wallet.public_key)==User{100,0},"funded genesis account");
        Require(GetUser(users,stranger.public_key)==User{},"new account starts at zero");
        Require(!CheckUserTx(User{100,0},0,1),"zero amount rejected");
        Require(!CheckUserTx(User{100,0},99,1),"fee included in required balance");
        Require(!CheckUserTx(User{UINT64_MAX,0},UINT64_MAX,1),"amount plus fee overflow prevented");
        Require(!CheckUserTx(User{100,UINT64_MAX},1,0),"nonce wraparound rejected");
        Require(!CheckUserTx(User{100,0},1,4097,true),"future nonce gap bounded");
        auto first=GenerateTx(wallet.public_key,receiver.public_key,10,1,wallet);
        auto second=GenerateTx(wallet.public_key,receiver.public_key,20,2,wallet);
        auto conflict=GenerateTx(wallet.public_key,stranger.public_key,11,1,wallet);
        Require(ProcessTxPackage(second)==1&&readyPoolTx==0,"future nonce waits");
        Require(GenerateBlock().empty(),"gap cannot produce a block");
        Require(ProcessTxPackage(first)==1&&readyPoolTx==2,"gap filling makes prefix ready");
        Require(ProcessTxPackage(conflict)==0&&conflictTx==1,"pool allows one transaction per sender nonce");
        auto invalidOrder=MakeBlock({second,first});
        Require(VerifyBlock(invalidOrder)&&!ProcessBlock(invalidOrder),"cryptographic validity still requires ordered state execution");
        auto data=GenerateBlock();
        auto parsed=UnSerializeBlock(data);
        Require(parsed.txs.size()==2&&ReadU64(parsed.txs[0].data()+72)==1&&ReadU64(parsed.txs[1].data()+72)==2,"producer orders account nonces");
        Commit(data);
        Require(GetUser(users,wallet.public_key)==User{66,2},"sender balance and nonce updated");
        Require(GetUser(users,receiver.public_key)==User{30,0}&&userBurned==4,"receiver and burned fees updated");
        Require(txpool.empty()&&txUserPool.empty()&&readyPoolTx==0,"both pool indexes cleared");
        Require(ProcessTxPackage(first)==0&&duplicateTx>0,"confirmed replay rejected");
        Require(ProcessTxPackage(conflict)==0&&invalidUserTx>0,"different payment with old nonce rejected");
        auto overspendA=GenerateTx(wallet.public_key,receiver.public_key,40,3,wallet);
        auto overspendB=GenerateTx(wallet.public_key,receiver.public_key,40,4,wallet);
        auto overspend=MakeBlock({overspendA,overspendB});
        auto before=users;
        auto height=DBReadBlockHeight();
        Require(!ProcessBlock(overspend),"cumulative block overspend rejected");
        Require(!CommitBlock(UnSerializeBlock(overspend),overspend),"commit independently rechecks account state");
        Require(users==before&&DBReadBlockHeight()==height&&userBurned==4,"invalid block makes no account changes");
        auto external=GenerateTx(stranger.public_key,wallet.public_key,1,1,stranger);
        Require(ProcessTxPackage(external)==0,"unfunded sender rejected");
        Require(!ProcessBlock(MakeBlock({external})),"unfunded sender rejected in network block path");
        auto self=GenerateTx(wallet.public_key,wallet.public_key,5,3,wallet);
        Commit(MakeBlock({self}));
        Require(GetUser(users,wallet.public_key)==User{64,3}&&userBurned==6,"self transfer burns fee and advances nonce");
        auto replacement=GenerateTx(wallet.public_key,stranger.public_key,4,4,wallet);
        auto selected=GenerateTx(wallet.public_key,receiver.public_key,3,4,wallet);
        Require(ProcessTxPackage(replacement)==1,"local pending candidate");
        Commit(MakeBlock({selected}));
        Require(txpool.empty()&&readyPoolTx==0,"external valid alternative clears local conflicting nonce");
        auto fifth=GenerateTx(wallet.public_key,receiver.public_key,1,5,wallet);
        auto fifthBlock=MakeBlock({fifth});
        before=users;
        auto burned=userBurned;
        db.reset();
        CheckDBStatus(rocksdb::DB::OpenForReadOnly(options,path,&db));
        RequireThrow([&]{CommitBlock(UnSerializeBlock(fifthBlock),fifthBlock);},"read-only database rejects write");
        Require(users==before&&userBurned==burned,"failed write preserves account cache and fees");
        db.reset();
        genesisConfigured=false;
        InitDB(path,true);
        GenerateGenesisBlock();
        Require(users==before&&userBurned==burned,"restart restores committed account state");
        Commit(fifthBlock);
        auto head=DBReadCurrentBlock();
        db.reset();
        auto wrongConfig=GetGenesisConfig();
        wrongConfig["base_fee"]=3;
        SetGenesisUsers(wrongConfig);
        RequireThrow([&]{InitDB(path,true);},"wrong genesis configuration rejected on restart");
        db.reset();
        genesisConfigured=false;
        InitDB(path,true);
        GenerateGenesisBlock();
        Require(DBReadCurrentBlock()==head&&baseFee==2,"stored genesis configuration restored");
        auto spendSix=GenerateTx(wallet.public_key,receiver.public_key,40,6,wallet);
        auto spendSeven=GenerateTx(wallet.public_key,receiver.public_key,40,7,wallet);
        auto spendEight=GenerateTx(wallet.public_key,receiver.public_key,1,8,wallet);
        Require(ProcessTxPackage(spendSix)==1&&ProcessTxPackage(spendSeven)==1&&ProcessTxPackage(spendEight)==1&&readyPoolTx==3,"pool accepts individually affordable consecutive transactions");
        auto affordable=GenerateBlock();
        Require(UnSerializeBlock(affordable).txs.size()==1,"producer selects affordable prefix");
        Require(txpool.size()==1&&txUserPool.at(wallet.public_key).txs.size()==1&&readyPoolTx==1,"overspend suffix removed from both indexes and readiness");
        Commit(affordable);
        Require(GetUser(users,wallet.public_key)==User{14,6}&&txpool.empty()&&txUserPool.empty()&&readyPoolTx==0,"affordable prefix commits and clears pool");
        auto stuck=GenerateTx(wallet.public_key,receiver.public_key,1,8,wallet);
        Require(ProcessTxPackage(stuck)==1&&readyPoolTx==0,"pending nonce gap created");
        nodeRunning=false;
        MainLoop();
        Require(!nodeFailed&&txpool.size()==1,"shutdown exits with unresolved nonce gap");
        // 旧数据库保留原版本标记，由启动检查拒绝静默转换。
        CheckDBStatus(db->Put(rocksdb::WriteOptions(),"SchemaVersion","2"));
        db.reset();
        RequireThrow([&]{InitDB(path,true);},"schema 2 requires a new database");
        string version;
        Require(DBGet("SchemaVersion",version)&&version=="2","old schema marker preserved");
        db.reset();
        cout<<"PASS "<<checks<<" account checks. Database: "<<path<<endl;
        return 0;
    } catch (const exception& e) {
        cerr<<e.what()<<endl;
        return 1;
    }
}
