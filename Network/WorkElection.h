#pragma once

// Included after the node identity and send functions in Vdf.h. Work proving
// and received proof verification have their own workers; neither uses the
// transaction verification pool or waits on a socket coroutine.
#include "../Consensus/WorkRules.h"
#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <type_traits>

namespace WorkElection=chain::work_election;
namespace WorkRules=chain::work_consensus;

struct WorkRuntimeSet {
    WorkElection::BeaconContext context;
    vector<WorkElection::Commit> commits;
    vector<WorkElection::Reveal> reveals;
    vector<shared_ptr<const WorkElection::Ticket>> tickets;
};
mutex workRuntimeMutex,workDiskMutex,workQueueMutex;
deque<WorkRuntimeSet> workRuntimeSets;
map<pair<uint64_t,array<uint8_t,32>>,WorkElection::Work> workLocalCache;
filesystem::path workCacheDirectory,workSecretDirectory;
unique_ptr<thread_pool> workVerifyThreads,workBeaconThreads;
thread workControlThread;
set<array<uint8_t,32>> workActiveMessages,workVerifiedMessages;
deque<array<uint8_t,32>> workVerifiedOrder;
size_t workPendingBytes=0,workPendingTickets=0,workPendingBeacons=0;
atomic<uint64_t> workQueueRejected{0},workDuplicateMessages{0},workVerifiedTickets{0},workInvalidMessages{0};
atomic<uint64_t> workDiskHits{0},workComputedRounds{0},workComputeNs{0};
atomic<uint64_t> workProofVerifyNs{0},workProofChecks{0};
atomic<uint64_t> workLocalCompleted{0};
atomic<uint64_t> workStoredProofs{0};
constexpr size_t workQueueTicketLimit=32,workQueueBeaconLimit=64,workQueueByteLimit=2*1024*1024;

