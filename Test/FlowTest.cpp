#include <filesystem>
#include <future>
#include <iostream>
#include "../Network/Server.h"
using namespace std;
void Require(bool ok,const char* label) { if (!ok) throw runtime_error(label); }
void TestVerificationBudget() {
    VerificationBudget budget; budget.limit=1;
    auto held=make_unique<VerificationBudget::Lease>(budget,false);
    vector<int> order; vector<future<void>> jobs;
    jobs.push_back(async(launch::async,[&] { VerificationBudget::Lease permit(budget,false); order.push_back(0); }));
    for (int i=0;i<5;++i) jobs.push_back(async(launch::async,[&] { VerificationBudget::Lease permit(budget,true); order.push_back(1); }));
    auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3); bool queued=false;
    while (std::chrono::steady_clock::now()<deadline) {
        auto stats=budget.Snapshot();
        if (stats.normalWaiting==1&&stats.priorityWaiting==5) { queued=true; break; }
        this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    held.reset(); for (auto& job:jobs) job.get();
    Require(queued&&order.size()==6&&order.front()==1&&find(order.begin(),order.end(),0)-order.begin()<=4,"priority crypto grants preserve bounded ordinary progress");
    Require(budget.Snapshot().peak==1&&budget.Snapshot().active==0,"all signature lanes share one configured budget");
    try { VerificationBudget::Lease permit(budget,true); throw runtime_error("test"); } catch (...) {}
    Require(budget.Snapshot().active==0,"exception releases crypto capacity");
}
void TestMetadataLane() {
    io_context io; auto peer=make_shared<Peer>(tcp::socket(io));
    workScheduler=true; candidateThreads=make_unique<thread_pool>(1); metadataThreads=make_unique<thread_pool>(1);
    promise<void> entered,release; auto unblock=release.get_future().share();
    post(*candidateThreads,[&] { entered.set_value(); unblock.wait(); }); entered.get_future().wait();
    pendingBlockBytes=16*1024*1024; auto before=metadataTasks.load();
    exception_ptr error;
    co_spawn(peer->strand,ProcessMessage(peer,39,vector<uint8_t>(112)),[&](exception_ptr value) { error=value; });
    io.run();
    auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
    while (metadataTasks==before&&std::chrono::steady_clock::now()<deadline) this_thread::sleep_for(std::chrono::milliseconds(1));
    bool progressed=metadataTasks>before;
    release.set_value(); metadataThreads->join(); candidateThreads->join(); metadataThreads.reset(); candidateThreads.reset();
    pendingBlockBytes=0; workScheduler=false;
    if (error) rethrow_exception(error);
    Require(progressed&&pendingMetadata==0&&pendingMetadataBytes==0&&pendingVerify==0,"metadata executes despite a blocked full-block worker and full block-byte budget");
}
void TestPoolSnapshots(const filesystem::path& root) {
    vector<Wallet> wallets; nlohmann::json config={{"base_fee",1},{"accounts",nlohmann::json::array()}};
    for (size_t i=0;i<130;++i) {
        wallets.push_back(GenerateWallet()); config["accounts"].push_back({{"public_key",U32ToHex(wallets.back().public_key)},{"balance",10000}});
    }
    db.reset(); SetGenesisUsers(config); InitDB((root/"snapshots").string()); GenerateGenesisBlock();
    txpool.clear(); txUserPool.clear(); evmPool.clear(); evmPoolBytes=0; readyPoolTx=0;
    for (auto& wallet:wallets) for (uint64_t nonce=1;nonce<=3;++nonce)
        Require(ProcessTxPackage(GenerateTx(wallet.public_key,wallets[0].public_key,1,nonce,wallet))==1,"snapshot test admission");
    for (auto maximum:{size_t(1),size_t(64),size_t(137),size_t(390),size_t(6000)}) {
        maxBlockTx=maximum; poolSnapshot=false; auto baseline=GenerateBlock();
        poolSnapshot=true; auto snapshot=GenerateBlock();
        Require(snapshot==baseline&&VerifyBlock(snapshot),"chunked snapshots preserve quiescent multi-account order and block limits");
    }
    auto snapshot=GenerateBlock(); Require(ProcessBlock(snapshot)&&CommitBlock(UnSerializeBlock(snapshot),snapshot),"snapshot block commits with normal account validation");
    Require(txpool.empty()&&readyPoolTx==0&&poolSnapshotChunks>6,"snapshot chunks maintain nonce indexes and clear committed transactions");
    poolSnapshot=false;
}
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
        TestVerificationBudget(); TestMetadataLane();
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
        // 高度较高的分支有 3 笔，较短分支有 4 笔；最高的 5 笔候选序号无效。
        db.reset(); InitDB((root/"ranked").string()); GenerateGenesisBlock(); genesis=DBReadCurrentBlock();
        forkBlocks.clear(); forkChecks.clear(); forkBytes=0;
        txpool.clear(); txUserPool.clear(); readyPoolTx=0;
        auto lower=Candidate(wallet,receiver,genesis,2,1);
        Block taller; taller.height=3; taller.previousHash=lower.first.hash;
        taller.txs={GenerateTx(wallet.public_key,receiver.public_key,1,3,wallet)};
        taller.merkleRoot=MerkleCompute(vector<array<uint8_t,32>>{GetTransactionHash(taller.txs[0])});
        auto tallerBytes=SerializeBlockALL(taller),preferred=Candidate(wallet,receiver,genesis,4,2).second;
        auto preferredBlock=UnSerializeBlock(preferred); auto bad=Candidate(wallet,receiver,genesis,5,1,true);
        QueueForkBlock(lower.first,lower.second); QueueForkBlock(taller,tallerBytes);
        QueueForkBlock(preferredBlock,preferred); QueueForkBlock(bad.first,bad.second);
        writes=dbWriteCount.load(); auto selections=forkSelections.load(),invalidCount=forkInvalidCandidates.load();
        Require(ProcessForks()&&DBReadCurrentBlock()==preferredBlock.hash,"cumulative rank wins before height and invalid leader falls back");
        Require(dbWriteCount-writes==1&&forkSelections-selections==1&&forkInvalidCandidates-invalidCount==1,"one valid branch writes once per schedule");
        Require(ReadChainHead().hash==preferredBlock.hash&&ReadChainHead().transactions==4,"published chain head follows successful branch persistence");
        auto stale=Candidate(wallet,receiver,genesis,5,3); RankedFork prepared;
        Require(RankFork(make_shared<ForkEntry>(stale),prepared)==1,"prepare another fork rank");
        Block next; next.height=3; next.previousHash=preferredBlock.hash;
        next.txs={GenerateTx(wallet.public_key,receiver.public_key,2,5,wallet)};
        next.merkleRoot=MerkleCompute(vector<array<uint8_t,32>>{GetTransactionHash(next.txs[0])}); auto nextBytes=SerializeBlockALL(next);
        Require(CommitBlock(next,nextBytes),"advance state after rank preparation");
        detached.clear(); Require(AdoptFork(prepared,detached)==0&&DBReadCurrentBlock()==next.hash,"stale rank cannot publish another state");
        auto snapshot=ReadChainHead(); stateLock=unique_lock(dbCommitMutex);
        auto headTask=async(launch::async,[&] { return ReadQueryResponse(19,{}); });
        concurrent=headTask.wait_for(std::chrono::seconds(2))==future_status::ready;
        stateLock.unlock(); auto headReply=headTask.get();
        Require(concurrent&&ReadU64(headReply.data()+5+64)==snapshot.height&&memcmp(headReply.data()+5+32,snapshot.hash.data(),32)==0,"chain head query stays readable while commit is locked");
        vector<uint8_t> request(10); WriteU64(request.data(),2); request[8]=2;
        auto replies=ReadBlockReplies(42,request); vector<vector<uint8_t>> parsed;
        Require(replies.size()==1&&replies[0][0]==43&&ReadBlockBatch(vector<uint8_t>(replies[0].begin()+5,replies[0].end()),parsed)&&
                parsed.size()==2&&parsed[0]==preferred&&parsed[1]==nextBytes,"read one consistent contiguous block reply");
        {
            boost::asio::io_context io; auto peer=make_shared<Peer>(tcp::socket(io));
            queryThreads=make_unique<boost::asio::thread_pool>(1);
            promise<void> progress; auto progressed=progress.get_future();
            promise<exception_ptr> completed; auto finished=completed.get_future();
            vector<uint8_t> heightRequest(8); WriteU64(heightRequest.data(),2);
            stateLock=unique_lock(dbCommitMutex);
            co_spawn(peer->strand,SendBlockReply(peer,6,heightRequest),[&](exception_ptr error) { completed.set_value(error); });
            post(peer->strand,[&] { progress.set_value(); });
            thread runner([&] { io.run(); });
            bool responsive=progressed.wait_for(std::chrono::seconds(2))==future_status::ready;
            bool waiting=finished.wait_for(std::chrono::milliseconds(0))!=future_status::ready;
            stateLock.unlock();
            bool done=finished.wait_for(std::chrono::seconds(5))==future_status::ready;
            io.stop(); runner.join(); queryThreads->join(); queryThreads.reset();
            Require(responsive&&waiting&&done,"blocked block read leaves the network strand responsive");
            if (auto error=finished.get()) rethrow_exception(error);
        }
        Require(pendingBlockReplies==0,"block reply completion releases its budget");
        // 同一块头的改签名内容与正确内容分别验证，完成后释放全部在途记录。
        bool duplicate=false; auto claim=ReserveBlockWork(nextBytes,false,duplicate);
        auto second=ReserveBlockWork(nextBytes,false,duplicate);
        Require(claim&&!second&&duplicate,"identical complete blocks share in-flight work");
        auto changed=nextBytes; changed[112+112]^=1; duplicate=false;
        auto altered=ReserveBlockWork(changed,false,duplicate);
        Require(altered&&!duplicate&&!VerifyBlock(changed),"changed signature shares no trusted block work");
        altered.reset(); claim.reset(); duplicate=false;
        claim=ReserveBlockWork(nextBytes,false,duplicate);
        Require(claim&&!duplicate&&VerifyBlock(nextBytes),"correct content accepted after failed content");
        claim.reset(); Require(blockWork.empty()&&blockWorkBytes==0,"in-flight block records release on completion");
        Require(ReserveVerify(maxProtocolTx*4,true),"fill the full-block budget");
        for (size_t i=0;i<64;i++) Require(ReserveVerify(1,true,true),"metadata keeps independent bounded capacity");
        Require(!ReserveVerify(1,true,true),"metadata count is bounded");
        for (size_t i=0;i<64;i++) ReleaseVerify(1,true,true);
        ReleaseVerify(maxProtocolTx*4,true);
        Require(pendingVerify==0&&pendingMetadata==0,"metadata and block budgets release together");
        auto untouched=GenerateWallet(); users[untouched.public_key]={10000,0}; PublishPoolUsers();
        for (uint64_t nonce=1;nonce<=6;nonce++) {
            if (nonce>5) ProcessTx(GenerateTx(wallet.public_key,receiver.public_key,1,nonce,wallet));
            ProcessTx(GenerateTx(untouched.public_key,receiver.public_key,1,nonce,untouched));
        }
        auto* retained=&txUserPool.at(untouched.public_key);
        users[wallet.public_key].nonce=2; PublishPoolUsers();
        array<array<uint8_t,32>,1> changedUsers{wallet.public_key}; RefreshTxPool(changedUsers);
        Require(&txUserPool.at(untouched.public_key)==retained&&txUserPool.at(wallet.public_key).readyNonce==2,"partial refresh preserves unrelated users and recalculates rollback gaps");
        auto partialPool=txpool; auto partialReady=readyPoolTx; RefreshTxPool();
        Require(txpool==partialPool&&readyPoolTx==partialReady,"partial nonce rollback matches full refresh");
        maxBlockTx=6000;
        TestPoolSnapshots(root);
        db.reset(); cout<<"Admission, compact indexes and changed-state fork checks passed\n"; return 0;
    } catch (const exception& e) { cerr<<e.what()<<'\n'; return 1; }
}
