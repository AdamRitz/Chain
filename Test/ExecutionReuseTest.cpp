#include <filesystem>
#include <iostream>
#include "../Transaction/Block.h"
using namespace std;

size_t checks=0;
void Require(bool value,const char* label) {
    if (!value) throw runtime_error(label);
    checks++;
}
Wallet TestWallet(uint64_t number) {
    array<uint8_t,crypto_sign_SEEDBYTES> seed{};
    const string domain="Chain execution regression";
    memcpy(seed.data(),domain.data(),min(domain.size(),size_t(24))); WriteU64(seed.data()+24,number);
    Wallet wallet;
    if (crypto_sign_seed_keypair(wallet.public_key.data(),wallet.private_key.data(),seed.data()))
        throw runtime_error("test key generation failed");
    return wallet;
}
vector<uint8_t> Candidate(const vector<array<uint8_t,176>>& txs,const EvmPayloads& evm={}) {
    Block block;
    block.height=DBReadBlockHeight()+1; block.previousHash=DBReadCurrentBlock();
    block.txs=txs; block.evm=evm;
    vector<array<uint8_t,32>> hashes;
    for (const auto& tx:txs) hashes.push_back(GetTransactionHash(tx));
    block.merkleRoot=MerkleCompute(hashes);
    auto data=SerializeBlockALL(block);
    Require(VerifyBlock(data),"test candidate signatures and Merkle root");
    return data;
}
UserMap Prepared(const vector<uint8_t>& data) {
    bool fullyChecked=false;
    Require(DBPrepareBlockUsers(data,nullptr,&fullyChecked)==CandidateCheckResult::Valid&&fullyChecked,
            "candidate execution completes");
    lock_guard lock(executionMutex);
    for (const auto& entry:executionCache) if (entry->data==data) return entry->users;
    throw runtime_error("missing prepared candidate");
}
UserMap Serial(const vector<uint8_t>& data) {
    const auto threads=nativeExecutionThreads; nativeExecutionThreads=1;
    UserMap result;
    { shared_lock lock(dbCommitMutex); Require(DBApplyBlockUsers(data,result),"serial reference execution"); }
    nativeExecutionThreads=threads;
    return result;
}
pair<array<uint8_t,176>,vector<uint8_t>> Deployment(const Wallet& wallet,const string& hex) {
    auto input=EvmBytes(hex); vector<uint8_t> payload(45+input.size());
    memcpy(payload.data(),"EVM1",4); WriteU64(payload.data()+5,100000);
    memcpy(payload.data()+13,genesisRoot.data(),32); memcpy(payload.data()+45,input.data(),input.size());
    array<uint8_t,176> tx{}; memcpy(tx.data(),wallet.public_key.data(),32); WriteU64(tx.data()+72,1);
    const auto hash=EvmTransactionHash(tx,payload); memcpy(tx.data()+80,hash.data(),32);
    crypto_sign_detached(tx.data()+112,nullptr,hash.data(),32,wallet.private_key.data());
    return {tx,payload};
}
nlohmann::json Benchmark(const vector<Wallet>& owners,const vector<Wallet>& receivers) {
    constexpr size_t repeats=20;
    const auto savedReuse=executionPrefixReuse; const auto savedThreads=nativeExecutionThreads;
    vector<array<uint8_t,176>> transactions;
    for (uint64_t nonce=1;transactions.size()<6000;nonce++) for (size_t i=0;i<owners.size()&&transactions.size()<6000;i++)
        transactions.push_back(GenerateTx(owners[i].public_key,receivers[i].public_key,nonce%7+1,nonce,owners[i]));
    vector<vector<uint8_t>> candidates; vector<UserMap> expected;
    for (size_t count:{2000,4000,6000}) {
        candidates.push_back(Candidate(vector<array<uint8_t,176>>(transactions.begin(),transactions.begin()+count)));
        expected.push_back(Serial(candidates.back()));
    }
    auto measure=[&](bool reuse,unsigned threads,bool onlyFull) {
        executionPrefixReuse=reuse; nativeExecutionThreads=threads;
        const auto reusedBefore=executionPrefixTransactions.load(),parallelBefore=nativeParallelBlocks.load();
        const auto executeBefore=candidateExecuteNs.load(); uint64_t elapsed=0;
        for (size_t repeat=0;repeat<repeats;repeat++) {
            { unique_lock lock(dbCommitMutex); InvalidateExecutionCache(); }
            for (size_t i=onlyFull?2:0;i<candidates.size();i++) {
                const auto start=GetSteadyTime();
                const auto result=DBPrepareBlockUsers(candidates[i]); elapsed+=GetSteadyTime()-start;
                Require(result==CandidateCheckResult::Valid,"benchmark candidate accepted");
                lock_guard lock(executionMutex); bool matched=false;
                for (const auto& entry:executionCache) if (entry->data==candidates[i]) {
                    Require(entry->users==expected[i]&&entry->evm.gasUsed==0&&!entry->evm.active,
                            "benchmark execution equals serial reference"); matched=true; break;
                }
                Require(matched,"benchmark candidate remains cached");
            }
        }
        return nlohmann::json{{"total_prepare_ns",elapsed},{"mean_round_ns",elapsed/repeats},
            {"candidate_execution_ns",candidateExecuteNs.load()-executeBefore},
            {"reused_transactions",executionPrefixTransactions.load()-reusedBefore},
            {"parallel_blocks",nativeParallelBlocks.load()-parallelBefore},{"state_valid",true}};
    };
    nlohmann::json report={{"repetitions",repeats},{"candidate_transactions",{2000,4000,6000}},
        {"account_components",owners.size()}};
    report["prefix_disabled"]=measure(false,1,false);
    report["prefix_enabled"]=measure(true,1,false);
    report["native_threads_1"]=measure(false,1,true);
    report["native_threads_2"]=measure(false,2,true);
    executionPrefixReuse=savedReuse; nativeExecutionThreads=savedThreads;
    { unique_lock lock(dbCommitMutex); InvalidateExecutionCache(); }
    return report;
}
int main(int argc,char** argv) {
    try {
        if (sodium_init()<0) return 1;
        spdlog::set_level(spdlog::level::off);
        const auto root=filesystem::path(argc>1?argv[1]:"execution-reuse-data")/to_string(GetSteadyTime());
        filesystem::create_directories(root);
        vector<Wallet> owners,receivers;
        auto accounts=nlohmann::json::array();
        for (size_t i=0;i<8;i++) {
            owners.push_back(TestWallet(i*2+1)); receivers.push_back(TestWallet(i*2+2));
            accounts.push_back({{"public_key",U32ToHex(owners.back().public_key)},{"balance",1000000}});
        }
        auto deployer=TestWallet(100);
        accounts.push_back({{"public_key",U32ToHex(deployer.public_key)},{"balance",1000000}});
        SetGenesisUsers({{"base_fee",1},{"accounts",accounts}});
        InitDB(root.string()); GenerateGenesisBlock();
        const auto initial=users; const auto initialBurned=userBurned;
        vector<array<uint8_t,176>> first;
        for (uint64_t i=1;i<=32;i++) first.push_back(GenerateTx(owners[0].public_key,receivers[0].public_key,1,i,owners[0]));
        auto base=Candidate(first);
        Require(Prepared(base)==Serial(base),"initial candidate matches reference");
        auto extended=first;
        for (uint64_t i=33;i<=48;i++) extended.push_back(GenerateTx(owners[0].public_key,receivers[0].public_key,1,i,owners[0]));
        auto appended=Candidate(extended);
        auto reused=executionPrefixTransactions.load();
        Require(Prepared(appended)==Serial(appended),"appended candidate matches reference");
        Require(executionPrefixTransactions.load()-reused==32,"appended candidate reuses its exact prefix");
        auto diverged=extended;
        for (size_t i=40;i<diverged.size();i++) diverged[i]=GenerateTx(owners[0].public_key,receivers[0].public_key,2,i+1,owners[0]);
        auto divergent=Candidate(diverged); reused=executionPrefixTransactions.load();
        Require(Prepared(divergent)==Serial(divergent),"rewound divergent suffix matches reference");
        Require(executionPrefixTransactions.load()-reused==40,"longer cached candidate rewinds to common prefix");
        auto reordered=extended; swap(reordered[40],reordered[41]);
        Require(DBPrepareBlockUsers(Candidate(reordered))==CandidateCheckResult::Invalid,"reordered nonce suffix rejected");
        auto invalid=first;
        invalid.push_back(GenerateTx(owners[0].public_key,receivers[0].public_key,1,99,owners[0]));
        Require(DBPrepareBlockUsers(Candidate(invalid))==CandidateCheckResult::Invalid,"bad nonce after reused prefix rejected");
        invalid.back()=GenerateTx(owners[0].public_key,receivers[0].public_key,1000000,33,owners[0]);
        Require(DBPrepareBlockUsers(Candidate(invalid))==CandidateCheckResult::Invalid,"overspend after reused prefix rejected");
        Require(users==initial&&poolUsers==initial&&userBurned==initialBurned,"failed candidates preserve published accounts");
        auto changedFirst=extended;
        changedFirst[0]=GenerateTx(owners[0].public_key,receivers[0].public_key,2,1,owners[0]);
        auto different=Candidate(changedFirst); reused=executionPrefixTransactions.load();
        Require(Prepared(different)==Serial(different)&&executionPrefixTransactions==reused,"changed first transaction does not reuse state");
        auto hit=candidateExecutionCacheHits.load();
        Prepared(different);
        Require(candidateExecutionCacheHits==hit+1,"complete byte match still uses complete execution cache");
        {
            unique_lock lock(dbCommitMutex); InvalidateExecutionCache();
        }
        reused=executionPrefixTransactions.load();
        Require(Prepared(appended)==Serial(appended)&&executionPrefixTransactions==reused,"new state version clears prefix executions");
        const auto executions=candidateExecutions.load();
        baseFee=2;
        Require(Prepared(appended)==Serial(appended)&&candidateExecutions==executions+1,
                "changed execution fee invalidates complete cached execution");
        baseFee=1;
        { unique_lock lock(dbCommitMutex); InvalidateExecutionCache(); }
        Prepared(base);
        const auto deployment=Deployment(deployer,"6001600c60003960016000f300");
        auto mixed=first; mixed.push_back(deployment.first);
        const auto evmBlock=Candidate(mixed,{{first.size(),deployment.second}});
        reused=executionPrefixTransactions.load();
        Require(Prepared(evmBlock)==Serial(evmBlock)&&executionPrefixTransactions==reused,"EVM block executes without native prefix reuse");
        {
            lock_guard lock(executionMutex);
            bool checked=false;
            for (const auto& entry:executionCache) if (entry->data==evmBlock) {
                Require(entry->evm.active&&entry->evm.receipts.size()==1&&entry->evm.gasUsed>0&&entry->nativeUndo.empty(),
                        "EVM state receipts and gas remain attached to complete block only"); checked=true;
            }
            Require(checked,"EVM execution cached");
        }
        auto secondDeployment=Deployment(deployer,"6002600c60003960026000f35f00");
        mixed.back()=secondDeployment.first;
        auto changedEvm=Candidate(mixed,{{first.size(),secondDeployment.second}});
        Require(Prepared(changedEvm)==Serial(changedEvm)&&executionPrefixTransactions==reused,"changed EVM input executes independently");
        { unique_lock lock(dbCommitMutex); InvalidateExecutionCache(); }
        vector<array<uint8_t,176>> independent;
        for (uint64_t nonce=1;nonce<=128;nonce++) for (size_t i=0;i<owners.size();i++)
            independent.push_back(GenerateTx(owners[i].public_key,receivers[i].public_key,nonce%7+1,nonce,owners[i]));
        auto parallel=Candidate(independent);
        const auto reference=Serial(parallel); nativeExecutionThreads=4;
        auto parallelCount=nativeParallelBlocks.load();
        Require(Prepared(parallel)==reference&&nativeParallelBlocks==parallelCount+1,"disjoint components equal serial account state");
        for (size_t i=0;i<owners.size();i++) independent.push_back(GenerateTx(owners[i].public_key,receivers[i].public_key,3,129,owners[i]));
        auto parallelAppend=Candidate(independent); reused=executionPrefixTransactions.load();
        Require(Prepared(parallelAppend)==Serial(parallelAppend)&&executionPrefixTransactions.load()-reused==1024,
                "parallel execution produces reversible prefix journal");
        // Rewind across transactions from several independent components.
        for (size_t i=900;i<independent.size();i++) {
            const size_t sender=i%owners.size(); const uint64_t nonce=i/owners.size()+1;
            independent[i]=GenerateTx(owners[sender].public_key,receivers[sender].public_key,11,nonce,owners[sender]);
        }
        auto parallelDivergence=Candidate(independent); reused=executionPrefixTransactions.load();
        Require(Prepared(parallelDivergence)==Serial(parallelDivergence)&&executionPrefixTransactions.load()-reused==900,
                "parallel journal rewinds interleaved components in block order");
        auto failed=independent;
        failed.back()=GenerateTx(owners[7].public_key,receivers[7].public_key,1,999,owners[7]);
        auto failedParallel=Candidate(failed); UserMap untouched;
        { shared_lock lock(dbCommitMutex); Require(!DBApplyBlockUsers(failedParallel,untouched),"parallel invalid component rejected"); }
        Require(untouched.empty()&&users==initial&&poolUsers==initial,"failed parallel execution publishes no partial state");
        // An account shared by transfers puts their complete dependency chain
        // into one component, including receiver-then-sender funding.
        vector<array<uint8_t,176>> connected;
        for (uint64_t nonce=1;nonce<=300;nonce++) {
            connected.push_back(GenerateTx(owners[0].public_key,receivers[0].public_key,2,nonce,owners[0]));
            connected.push_back(GenerateTx(receivers[0].public_key,owners[0].public_key,1,nonce,receivers[0]));
        }
        auto connectedBlock=Candidate(connected); parallelCount=nativeParallelBlocks.load();
        Require(Prepared(connectedBlock)==Serial(connectedBlock)&&nativeParallelBlocks==parallelCount,
                "shared-account transfers retain serial funding and nonce order");
        vector<array<uint8_t,176>> self;
        for (uint64_t nonce=1;nonce<=40;nonce++) self.push_back(GenerateTx(owners[0].public_key,owners[0].public_key,1,nonce,owners[0]));
        auto selfBlock=Candidate(self); Prepared(selfBlock);
        self.back()=GenerateTx(owners[0].public_key,receivers[0].public_key,1,40,owners[0]);
        auto selfChanged=Candidate(self);
        Require(Prepared(selfChanged)==Serial(selfChanged),"self-transfer journal restores account once");
        for (uint64_t amount=2;amount<=12;amount++) {
            self.back()=GenerateTx(owners[0].public_key,receivers[0].public_key,amount,40,owners[0]);
            Prepared(Candidate(self));
        }
        {
            lock_guard lock(executionMutex); size_t bytes=0;
            for (const auto& entry:executionCache) {
                Require(entry->bytes>=entry->data.size()+entry->nativeUndo.capacity()*sizeof(NativeExecutionUndo),
                        "undo journal is included in execution cache budget");
                bytes+=entry->bytes;
            }
            Require(bytes==executionBytes&&bytes<=maxExecutionBytes&&executionCache.size()<=8,
                    "execution cache retains byte and entry limits");
        }
        {
            unique_lock lock(dbCommitMutex);
            const auto old=users[receivers[0].public_key]; users[receivers[0].public_key]={UINT64_MAX,0};
            InvalidateExecutionCache();
            UserMap overflow;
            Require(!DBApplyBlockUsers(base,overflow),"receiver uint64 overflow rejected");
            users[receivers[0].public_key]=old;
            if (!initial.contains(receivers[0].public_key)) users.erase(receivers[0].public_key);
            baseFee=UINT64_MAX;
            Require(!DBApplyBlockUsers(base,overflow),"fee and amount overflow rejected without wrapping");
            baseFee=1; InvalidateExecutionCache();
        }
        nlohmann::json benchmark;
        if (argc>2&&string(argv[2])=="--benchmark") benchmark=Benchmark(owners,receivers);
        auto commit=Candidate(first); const auto expected=Prepared(commit);
        const auto beforeUsers=users; const auto beforeBurned=userBurned;
        db.reset();
#if ROCKSDB_MAJOR >= 11
        unique_ptr<rocksdb::DB> readOnly;
        CheckDBStatus(rocksdb::DB::OpenForReadOnly(options,root.string(),&readOnly)); db=std::move(readOnly);
#else
        rocksdb::DB* readOnly=nullptr;
        CheckDBStatus(rocksdb::DB::OpenForReadOnly(options,root.string(),&readOnly)); db.reset(readOnly);
#endif
        bool failedWrite=false;
        try { DBCommitBlock(UnSerializeBlock(commit).hash,commit); } catch (const exception&) { failedWrite=true; }
        Require(failedWrite&&users==beforeUsers&&poolUsers==beforeUsers&&userBurned==beforeBurned,
                "write failure preserves published state after cached execution");
        db.reset(); InitDB(root.string()); GenerateGenesisBlock();
        Prepared(commit);
        Require(DBCommitBlock(UnSerializeBlock(commit).hash,commit),"prepared execution commits atomically");
        for (const auto& [key,value]:expected) Require(GetUser(users,key)==value,"committed account equals prepared execution");
        Require(executionCache.empty()&&executionBytes==0,"commit clears all old-parent prefix executions");
        Require(DBPrepareBlockUsers(appended)==CandidateCheckResult::Invalid,"previous parent candidate rejected after commit");
        Require(poolUsers==users&&userBurned==32,"account mirror and aggregate fees committed together");
        nlohmann::json report={{"checks",checks},{"prefix_hits",executionPrefixHits.load()},
            {"prefix_transactions",executionPrefixTransactions.load()},{"parallel_blocks",nativeParallelBlocks.load()},
            {"parallel_transactions",nativeParallelTransactions.load()}};
        if (!benchmark.is_null()) report["benchmark"]=std::move(benchmark);
        cout<<report.dump()<<endl;
        db.reset(); return 0;
    } catch (const exception& error) {
        cerr<<error.what()<<endl; db.reset(); return 1;
    }
}