array<uint8_t,32> WorkMessageHash(uint8_t type,span<const uint8_t> data) {
    array<uint8_t,32> hash{};crypto_generichash_state state;
    crypto_generichash_init(&state,nullptr,0,hash.size());
    crypto_generichash_update(&state,&type,1);
    crypto_generichash_update(&state,data.data(),data.size());
    crypto_generichash_final(&state,hash.data(),hash.size());return hash;
}
WorkRuntimeSet& WorkSetLocked(const WorkElection::BeaconContext& context) {
    auto it=find_if(workRuntimeSets.begin(),workRuntimeSets.end(),[&](const auto& item){return item.context==context;});
    if (it!=workRuntimeSets.end()) return *it;
    if (workRuntimeSets.size()>=8) {
        workRuntimeSets.pop_front();
        // A returning branch must be able to refill its evicted record set.
        lock_guard queueLock(workQueueMutex);workVerifiedMessages.clear();workVerifiedOrder.clear();
    }
    workRuntimeSets.push_back({context,{},{},{}});return workRuntimeSets.back();
}
WorkRuntimeSet ReadWorkSet(const WorkElection::BeaconContext& context) {
    lock_guard lock(workRuntimeMutex);
    for (const auto& item:workRuntimeSets) if (item.context==context) return item;
    return {context,{},{},{}};
}
bool WorkTicketLess(const WorkElection::Ticket& left,const WorkElection::Ticket& right) {
    const auto a=WorkElection::DeriveScore(left.context,left.publicKey);
    const auto b=WorkElection::DeriveScore(right.context,right.publicKey);
    return a==b?left.publicKey<right.publicKey:a<b;
}
vector<shared_ptr<const WorkElection::Ticket>> MergeWorkTickets(
        vector<shared_ptr<const WorkElection::Ticket>> records) {
    sort(records.begin(),records.end(),[](const auto& a,const auto& b){return WorkTicketLess(*a,*b);});
    vector<shared_ptr<const WorkElection::Ticket>> result;result.reserve(10);
    for (auto& record:records) {
        if (any_of(result.begin(),result.end(),[&](const auto& item){return item->publicKey==record->publicKey;})) continue;
        result.push_back(std::move(record));if (result.size()==10) break;
    }
    return result;
}
// The caller holds dbCommitMutex. A head can be committed before its slot
// ends, and the logical clock can move back during synchronization. Reading
// the upcoming round therefore previews at least the slot after that head.
// The producing path below always checks and signs the actual current slot.
WorkRules::State ReadWorkStateLocked(uint64_t slot,uint64_t* previewSlot=nullptr) {
    const auto parentSlot=DBVdfParentSlot();
    if (parentSlot==UINT64_MAX) throw runtime_error("Work parent slot overflow");
    slot=max(max(slot,VdfConsensus::StartSlot()),parentSlot+1);
    if (previewSlot) *previewSlot=slot;
    return WorkRules::ForSlot(DBWorkStateForNextBlock(DBReadBlockHeight()+1,slot),slot,VdfConsensus::config);
}
WorkRules::State ReadWorkState(uint64_t slot,uint64_t* previewSlot=nullptr) {
    shared_lock lock(dbCommitMutex);
    return ReadWorkStateLocked(slot,previewSlot);
}
bool WorkMember(const WorkRules::State& state,const WorkElection::PublicKey& key) {
    return find(state.committee.begin(),state.committee.end(),key)!=state.committee.end();
}
uint64_t WorkPhase(const WorkRules::State& state,uint64_t slot) {
    return slot>=state.startSlot?slot-state.startSlot:0;
}
bool WorkBroadcastScore(const WorkElection::Ticket& ticket,const WorkRules::State& state,uint64_t slot) {
    const auto phase=WorkPhase(state,slot);
    if (!state.seedReady||state.frozen||phase<VdfConsensus::config.revealEndSlot||phase>=VdfConsensus::config.proofEndSlot||
            ticket.context!=WorkRules::TicketContextFor(state,VdfConsensus::config)) return false;
    unsigned percent=phase<VdfConsensus::config.revealEndSlot+10?5:
        (phase<VdfConsensus::config.ticketEndSlot?15:100);
    const auto score=WorkElection::DeriveScore(ticket.context,ticket.publicKey);
    // Compare the first 32 score bits against an absolute hash percentile.
    const uint64_t prefix=(uint64_t(score[0])<<24)|(uint64_t(score[1])<<16)|(uint64_t(score[2])<<8)|score[3];
    return percent==100||prefix*100<(uint64_t(1)<<32)*percent;
}
bool WorkTicketCompetitive(const WorkElection::Ticket& ticket,const WorkRules::State& state) {
    auto records=ReadWorkSet(WorkRules::BeaconContextFor(state)).tickets;
    erase_if(records,[&](const auto& item){return item->context!=ticket.context;});
    records.insert(records.end(),state.pool.begin(),state.pool.end());records=MergeWorkTickets(std::move(records));
    for (const auto& item:records) if (item->publicKey==ticket.publicKey) return false;
    return records.size()<10||WorkTicketLess(ticket,*records.back());
}
bool StoreWorkTicket(shared_ptr<const WorkElection::Ticket> record,const WorkRules::State& state) {
    lock_guard lock(workRuntimeMutex);auto& set=WorkSetLocked(WorkRules::BeaconContextFor(state));
    if (erase_if(set.tickets,[&](const auto& item){return item->context!=record->context;})) {
        lock_guard queueLock(workQueueMutex);workVerifiedMessages.clear();workVerifiedOrder.clear();
    }
    if (any_of(set.tickets.begin(),set.tickets.end(),[&](const auto& item){return item->publicKey==record->publicKey;})) return false;
    set.tickets.push_back(record);set.tickets=MergeWorkTickets(std::move(set.tickets));
    const bool kept=find(set.tickets.begin(),set.tickets.end(),record)!=set.tickets.end();
    if (kept) vdfOwnProofVersion++;return kept;
}
template<class Record> bool StoreWorkBeacon(const Record& record) {
    lock_guard lock(workRuntimeMutex);auto& set=WorkSetLocked(record.context);
    auto& records=[&]() -> auto& {
        if constexpr(is_same_v<Record,WorkElection::Commit>) return set.commits;
        else return set.reveals;
    }();
    const auto found=find_if(records.begin(),records.end(),[&](const auto& item){return item.publicKey==record.publicKey;});
    if (found!=records.end()) {
        if constexpr(is_same_v<Record,WorkElection::Reveal>) {
            if (*found!=record) {*found=record;vdfOwnProofVersion++;return true;}
        }
        return false;
    }
    records.push_back(record);vdfOwnProofVersion++;return true;
}
void PublishWorkMessage(uint8_t type,const vector<uint8_t>& encoded,uint64_t source=0) {
    BroadcastData(GenerateMessage(type,encoded),source);
}

