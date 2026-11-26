#include <filesystem>
#include <iostream>
#include "../Network/Server.h"

using namespace std;
namespace consensus=chain::vdf_consensus;
namespace election=chain::vdf_election;

size_t checks=0;
void Require(bool condition,const char* message) {
    if (!condition) throw runtime_error(message);
    ++checks;
}

Wallet SeededWallet(uint8_t value) {
    array<uint8_t,crypto_sign_SEEDBYTES> seed{}; seed.fill(value);
    Wallet wallet;
    if (crypto_sign_seed_keypair(wallet.public_key.data(),wallet.private_key.data(),seed.data()))
        throw runtime_error("Cannot create test wallet");
    return wallet;
}

struct Branch {
    map<uint64_t,consensus::Digest> hashes;
    vector<ForkEntry> blocks;
    explicit Branch(const consensus::Digest& genesis) { hashes.emplace(1,genesis); }
    election::Context Context(uint64_t target) const {
        return consensus::ContextForEpoch(consensus::config,genesisRoot,hashes.at(1),target,
            [&](uint64_t height)->optional<consensus::Digest> {
                const auto found=hashes.find(height);
                if (found==hashes.end()) return nullopt;
                return found->second;
            });
    }
    const ForkEntry& Add(const Wallet& producer,uint64_t slot,vector<election::Candidate> candidates={},
                        optional<election::Candidate> producerProof=nullopt) {
        Block block;
        block.height=hashes.rbegin()->first+1;
        block.previousHash=hashes.rbegin()->second;
        consensus::Extension extension;
        extension.slot=slot; extension.producer=producer.public_key;
        extension.candidates=std::move(candidates); extension.producerProof=std::move(producerProof);
        block.vdf=std::move(extension);
        block.merkleRoot=consensus::MerkleRoot(MerkleCompute(vector<array<uint8_t,32>>{}),*block.vdf);
        SerializeBlockALL(block);
        consensus::Sign(*block.vdf,block.hash,producer.private_key);
        auto bytes=SerializeBlockALL(block);
        Require(VerifyBlock(bytes),"signed branch block passes generic checks");
        hashes.emplace(block.height,block.hash);
        blocks.emplace_back(std::move(block),std::move(bytes));
        return blocks.back();
    }
};

vector<election::Candidate> Candidates(const election::Context& context,const vector<Wallet>& wallets,
                                     size_t first,size_t count) {
    vector<election::Candidate> records;
    for (size_t i=first;i<first+count;++i)
        records.push_back(election::CreateCandidate(context,wallets.at(i).private_key));
    sort(records.begin(),records.end(),consensus::CandidateLess);
    return records;
}

void ClearForks() {
    lock_guard lock(forkMutex);
    forkBlocks.clear(); forkChecks.clear(); forkBytes=0; forkReady=false;
}

void QueueBranch(const Branch& branch) {
    for (size_t i=0;i+1<branch.blocks.size();++i)
        Require(QueueForkBlock(branch.blocks[i].first,branch.blocks[i].second,false),"queue branch ancestor");
}

