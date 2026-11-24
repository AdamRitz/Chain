#pragma once

namespace VdfConsensus=chain::vdf_consensus;
namespace VdfElection=chain::vdf_election;

struct VdfCandidateSet {
    VdfElection::Context context;
    vector<VdfElection::Candidate> records;
};
mutex vdfRuntimeMutex;
deque<VdfCandidateSet> vdfCandidateSets;
deque<VdfElection::Candidate> vdfOwnProofs;
VdfElection::SecretKey vdfSecretKey{};
VdfElection::PublicKey vdfPublicKey{};
unique_ptr<thread_pool> vdfVerifyThreads;
thread vdfComputeThread;
atomic<size_t> vdfPendingProofs{0};
atomic<uint64_t> vdfComputed{0},vdfSkippedProofs{0},vdfGeneratedNs{0};
bool vdfIdentityReady=false;

// These helpers only manipulate records which have passed proof verification.
vector<VdfElection::Candidate> MergeVdfCandidates(vector<VdfElection::Candidate> records) {
    sort(records.begin(),records.end(),[](const auto& left,const auto& right) {
        if (VdfConsensus::CandidateLess(left,right)) return true;
        if (VdfConsensus::CandidateLess(right,left)) return false;
        return VdfElection::EncodeCandidate(left)<VdfElection::EncodeCandidate(right);
    });
    vector<VdfElection::Candidate> result;
    for (const auto& record:records) {
        if (any_of(result.begin(),result.end(),[&](const auto& item){return item.publicKey==record.publicKey;})) continue;
        result.push_back(record);
        if (result.size()==5) break;
    }
    return result;
}
vector<VdfElection::Candidate> ReadVdfCandidates(const VdfElection::Context& context) {
    lock_guard lock(vdfRuntimeMutex);
    for (const auto& item:vdfCandidateSets) if (item.context==context) return item.records;
    return {};
}
optional<VdfElection::Candidate> ReadOwnVdfProof(const VdfElection::Context& context) {
    lock_guard lock(vdfRuntimeMutex);
    for (const auto& item:vdfOwnProofs) if (item.context==context) return item;
    return nullopt;
}
bool StoreVdfCandidate(const VdfElection::Candidate& record,bool own=false) {
    lock_guard lock(vdfRuntimeMutex);
    if (own) {
        if (none_of(vdfOwnProofs.begin(),vdfOwnProofs.end(),[&](const auto& item){return item.context==record.context;})) {
            vdfOwnProofs.push_back(record);
            if (vdfOwnProofs.size()>8) vdfOwnProofs.pop_front();
        }
    }
    auto found=find_if(vdfCandidateSets.begin(),vdfCandidateSets.end(),[&](const auto& item){return item.context==record.context;});
    if (found==vdfCandidateSets.end()) {
        if (vdfCandidateSets.size()==8) vdfCandidateSets.pop_front();
        vdfCandidateSets.push_back({record.context,{}});
        found=prev(vdfCandidateSets.end());
    }
    auto before=found->records;
    found->records.push_back(record);
    found->records=MergeVdfCandidates(std::move(found->records));
    return before!=found->records;
}
uint64_t ReadVdfParentSlot() {
    return DBVdfParentSlot();
}
vector<VdfElection::Context> ReadVdfContexts() {
    shared_lock lock(dbCommitMutex);
    auto height=DBReadBlockHeight();
    if (height==UINT64_MAX) throw runtime_error("Block height overflow");
    auto current=VdfConsensus::Epoch(height+1);
    return {DBVdfContext(current),DBVdfContext(current+1)};
}
bool IsExpectedVdfContext(const VdfElection::Context& context) {
    if (!VdfConsensus::config.enabled) return false;
    for (const auto& expected:ReadVdfContexts()) if (expected==context) return true;
    return false;
}
void PublishVdfCandidate(const VdfElection::Candidate& record,uint64_t source=0) {
    auto encoded=VdfElection::EncodeCandidate(record);
    BroadcastData(GenerateMessage(49,encoded),source);
}
bool QueueVdfCandidate(const VdfElection::Candidate& record,uint64_t source) {
    if (!vdfVerifyThreads) return true;
    size_t count=vdfPendingProofs.load();
    do { if (count>=32) return false; } while (!vdfPendingProofs.compare_exchange_weak(count,count+1));
    try {
        post(*vdfVerifyThreads,[record,source] {
            struct Release { ~Release(){vdfPendingProofs--;} } release;
            try {
                if (!IsExpectedVdfContext(record.context)) { VdfConsensus::invalidProofs++; return; }
                auto best=ReadVdfCandidates(record.context);
                if (find(best.begin(),best.end(),record)!=best.end()) { vdfSkippedProofs++; return; }
                // A worse claimed score cannot enter the five verified records.
                // A better score is never admitted until its proof is checked.
                if (best.size()==5&&!VdfConsensus::CandidateLess(record,best.back())) { vdfSkippedProofs++; return; }
                if (!VdfConsensus::VerifyProof(record,record.context)) return;
                if (StoreVdfCandidate(record)) PublishVdfCandidate(record,source);
            } catch (const exception& error) { spdlog::warn("VDF message: {}",error.what()); }
        });
    } catch (...) { vdfPendingProofs--; throw; }
    return true;
}
void SendVdfCandidates(const shared_ptr<Peer>& peer) {
    if (!VdfConsensus::config.enabled) return;
    vector<VdfElection::Candidate> records;
    for (const auto& context:ReadVdfContexts()) {
        auto best=ReadVdfCandidates(context);
        records.insert(records.end(),best.begin(),best.end());
        if (auto own=ReadOwnVdfProof(context)) records.push_back(*own);
    }
    vector<VdfElection::EncodedCandidate> sent;
    for (const auto& record:records) {
        auto encoded=VdfElection::EncodeCandidate(record);
        if (find(sent.begin(),sent.end(),encoded)!=sent.end()) continue;
        SendData(peer,GenerateMessage(49,encoded)); sent.push_back(encoded);
    }
}