// A seed is written before publishing its commitment. Restarting an epoch
// cannot cause this node to sign a different commitment for the same anchor.
void WriteWorkFile(const filesystem::path& path,span<const uint8_t> bytes,bool secret) {
    if (filesystem::is_symlink(path)) throw runtime_error("Work cache path is a symbolic link");
    auto temporary=path;temporary+=".tmp";
    if (filesystem::is_symlink(temporary)) throw runtime_error("Work temporary path is a symbolic link");
    ofstream output(temporary,ios::binary|ios::trunc);
    if (!output) throw runtime_error("Cannot create Work cache file");
    if (secret) filesystem::permissions(temporary,filesystem::perms::owner_read|filesystem::perms::owner_write,filesystem::perm_options::replace);
    output.write(reinterpret_cast<const char*>(bytes.data()),streamsize(bytes.size()));output.close();
    if (!output) throw runtime_error("Cannot write Work cache file");
#ifdef _WIN32
    if (!MoveFileExW(temporary.c_str(),path.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH))
        throw runtime_error("Cannot replace Work cache file");
#else
    filesystem::rename(temporary,path);
#endif
}
vector<uint8_t> ReadWorkFile(const filesystem::path& path,size_t maximum) {
    if (!filesystem::exists(path)) return {};
    if (filesystem::is_symlink(path)||!filesystem::is_regular_file(path)||filesystem::file_size(path)>maximum)
        throw runtime_error("Invalid Work cache file");
    ifstream input(path,ios::binary);if (!input) throw runtime_error("Cannot read Work cache file");
    return vector<uint8_t>((istreambuf_iterator<char>(input)),istreambuf_iterator<char>());
}
filesystem::path WorkProofPath(uint64_t round,const WorkElection::Digest& anchor) {
    return workCacheDirectory/(to_string(round)+"-"+U32ToHex(anchor)+".work");
}
bool ReadLocalWork(uint64_t round,const WorkElection::Digest& anchor,WorkElection::Work& work) {
    lock_guard lock(workDiskMutex);const auto key=make_pair(round,anchor);
    if (auto item=workLocalCache.find(key);item!=workLocalCache.end()) {work=item->second;return true;}
    auto bytes=ReadWorkFile(WorkProofPath(round,anchor),4096);if (bytes.empty()) return false;
    if (!WorkElection::DecodeWork(bytes,work)||work.round!=round||work.anchor!=anchor) throw runtime_error("Invalid cached Work record");
    const auto started=GetSteadyTime();
    const bool valid=WorkElection::VerifyWork(genesisRoot,vdfPublicKey,work,VdfConsensus::config.workIterations);
    workProofVerifyNs+=GetSteadyTime()-started;workProofChecks++;
    if (!valid) throw runtime_error("Cached Work proof failed verification");
    if (workLocalCache.size()>=256) workLocalCache.erase(workLocalCache.begin());
    workLocalCache.emplace(key,work);workDiskHits++;return true;
}
void SaveLocalWork(const WorkElection::Work& work) {
    lock_guard lock(workDiskMutex);const auto path=WorkProofPath(work.round,work.anchor);
    const bool existing=filesystem::exists(path);
    WriteWorkFile(path,WorkElection::EncodeWork(work),false);if (!existing) workStoredProofs++;
    if (workLocalCache.size()>=256) workLocalCache.erase(workLocalCache.begin());
    workLocalCache[{work.round,work.anchor}]=work;
}
WorkElection::Digest LoadWorkSecret(const WorkElection::BeaconContext& context) {
    lock_guard lock(workDiskMutex);
    const auto path=workSecretDirectory/(to_string(context.targetEpoch)+"-"+U32ToHex(context.anchor)+".seed");
    auto bytes=ReadWorkFile(path,32);WorkElection::Digest secret{};
    if (bytes.empty()) {
        if (filesystem::exists(path)) throw runtime_error("Empty Work election secret");
        randombytes_buf(secret.data(),secret.size());WriteWorkFile(path,secret,true);
    }
    else {
        if (bytes.size()!=secret.size()) throw runtime_error("Invalid Work election secret");
        copy(bytes.begin(),bytes.end(),secret.begin());sodium_memzero(bytes.data(),bytes.size());
    }
    return secret;
}