int main(int argc,char** argv) {
    try {
        Require(sodium_init()>=0,"libsodium initializes");
        spdlog::set_level(spdlog::level::off);
        const auto root=filesystem::path(argc>1?argv[1]:"vdf-state-data")/to_string(GetSteadyTime());
        filesystem::create_directories(root);
        vector<Wallet> wallets;
        for (uint8_t i=1;i<=7;++i) wallets.push_back(SeededWallet(i));
        SetGenesisUsers({{"base_fee",0},{"accounts",nlohmann::json::array()},
            {"vdf",{{"enabled",true},{"epoch_blocks",2},{"iterations",election::kMinIterations},
                    {"slot_ms",50},{"start_time_ms",5000},{"fallback_slots",20},{"future_slots",2},{"producers",5}}}});
        uint64_t nowSlot=500;
        consensus::currentTimeMs=[&] { return nowSlot*consensus::config.slotMs; };
        InitDB(root.string(),true); GenerateGenesisBlock();
        const auto genesis=DBReadCurrentBlock();
        Branch main(genesis);
        const auto bootstrap0=election::CreateCandidate(main.Context(0),wallets[0].private_key);
        const auto bootstrap1=election::CreateCandidate(main.Context(1),wallets[0].private_key);
        Require(DBVdfParentSlot()==99,"genesis parent slot precedes configured start");
        Branch beforeStart=main;
        const auto oldSlot=beforeStart.Add(wallets[0],1,{},bootstrap0);
        Require(!DBCheckBlockUsers(oldSlot.second)&&!DBCommitBlock(oldSlot.first.hash,oldSlot.second),
                "new chain rejects first block backdated before configured start");
        auto append=[&](const ForkEntry& entry) {
            Require(DBCheckBlockUsers(entry.second),"prepare valid VDF block");
            Require(DBCommitBlock(entry.first.hash,entry.second),"commit valid VDF block");
        };
        append(main.Add(wallets[0],100,{},bootstrap0));
        append(main.Add(wallets[0],101,{},bootstrap0));
        Require(DBVdfNextContext(4)==main.Context(2),"G1 candidates use G0 block hashes");
        const auto selected=Candidates(main.Context(2),wallets,0,5);
        append(main.Add(wallets[0],102,selected,bootstrap1));
        Require(DBVdfCandidatesForNextBlock()==selected,"same group inherits rolling candidates");
        append(main.Add(wallets[0],103,selected,bootstrap1));
        Require(DBVdfCandidatesForNextBlock().empty(),"new group starts a separate candidate set");
        Require(DBVdfActiveCandidates(6)==selected,"previous boundary selects current producers");
        Require(DBVdfContext(2)==main.Context(2),"database and branch derive the same context");
        ClearVdfExtensionCache();
        const auto metadataLoads=vdfExtensionLoads.load();
        const auto metadataBytes=vdfExtensionReadBytes.load();
        for (size_t i=0;i<8;++i) {
            Require(DBVdfParentSlot()==103&&DBVdfActiveCandidates(6)==selected,
                    "parent and boundary metadata remain identical on repeated reads");
        }
        Require(vdfExtensionLoads==metadataLoads+1&&
                vdfExtensionReadBytes==metadataBytes+main.blocks.back().second.size(),
                "parent and boundary share one complete block read by hash");

        // A busy transaction pool must not repeatedly inspect a member which
        // is not scheduled. A later slot can make that same member eligible.
        const auto originalConfig=consensus::config;
        consensus::config.producerRotation=true;
        nowSlot=104;
        const auto scheduled=consensus::ScheduledProducer(nowSlot,selected);
        for (const auto& record:selected) if (record.publicKey!=*scheduled) { vdfPublicKey=record.publicKey; break; }
        vdfIdentityReady=true;
        consensus::Extension proposed;
        const auto productionAttempts=vdfPrepareAttempts.load();
        for (size_t i=0;i<8;++i)
            Require(!PrepareVdfBlock(6,DBReadCurrentBlock(),proposed),"off-turn member declines repeated production");
        Require(vdfPrepareAttempts==productionAttempts+1,"off-turn member checks its schedule only once per parent and slot");
        do {++nowSlot;} while (consensus::ScheduledProducer(nowSlot,selected)!=vdfPublicKey);
        Require(PrepareVdfBlock(6,DBReadCurrentBlock(),proposed)&&proposed.slot==nowSlot,
                "member retries and becomes eligible in its scheduled slot");
        consensus::config=originalConfig; nowSlot=500; vdfIdentityReady=false;
        { lock_guard lock(vdfAttemptMutex); vdfDeclinedAttempt.reset(); }

        const auto outsider=election::CreateCandidate(main.Context(2),wallets[6].private_key);
        Branch tooEarly=main;
        const auto early=tooEarly.Add(wallets[6],122,{},outsider);
        Require(!DBCheckBlockUsers(early.second)&&!DBCommitBlock(early.first.hash,early.second),
                "nonmember rejected at parent slot difference nineteen");
        Branch recovered=main;
        const auto fallback=recovered.Add(wallets[6],123,{},outsider);
        Require(DBCheckBlockUsers(fallback.second),"nonmember valid proof accepted at slot difference twenty");
        auto missing=fallback.first; missing.vdf->producerProof.reset();
        missing.merkleRoot=consensus::MerkleRoot(MerkleCompute(vector<array<uint8_t,32>>{}),*missing.vdf);
        SerializeBlockALL(missing); consensus::Sign(*missing.vdf,missing.hash,wallets[6].private_key);
        const auto missingBytes=SerializeBlockALL(missing);
        Require(VerifyBlock(missingBytes)&&!DBCheckBlockUsers(missingBytes),"fallback still requires producer proof");

        const auto current=main.Add(wallets[0],104);
        Require(DBCheckBlockUsers(current.second),"elected producer needs no duplicate current proof");
        const auto prepared=candidateExecutions.load();
        Require(DBCheckBlockUsers(current.second)&&candidateExecutions==prepared,"same valid bytes reuse execution");
        nowSlot=101;
        Require(!DBCheckBlockUsers(current.second)&&!DBCommitBlock(current.first.hash,current.second),
                "future slot is checked before execution cache reuse and commit");
        nowSlot=500;
        const auto cacheHits=executionCacheHits.load();
        Require(DBCommitBlock(current.first.hash,current.second)&&executionCacheHits==cacheHits+1,
                "valid current slot can commit its cached execution");

        // The same-height alternative must be eligible for replacement. Vary
        // only its signed slot until its hash wins the existing tie-break.
        Branch forkParent=main;
        forkParent.hashes.erase(6); forkParent.blocks.pop_back();
        optional<ForkEntry> timedFork;
        for (uint64_t slot=105;slot<=500&&!timedFork;++slot) {
            Branch trial=forkParent;
            const auto& entry=trial.Add(wallets[0],slot);
            if (entry.first.hash<current.first.hash) timedFork=entry;
        }
        Require(timedFork.has_value(),"construct a higher-ranked same-height time retry fork");
        const auto timedHash=timedFork->first.hash;
        const auto timedSlot=timedFork->first.vdf->slot;
        nowSlot=timedSlot-3;
        Require(VerifyBlock(timedFork->second),"saved block verification does not depend on current clock");
        Require(!ProcessBlock(timedFork->second)&&!HasForkBlock(timedHash,timedFork->second)&&
                !KnowsForkBlock(timedHash)&&!forkChecks.contains(timedHash),
                "future-slot ingress does not create a fork cache entry");
        nowSlot=timedSlot;
        Require(ProcessBlock(timedFork->second),"same block is admitted after the clock catches up");
        RankedFork rankedTimeFork;
        Require(RankFork(FindForkEntry(timedHash),rankedTimeFork)==1&&forkChecks.contains(timedHash),
                "valid-time alternative receives its branch rank");
        nowSlot=timedSlot-3;
        Require(!ProcessForks()&&DBReadCurrentBlock()==current.first.hash&&
                !HasForkBlock(timedHash,timedFork->second)&&!KnowsForkBlock(timedHash)&&!forkChecks.contains(timedHash),
                "clock rollback removes the rejected tip and rank without saving an invalid block");
        nowSlot=timedSlot;
        Require(ProcessBlock(timedFork->second)&&ProcessForks()&&DBReadCurrentBlock()==timedHash,
                "same fork is accepted after a temporary time failure");
        nowSlot=500;
        ClearForks();
        const auto originalHead=DBReadCurrentBlock();
        const auto originalContext=DBVdfContext(2);

        // This fork changes the source hashes. A record from the original G0
        // must fail even if its signature and VDF previously entered the cache.
        Branch invalid(genesis);
        invalid.Add(wallets[0],120,{},bootstrap0);
        invalid.Add(wallets[0],121,{},bootstrap0);
        invalid.Add(wallets[0],122,selected,bootstrap1);
        invalid.Add(wallets[0],123,selected,bootstrap1);
        invalid.Add(wallets[0],124);
        invalid.Add(wallets[0],125);
        QueueBranch(invalid);
        vector<vector<uint8_t>> detached;
        Require(AdoptFork(invalid.blocks.back(),detached)==-1,"fork replay rejects old-anchor candidate records");
        Require(detached.empty()&&DBReadCurrentBlock()==originalHead&&DBVdfContext(2)==originalContext&&
                DBVdfActiveCandidates(6)==selected,"invalid VDF fork restores head, context and committee");
        Require(!vdfExtensions.contains(invalid.blocks.front().first.hash),
                "failed branch does not publish temporary block metadata into cache");
        ClearForks();

        Branch alternate(genesis);
        alternate.Add(wallets[0],120,{},bootstrap0);
        alternate.Add(wallets[0],121,{},bootstrap0);
        const auto replacement=Candidates(alternate.Context(2),wallets,2,5);
        Require(alternate.Context(2)!=originalContext,"changed G0 changes the VDF challenge");
        alternate.Add(wallets[0],122,replacement,bootstrap1);
        alternate.Add(wallets[0],123,replacement,bootstrap1);
        alternate.Add(wallets[6],124);
        alternate.Add(wallets[6],125);
        QueueBranch(alternate);
        Require(AdoptFork(alternate.blocks.back(),detached)==1,"valid branch derives its own committee during replay");
        Require(detached.size()==5&&DBReadCurrentBlock()==alternate.blocks.back().first.hash&&
                DBVdfContext(2)==alternate.Context(2)&&DBVdfActiveCandidates(7)==replacement,
                "adopted branch publishes its own context and committee");
        Require(!consensus::VerifyProof(outsider,DBVdfContext(2)),"cached old-branch proof does not authorize new branch");

        Require(DBVdfActiveCandidates(8).empty(),"no candidates leaves the next committee empty");
        Branch noProof=alternate;
        const auto empty=noProof.Add(wallets[0],126);
        Require(!DBCheckBlockUsers(empty.second),"empty committee still requires current proof");
        const auto openProof=election::CreateCandidate(alternate.Context(3),wallets[0].private_key);
        append(alternate.Add(wallets[0],126,{},openProof));
        const auto savedHead=DBReadCurrentBlock();
        const auto savedContext=DBVdfContext(3);
        db.reset(); InitDB(root.string(),true);
        Require(vdfExtensions.empty(),"opening a database clears metadata from the previous database instance");
        GenerateGenesisBlock();
        Require(DBReadCurrentBlock()==savedHead&&DBVdfContext(3)==savedContext&&DBVdfParentSlot()==126,
                "restart restores VDF context and parent slot from stored blocks");
        Require(DBVdfActiveCandidates(7)==replacement&&DBVdfCandidatesForNextBlock().empty(),
                "restart derives committee and current candidates without extra schema");
        db.reset(); ClearForks();
        cout<<"PASS "<<checks<<" VDF state checks. Database: "<<root.string()<<'\n';
        return 0;
    } catch (const exception& error) {
        cerr<<error.what()<<'\n';
        return 1;
    }
}