// Called by GenerateBlock with dbCommitMutex held, before taking txpoolMutex.
bool PrepareVdfBlock(uint64_t height,const array<uint8_t,32>& parent,VdfConsensus::Extension& extension) {
    if (!vdfIdentityReady||!produceBlocks||DBReadCurrentBlock()!=parent) return false;
    auto epoch=VdfConsensus::Epoch(height);
    extension.slot=VdfConsensus::CurrentSlot();
    if (extension.slot<VdfConsensus::MinimumSlot(height)) return false;
    auto parentSlot=ReadVdfParentSlot();
    if (extension.slot<=parentSlot) return false;
    extension.producer=vdfPublicKey;
    auto active=DBVdfActiveCandidates(height);
    auto member=any_of(active.begin(),active.end(),[](const auto& record){return record.publicKey==vdfPublicKey;});
    if (epoch<2||active.empty()||!member) {
        if (epoch>=2&&!active.empty()&&extension.slot-parentSlot<VdfConsensus::config.fallbackSlots) return false;
        extension.producerProof=ReadOwnVdfProof(DBVdfContext(epoch));
        if (!extension.producerProof) return false;
    }
    if (epoch>0) {
        auto candidates=DBVdfCandidatesForNextBlock();
        auto received=ReadVdfCandidates(DBVdfContext(epoch+1));
        candidates.insert(candidates.end(),received.begin(),received.end());
        extension.candidates=MergeVdfCandidates(std::move(candidates));
    }
    return true;
}
void LoadVdfIdentity(const string& dataPath,const string& configuredPath) {
    auto path=configuredPath.empty()?Utf8Path(dataPath)/"vdf-identity.seed":Utf8Path(configuredPath);
    if (filesystem::is_symlink(path)) throw runtime_error("VDF identity must be an ordinary seed file");
    if (!filesystem::exists(path)) {
        if (!configuredPath.empty()) throw runtime_error("VDF seed file does not exist");
        array<uint8_t,32> seed{}; randombytes_buf(seed.data(),seed.size());
        ofstream output(path,ios::out|ios::binary);
        if (!output) throw runtime_error("Cannot create VDF seed file");
        filesystem::permissions(path,filesystem::perms::owner_read|filesystem::perms::owner_write,filesystem::perm_options::replace);
        output<<U32ToHex(seed)<<'\n'; output.close();
        sodium_memzero(seed.data(),seed.size());
        if (!output) throw runtime_error("Cannot write VDF seed file");
    }
    if (!filesystem::is_regular_file(path)||filesystem::file_size(path)>128) throw runtime_error("Invalid VDF seed file");
    ifstream input(path); string text,extra;
    if (!(input>>text)||(input>>extra)||text.size()!=64) throw runtime_error("VDF seed file must contain 32 bytes encoded as hex");
    auto seed=HexToU32(text);
    if (crypto_sign_seed_keypair(vdfPublicKey.data(),vdfSecretKey.data(),seed.data())!=0) throw runtime_error("VDF key derivation failed");
    sodium_memzero(seed.data(),seed.size()); sodium_memzero(text.data(),text.size());
    vdfIdentityReady=true;
}
void InitializeVdfRuntime(const string& dataPath,const string& identityPath) {
    if (!VdfConsensus::config.enabled) return;
    if (produceBlocks) LoadVdfIdentity(dataPath,identityPath);
    prepareVdfBlock=PrepareVdfBlock;
    signVdfBlock=[](const array<uint8_t,32>& hash,VdfConsensus::Signature& signature) {
        if (!vdfIdentityReady) return false;
        signature=VdfConsensus::SignHash(hash,vdfSecretKey); return true;
    };
    vdfVerifyThreads=make_unique<thread_pool>(1);
    if (!produceBlocks) return;
    vdfComputeThread=thread([] {
        try {
            uint64_t lastRequest=0;
            while (nodeRunning) {
                auto contexts=ReadVdfContexts();
                for (const auto& context:contexts) {
                    if (!nodeRunning) break;
                    if (ReadOwnVdfProof(context)) continue;
                    auto start=GetSteadyTime();
                    auto record=VdfElection::CreateCandidate(context,vdfSecretKey);
                    vdfGeneratedNs+=GetSteadyTime()-start; vdfComputed++;
                    if (!VdfConsensus::VerifyProof(record,context)) throw runtime_error("Locally generated VDF failed verification");
                    if (StoreVdfCandidate(record,true)) PublishVdfCandidate(record);
                }
                auto now=GetSteadyTime();
                if (now-lastRequest>=1000000000ULL) {
                    BroadcastData(GenerateMessage(50,{})); lastRequest=now;
                }
                this_thread::sleep_for(milliseconds(20));
            }
        } catch (const exception& error) {
            spdlog::error("VDF worker stopped: {}",error.what());
            nodeFailed=true;
            if (nodeIO) nodeIO->stop();
        }
    });
}
void StopVdfRuntime() {
    if (vdfComputeThread.joinable()) vdfComputeThread.join();
    if (vdfVerifyThreads) { vdfVerifyThreads->join(); vdfVerifyThreads.reset(); }
    sodium_memzero(vdfSecretKey.data(),vdfSecretKey.size());
}
nlohmann::json GetVdfStats() {
    if (!VdfConsensus::config.enabled) return {{"enabled",false}};
    shared_lock lock(dbCommitMutex);
    auto height=DBReadBlockHeight()+1,epoch=VdfConsensus::Epoch(height);
    auto producers=nlohmann::json::array(),candidates=nlohmann::json::array();
    for (const auto& record:DBVdfActiveCandidates(height)) producers.push_back(U32ToHex(record.publicKey));
    if (epoch>0) for (const auto& record:DBVdfCandidatesForNextBlock())
        candidates.push_back({{"public_key",U32ToHex(record.publicKey)},{"score",U32ToHex(VdfConsensus::Score(record))}});
    VdfConsensus::Extension proposed;
    bool eligible=PrepareVdfBlock(height,DBReadCurrentBlock(),proposed);
    return {{"enabled",true},{"public_key",vdfIdentityReady?U32ToHex(vdfPublicKey):string{}},
        {"epoch",epoch},{"next_epoch",epoch+1},{"slot",VdfConsensus::CurrentSlot()},
        {"eligible",eligible},{"producers",producers},{"candidates",candidates},
        {"computed",vdfComputed.load()},{"generation_ms",vdfGeneratedNs.load()/1e6},
        {"proof_checks",VdfConsensus::proofChecks.load()},{"proof_cache_hits",VdfConsensus::proofCacheHits.load()},
        {"invalid_proofs",VdfConsensus::invalidProofs.load()},{"skipped_proofs",vdfSkippedProofs.load()},
        {"pending_proofs",vdfPendingProofs.load()}};
}