map<uint64_t,WorkElection::Digest> ReadWorkAnchors(const WorkElection::Context& context,const WorkElection::PublicKey& key) {
    const auto samples=WorkElection::SampleGroups(context,key);
    map<uint64_t,WorkElection::Digest> result;
    shared_lock lock(dbCommitMutex);
    for (const auto& group:samples) for (uint64_t n=0;n<group.count;++n) {
        const auto round=group.start+n;
        if (!result.count(round)) result.emplace(round,DBWorkAnchor(round));
    }
    return result;
}
void FinishWorkMessage(const array<uint8_t,32>& hash,size_t bytes,bool ticket,bool valid) {
    lock_guard lock(workQueueMutex);workActiveMessages.erase(hash);workPendingBytes-=bytes;
    if (ticket) --workPendingTickets;else --workPendingBeacons;
    if (valid&&workVerifiedMessages.insert(hash).second) {
        workVerifiedOrder.push_back(hash);
        if (workVerifiedOrder.size()>4096) {workVerifiedMessages.erase(workVerifiedOrder.front());workVerifiedOrder.pop_front();}
    }
}
bool QueueWorkMessage(uint8_t type,vector<uint8_t> bytes,uint64_t source) {
    if (!VdfConsensus::config.workElection||!workVerifyThreads||!workBeaconThreads) return true;
    const bool ticket=type==62;const auto hash=WorkMessageHash(type,bytes);const auto size=bytes.size();
    {
        lock_guard lock(workQueueMutex);
        if (workActiveMessages.count(hash)||workVerifiedMessages.count(hash)) {workDuplicateMessages++;return true;}
        if ((ticket?workPendingTickets>=workQueueTicketLimit:workPendingBeacons>=workQueueBeaconLimit)||
                size>workQueueByteLimit-workPendingBytes) {workQueueRejected++;return false;}
        workActiveMessages.insert(hash);workPendingBytes+=size;
        if (ticket) ++workPendingTickets;else ++workPendingBeacons;
    }
    try {
        post(ticket?*workVerifyThreads:*workBeaconThreads,[type,source,hash,size,ticket,bytes=std::move(bytes)] {
            bool valid=false;
            struct Release { const array<uint8_t,32>& hash;size_t size;bool ticket;bool& valid;
                ~Release(){FinishWorkMessage(hash,size,ticket,valid);} } release{hash,size,ticket,valid};
            try {
                if (!nodeRunning) return;
                auto slot=VdfConsensus::CurrentSlot();auto state=ReadWorkState(slot);
                const auto beacon=WorkRules::BeaconContextFor(state);const auto phase=WorkPhase(state,slot);
                if (type==60) {
                    WorkElection::Commit record;
                    if (!WorkElection::DecodeCommit(bytes,record)||!WorkMember(state,record.publicKey)||
                            !WorkElection::VerifyCommit(record,beacon)) {workInvalidMessages++;return;}
                    if (phase<VdfConsensus::config.commitStartSlot||phase>=VdfConsensus::config.commitEndSlot) return;
                    valid=true;if (StoreWorkBeacon(record)) PublishWorkMessage(type,bytes,source);
                } else if (type==61) {
                    WorkElection::Reveal record;
                    if (!WorkElection::DecodeReveal(bytes,record)||!WorkMember(state,record.publicKey)) {workInvalidMessages++;return;}
                    auto commitment=find_if(state.commits.begin(),state.commits.end(),[&](const auto& item){return item.publicKey==record.publicKey;});
                    if (commitment==state.commits.end()) return;
                    if (!WorkElection::VerifyReveal(record,beacon,*commitment)) {workInvalidMessages++;return;}
                    if (phase<VdfConsensus::config.commitEndSlot||phase>=VdfConsensus::config.revealEndSlot) return;
                    valid=true;if (StoreWorkBeacon(record)) PublishWorkMessage(type,bytes,source);
                } else if (type==62) {
                    WorkElection::Ticket record;
                    if (!WorkElection::DecodeTicket(bytes,record)) {workInvalidMessages++;return;}
                    if (!state.seedReady||record.context!=WorkRules::TicketContextFor(state,VdfConsensus::config)||
                            !WorkBroadcastScore(record,state,slot)||!WorkTicketCompetitive(record,state)) return;
                    auto anchors=ReadWorkAnchors(record.context,record.publicKey);
                    const auto started=GetSteadyTime();
                    const bool verified=WorkElection::VerifyTicket(record,record.context,[&](uint64_t round,WorkElection::Digest& anchor) {
                            auto found=anchors.find(round);if (found==anchors.end()) return false;anchor=found->second;return true;
                        },nullptr,&WorkRules::proofCache);
                    workProofVerifyNs+=GetSteadyTime()-started;workProofChecks+=record.works.size();
                    if (!verified) {workInvalidMessages++;return;}
                    // Proof checking can span slots or a reorganization. Admit
                    // only against the current branch and election context.
                    slot=VdfConsensus::CurrentSlot();auto current=ReadWorkState(slot);
                    if (!current.seedReady||record.context!=WorkRules::TicketContextFor(current,VdfConsensus::config)||
                            !WorkBroadcastScore(record,current,slot)) return;
                    auto currentAnchors=ReadWorkAnchors(record.context,record.publicKey);
                    if (anchors!=currentAnchors) return;
                    valid=true;workVerifiedTickets++;
                    if (StoreWorkTicket(make_shared<const WorkElection::Ticket>(std::move(record)),current)) PublishWorkMessage(type,bytes,source);
                }
                if (valid) WakeMainLoop();
            } catch (const exception& error) {workInvalidMessages++;spdlog::warn("Work election message: {}",error.what());}
        });
    } catch (...) {FinishWorkMessage(hash,size,ticket,false);throw;}
    return true;
}

void SendWorkRecords(const shared_ptr<Peer>& peer) {
    const auto now=GetSteadyTime();if (now-peer->workRecordsSentAt<500'000'000ULL) return;
    peer->workRecordsSentAt=now;
    const auto slot=VdfConsensus::CurrentSlot();const auto state=ReadWorkState(slot);
    const auto records=ReadWorkSet(WorkRules::BeaconContextFor(state));const auto phase=WorkPhase(state,slot);
    if (phase<VdfConsensus::config.commitEndSlot)
        for (const auto& record:records.commits) if (none_of(state.commits.begin(),state.commits.end(),[&](const auto& item){return item.publicKey==record.publicKey;}))
            SendData(peer,GenerateMessage(60,WorkElection::EncodeCommit(record)));
    if (phase>=VdfConsensus::config.commitEndSlot&&phase<VdfConsensus::config.revealEndSlot)
        for (const auto& record:records.reveals) if (none_of(state.reveals.begin(),state.reveals.end(),[&](const auto& item){return item.publicKey==record.publicKey;}))
            SendData(peer,GenerateMessage(61,WorkElection::EncodeReveal(record)));
    for (const auto& record:records.tickets) if (WorkBroadcastScore(*record,state,slot)&&
            none_of(state.pool.begin(),state.pool.end(),[&](const auto& item){return item->publicKey==record->publicKey;}))
        SendData(peer,GenerateMessage(62,WorkElection::EncodeTicket(*record)));
}

bool PrepareWorkBlock(uint64_t height,const array<uint8_t,32>& parent,VdfConsensus::Extension& extension) {
    if (!vdfIdentityReady||!produceBlocks||DBReadCurrentBlock()!=parent) return false;
    const auto slot=VdfConsensus::CurrentSlot();if (slot<VdfConsensus::StartSlot()) return false;
    // In particular, the last block of a round may have arrived during this
    // same slot. Do not initialize the next round with lastSlot == startSlot.
    if (slot<=DBVdfParentSlot()) return false;
    auto state=WorkRules::ForSlot(DBWorkStateForNextBlock(height,slot),slot,VdfConsensus::config);
    auto schedule=WorkRules::Preview(state,slot,VdfConsensus::config);
    if (!schedule||schedule->producer!=vdfPublicKey) return false;
    WorkRules::Delta delta;const auto records=ReadWorkSet(WorkRules::BeaconContextFor(state));
    const auto phase=WorkPhase(state,slot);
    if (phase>=VdfConsensus::config.commitStartSlot&&phase<VdfConsensus::config.commitEndSlot)
        for (const auto& record:records.commits) if (none_of(state.commits.begin(),state.commits.end(),[&](const auto& item){return item.publicKey==record.publicKey;})) delta.commits.push_back(record);
    if (phase>=VdfConsensus::config.commitEndSlot&&phase<VdfConsensus::config.revealEndSlot)
        for (const auto& record:records.reveals) if (none_of(state.reveals.begin(),state.reveals.end(),[&](const auto& item){return item.publicKey==record.publicKey;})) {
            const auto found=find_if(state.commits.begin(),state.commits.end(),[&](const auto& item){return item.publicKey==record.publicKey;});
            if (found!=state.commits.end()&&WorkElection::VerifyReveal(record,WorkRules::BeaconContextFor(state),*found)) delta.reveals.push_back(record);
        }
    if (phase>=VdfConsensus::config.revealEndSlot&&phase<VdfConsensus::config.proofEndSlot) {
        auto merged=state.pool;
        for (const auto& record:records.tickets) if (record->context==WorkRules::TicketContextFor(state,VdfConsensus::config)) merged.push_back(record);
        merged=MergeWorkTickets(std::move(merged));
        size_t bytes=5;
        const size_t budget=min(size_t(VdfConsensus::kMaxWorkDeltaBytes),size_t(256*1024));
        for (const auto& record:merged) if (record->context==WorkRules::TicketContextFor(state,VdfConsensus::config)&&
                none_of(state.pool.begin(),state.pool.end(),[&](const auto& item){return item->publicKey==record->publicKey;})) {
            const auto size=4+WorkElection::kTicketHeaderBytes+crypto_sign_BYTES+record->works.size()*WorkElection::kWorkBytes;
            if (size>budget-bytes) break;
            bytes+=size;delta.tickets.push_back(*record);
        }
    }
    if (phase>=VdfConsensus::config.proofEndSlot&&!state.frozen) delta.finalization=WorkRules::ExpectedFinalization(state,slot,VdfConsensus::config);
    extension.slot=slot;extension.producer=vdfPublicKey;extension.turn=schedule->turn;
    extension.work=WorkRules::EncodeDelta(delta);return true;
}