void RunVdfBlockLoop() {
    try {
        uint64_t lastProducedSlot=UINT64_MAX;
        uint64_t attemptedSlot=UINT64_MAX,attemptedVersion=UINT64_MAX;
        size_t attemptedReady=SIZE_MAX;
        optional<steady_clock::time_point> commitTime;
        const auto propagationMs=max(uint64_t(1),min(uint64_t(12),VdfConsensus::config.slotMs/4));
        while (nodeRunning||blockReady||forkReady) {
            if (ProcessForks()) { AnnounceBlock(DBReadBlockByHash(DBReadCurrentBlock())); commitTime.reset(); }
            auto slot=VdfConsensus::CurrentSlot();
            auto phase=uint64_t(GetLogicalTimeUs()/1000)%VdfConsensus::config.slotMs;
            size_t ready;
            { lock_guard lock(txpoolMutex); ready=readyPoolTx; }
            auto full=blockTxStep>1?maxBlockTx/blockTxStep*blockTxStep:maxBlockTx;
            const bool atDeadline=phase>=VdfConsensus::config.slotMs-propagationMs;
            const auto stateVersion=ReadChainHead().version;
            const bool earlyAttempt=full&&ready>=full&&
                (attemptedSlot!=slot||attemptedReady!=ready||attemptedVersion!=stateVersion);
            if (nodeRunning&&produceBlocks&&slot!=lastProducedSlot&&
                (earlyAttempt||atDeadline)) {
                attemptedSlot=slot; attemptedReady=ready; attemptedVersion=stateVersion;
                auto data=GenerateBlock(blockTxStep,atDeadline);
                if (!data.empty()&&(atDeadline||ReadU64(data.data()+72)>=full)) {
                    lastProducedSlot=slot;
                    if (ProcessBlock(data)) {
                        localProposals++;
                        auto count=ReadU64(data.data()+72);
                        if (blockTxStep>1&&count) { if (count<blockTxStep) blockTailProposals++; else blockStepProposals++; }
                        AnnounceBlock(data);
                    }
                }
            }
            if (blockReady) {
                if (!commitTime) commitTime=steady_clock::now()+milliseconds(propagationMs);
                if (!nodeRunning||steady_clock::now()>=*commitTime) {
                    pair<Block,vector<uint8_t>> selected;
                    { lock_guard lock(blockBufferLock); selected=std::move(BlockBuffer[0]); BlockBuffer[0]={}; blockReady=false; }
                    commitTime.reset();
                    if (!selected.second.empty()&&CommitBlock(selected.first,selected.second)) {
                        AnnounceBlock(selected.second); forkReady=true;
                    }
                }
            } else commitTime.reset();
            if (!nodeRunning&&!blockReady&&!forkReady) break;
            unique_lock lock(txpoolMutex);
            txpoolCondition.wait_for(lock,milliseconds(2));
        }
    } catch (const exception& error) {
        spdlog::error("VDF block loop stopped: {}",error.what());
        nodeFailed=true; nodeRunning=false;
        if (nodeIO) nodeIO->stop();
    }
}