void RunWorkComputation() {
    try {
        uint64_t next=0;array<uint8_t,chain::vdf::kOutputBytes> previous{};WorkElection::Digest priorAnchor{};
        while (nodeRunning) {
            WorkElection::Digest anchor{};bool available=false;
            {
                shared_lock lock(dbCommitMutex);const auto completed=(DBReadBlockHeight()-1)/VdfConsensus::config.epochBlocks;
                if (next&&((next>completed)||DBWorkAnchor(next-1)!=priorAnchor)) {next=0;previous={};priorAnchor={};}
                if (next<completed) {anchor=DBWorkAnchor(next);available=true;}
            }
            if (!available) {this_thread::sleep_for(milliseconds(50));continue;}
            WorkElection::Work work;
            if (!ReadLocalWork(next,anchor,work)||work.previousOutput!=previous) {
                const auto start=GetSteadyTime();
                work=WorkElection::CreateWork(genesisRoot,vdfPublicKey,next,anchor,previous,VdfConsensus::config.workIterations);
                workComputeNs+=GetSteadyTime()-start;workComputedRounds++;
                SaveLocalWork(work);
            }
            previous=WorkElection::WorkOutput(work);priorAnchor=anchor;++next;workLocalCompleted=next;
        }
    } catch (const exception& error) {
        spdlog::error("Work calculation stopped: {}",error.what());nodeFailed=true;nodeRunning=false;if (nodeIO) nodeIO->stop();
    }
}
void RunWorkControl() {
    try {
        uint64_t lastRequest=0,lastBroadcast=0,lastTicketAttempt=0;
        optional<WorkElection::Context> attemptedTicket;
        shared_ptr<const WorkElection::Ticket> ownTicket;
        optional<WorkElection::BeaconContext> secretContext;
        WorkElection::Digest secret{};
        optional<WorkElection::Commit> ownCommit;
        optional<WorkElection::Reveal> ownReveal;
        struct ClearSecret {WorkElection::Digest& value;optional<WorkElection::Reveal>& reveal;
            ~ClearSecret(){sodium_memzero(value.data(),value.size());if(reveal)sodium_memzero(reveal->value.data(),reveal->value.size());}} clearSecret{secret,ownReveal};
        while (nodeRunning) {
            const auto slot=VdfConsensus::CurrentSlot();const auto state=ReadWorkState(slot);
            const auto phase=WorkPhase(state,slot);const auto beacon=WorkRules::BeaconContextFor(state);
            const auto now=GetSteadyTime();
            if (secretContext&&(*secretContext!=beacon||phase>=VdfConsensus::config.revealEndSlot)) {
                sodium_memzero(secret.data(),secret.size());
                if (ownReveal) sodium_memzero(ownReveal->value.data(),ownReveal->value.size());
                secretContext.reset();ownCommit.reset();ownReveal.reset();
            }
            if (vdfIdentityReady&&WorkMember(state,vdfPublicKey)&&phase>=VdfConsensus::config.commitStartSlot&&phase<VdfConsensus::config.revealEndSlot) {
                if (!secretContext||*secretContext!=beacon) {
                    sodium_memzero(secret.data(),secret.size());secret=LoadWorkSecret(beacon);secretContext=beacon;
                    ownCommit=WorkElection::CreateCommit(beacon,vdfSecretKey,secret);
                    ownReveal=WorkElection::CreateReveal(beacon,vdfSecretKey,secret);
                }
                if (phase<VdfConsensus::config.commitEndSlot) {
                    const auto& record=*ownCommit;
                    if (none_of(state.commits.begin(),state.commits.end(),[](const auto& item){return item.publicKey==vdfPublicKey;})&&
                            (StoreWorkBeacon(record)||now-lastBroadcast>=1'000'000'000ULL)) PublishWorkMessage(60,WorkElection::EncodeCommit(record));
                } else {
                    const auto found=find_if(state.commits.begin(),state.commits.end(),[](const auto& record){return record.publicKey==vdfPublicKey;});
                    const auto& record=*ownReveal;
                    if (found!=state.commits.end()&&none_of(state.reveals.begin(),state.reveals.end(),[](const auto& item){return item.publicKey==vdfPublicKey;})&&
                            WorkElection::VerifyReveal(record,beacon,*found)&&
                            (StoreWorkBeacon(record)||now-lastBroadcast>=1'000'000'000ULL)) PublishWorkMessage(61,WorkElection::EncodeReveal(record));
                }
            }
            if (vdfIdentityReady&&state.seedReady&&!state.frozen&&phase<VdfConsensus::config.proofEndSlot) {
                const auto context=WorkRules::TicketContextFor(state,VdfConsensus::config);
                if (!attemptedTicket||*attemptedTicket!=context) {attemptedTicket=context;ownTicket.reset();lastTicketAttempt=0;}
                WorkElection::Ticket ranked;ranked.context=context;ranked.publicKey=vdfPublicKey;
                if (context.matureRounds&&!ownTicket&&now-lastTicketAttempt>=500'000'000ULL&&
                        WorkBroadcastScore(ranked,state,slot)&&WorkTicketCompetitive(ranked,state)) {
                    lastTicketAttempt=now;
                    auto anchors=ReadWorkAnchors(context,vdfPublicKey);map<uint64_t,WorkElection::Work> works;bool complete=true;
                    for (const auto& [round,anchor]:anchors) {WorkElection::Work work;if (!ReadLocalWork(round,anchor,work)) {complete=false;break;}works.emplace(round,work);}
                    if (complete) {
                        auto ticket=WorkElection::CreateTicket(context,vdfSecretKey,[&](uint64_t round,WorkElection::Work& work) {
                            auto it=works.find(round);if (it==works.end()) return false;work=it->second;return true;
                        });
                        ownTicket=make_shared<const WorkElection::Ticket>(std::move(ticket));
                    }
                }
                if (ownTicket&&WorkBroadcastScore(*ownTicket,state,slot)&&WorkTicketCompetitive(*ownTicket,state))
                    QueueWorkMessage(62,WorkElection::EncodeTicket(*ownTicket),0);
            }
            if (now-lastBroadcast>=1'000'000'000ULL) {
                // Relay new verified pool entries once. Only the originating
                // node retries its own missing ticket, rather than every node
                // broadcasting all ten proof bundles every second.
                if (ownTicket&&WorkBroadcastScore(*ownTicket,state,slot)&&
                        none_of(state.pool.begin(),state.pool.end(),[](const auto& item){return item->publicKey==vdfPublicKey;})) {
                    const auto records=ReadWorkSet(beacon);
                    if (any_of(records.tickets.begin(),records.tickets.end(),[&](const auto& item){return item->publicKey==vdfPublicKey&&item->context==ownTicket->context;}))
                        PublishWorkMessage(62,WorkElection::EncodeTicket(*ownTicket));
                }
                lastBroadcast=now;
            }
            if (phase>=VdfConsensus::config.commitStartSlot&&phase<VdfConsensus::config.proofEndSlot&&now-lastRequest>=2'000'000'000ULL) {
                array<uint8_t,32> salt{};randombytes_buf(salt.data(),salt.size());
                auto peers=SelectBroadcastPeers(0,salt,1,true);
                for (const auto& peer:peers) SendData(peer,GenerateMessage(50,{}));
                lastRequest=now;
            }
            this_thread::sleep_for(milliseconds(20));
        }
    } catch (const exception& error) {
        spdlog::error("Work election worker stopped: {}",error.what());nodeFailed=true;nodeRunning=false;if (nodeIO) nodeIO->stop();
    }
}
void InitializeWorkRuntime(const string& dataPath) {
    const auto base=Utf8Path(dataPath)/"work-vdf"/U32ToHex(genesisRoot)/U32ToHex(vdfPublicKey)/to_string(VdfConsensus::config.workIterations);
    workCacheDirectory=base/"proofs";workSecretDirectory=base/"secrets";
    filesystem::create_directories(workCacheDirectory);filesystem::create_directories(workSecretDirectory);
    filesystem::permissions(workSecretDirectory,filesystem::perms::owner_all,filesystem::perm_options::replace);
    workStoredProofs=0;
    for (const auto& file:filesystem::directory_iterator(workCacheDirectory)) if (file.is_regular_file()&&file.path().extension()==".work") workStoredProofs++;
    workVerifyThreads=make_unique<thread_pool>(1);workBeaconThreads=make_unique<thread_pool>(1);
    if (produceBlocks) vdfComputeThread=thread(RunWorkComputation);
    workControlThread=thread(RunWorkControl);
}
void StopWorkRuntime() {
    if (workControlThread.joinable()) workControlThread.join();
    if (vdfComputeThread.joinable()) vdfComputeThread.join();
    if (workVerifyThreads) {workVerifyThreads->join();workVerifyThreads.reset();}
    if (workBeaconThreads) {workBeaconThreads->join();workBeaconThreads.reset();}
}
nlohmann::json GetWorkStats() {
    const auto slot=VdfConsensus::CurrentSlot();uint64_t previewSlot=slot;
    const auto state=ReadWorkState(slot,&previewSlot);
    nlohmann::json producers=nlohmann::json::array(),next=nlohmann::json::array(),candidates=nlohmann::json::array();
    for (const auto& key:state.committee) producers.push_back(U32ToHex(key));
    for (const auto& key:state.nextCommittee) next.push_back(U32ToHex(key));
    for (const auto& record:state.pool) candidates.push_back({{"public_key",U32ToHex(record->publicKey)},{"score",U32ToHex(WorkElection::DeriveScore(record->context,record->publicKey))}});
    const auto scheduled=WorkRules::Preview(state,previewSlot,VdfConsensus::config);
    const auto matureRounds=state.epoch?state.epoch-1:0;
    size_t pendingTickets,pendingBeacons,pendingBytes;
    {lock_guard lock(workQueueMutex);pendingTickets=workPendingTickets;pendingBeacons=workPendingBeacons;pendingBytes=workPendingBytes;}
    return {{"enabled",true},{"work_election",true},{"public_key",vdfIdentityReady?U32ToHex(vdfPublicKey):string{}},
        {"producer_rotation",true},{"consecutive_blocks",VdfConsensus::config.consecutiveBlocks},
        {"producer_timeout_slots",VdfConsensus::config.producerTimeoutSlots},{"epoch",state.epoch},{"next_epoch",state.epoch+1},
        {"slot",slot},{"phase",WorkPhase(state,slot)},{"scheduled_producer",scheduled?U32ToHex(scheduled->producer):string{}},
        {"eligible",slot==previewSlot&&scheduled&&scheduled->producer==vdfPublicKey},{"producers",producers},{"next_producers",next},{"candidates",candidates},
        {"work_iterations",VdfConsensus::config.workIterations},{"work_groups",VdfConsensus::config.workGroups},{"work_group_size",VdfConsensus::config.workGroupSize},
        {"work_mature_rounds",matureRounds},{"work_sample_count",min(matureRounds,VdfConsensus::config.workGroups*VdfConsensus::config.workGroupSize)},
        {"work_computed",workComputedRounds.load()},{"work_completed",workLocalCompleted.load()},{"work_stored",workStoredProofs.load()},{"work_disk_hits",workDiskHits.load()},
        {"work_generation_ms",workComputeNs.load()/1e6},{"pending_proofs",pendingTickets},{"pending_beacons",pendingBeacons},{"pending_bytes",pendingBytes},
        {"proof_checks",workProofChecks.load()},{"work_ticket_checks",workVerifiedTickets.load()},{"work_proof_verify_ms",workProofVerifyNs.load()/1e6},
        {"prepare_attempts",vdfPrepareAttempts.load()},{"declined_attempt_hits",vdfDeclinedAttemptHits.load()},
        {"work_state_cache_hits",workStateCacheHits.load()},{"work_state_replays",workStateReplays.load()},
        {"work_anchor_cache_hits",workAnchorCacheHits.load()},{"work_ticket_cache_size",WorkRules::proofCache.Size()},
        {"queue_rejected",workQueueRejected.load()},{"duplicate_messages",workDuplicateMessages.load()},
        {"invalid_proofs",workInvalidMessages.load()},{"random_seed_ready",state.seedReady},{"election_frozen",state.frozen},{"missing_reveals",state.missingMask}};
}
