#ifndef CHAIN_NETWORK_BLOCKS_H
#define CHAIN_NETWORK_BLOCKS_H
#include "CompactCodec.h"
// 摘要保留区块头、交易哈希和输入类型，缺少的完整交易按索引补传。
struct CompactBlock {
    vector<uint8_t> header;
    vector<uint8_t> vdf;
    vector<array<uint8_t,32>> hashes;
    vector<uint8_t> contracts;
    vector<vector<uint8_t>> transactions;
    vector<uint64_t> shortIds;
    chain::compact::Key shortKey{};
    weak_ptr<Peer> source;
    vector<weak_ptr<Peer>> sources;
    unordered_set<uint64_t> requestedSources;
    uint64_t sourceId=0,created=0,lastRequest=0;
    unsigned attempts=0;
    bool shortEncoding=false,repairedAll=false,fullFallback=false,awaitingVerification=false;
    size_t missing=0,bytes=0,encodedBytes=0;
};
mutex relayMutex;
unordered_map<array<uint8_t,32>,shared_ptr<CompactBlock>,GetMapHash> compactBlocks;
unordered_map<array<uint8_t,32>,shared_ptr<CompactBlock>,GetMapHash> compactBuilding;
unordered_map<array<uint8_t,32>,shared_ptr<const vector<uint8_t>>,GetMapHash> relayBlocks;
unordered_map<array<uint8_t,32>,uint64_t,GetMapHash> blockSources,announcedBlocks,inflightBlocks;
deque<array<uint8_t,32>> relayOrder,sourceOrder,announceOrder;
unordered_map<array<uint8_t,32>,vector<uint8_t>,GetMapHash> recentBlockTransactions;
deque<array<uint8_t,32>> relayTransactionOrder;
size_t relayTransactionBytes=0;
size_t maxRelayTransactionBytes=32*1024*1024,maxRecentBlockTransactions=131072;
size_t compactBytes=0,relayBytes=0;
constexpr size_t maxCompactBytes=32*1024*1024,maxCompactBlocks=16,maxRelayBytes=32*1024*1024;
atomic<uint64_t> compactAnnouncements{0},compactReconstructed{0},missingTransactions{0},relaySuppressed{0};
atomic<uint64_t> syncRequests{0},syncRetries{0},syncBlocks{0};
atomic<uint64_t> recentTransactionHits{0};
atomic<uint64_t> compactShortAnnouncements{0},compactRepairRetries{0},compactFullFallbacks{0},compactRootFailures{0};
bool compactPrefill=true;
size_t compactPrefillMax=20,compactPrefillBytes=16384;
bool (*compactPeerKnowsTransaction)(const shared_ptr<Peer>&,const array<uint8_t,32>&)=nullptr;
atomic<uint64_t> compactPrefilledAnnouncements{0},compactPrefilledTransactions{0},compactPrefilledReceived{0};
unsigned compactRepairMs=200;
atomic<size_t> compactScanBytes{0};
atomic<uint64_t> compactScanSnapshots{0},compactScanSnapshotHits{0};
constexpr size_t maxCompactScanBytes=16*1024*1024,maxCompactScanTransactions=262144;
struct CompactScanReservation {
    size_t bytes=0;
    bool Reserve(size_t wanted) {
        auto used=compactScanBytes.load();
        while (wanted<=maxCompactScanBytes-used) {
            if (compactScanBytes.compare_exchange_weak(used,used+wanted)) { bytes=wanted; return true; }
        }
        return false;
    }
    ~CompactScanReservation() { compactScanBytes.fetch_sub(bytes); }
};
struct CompactHashSnapshot {
    CompactScanReservation reservation;
    vector<array<uint8_t,32>> hashes;
    uint64_t created=0;
};
mutex compactSnapshotMutex;
shared_ptr<const CompactHashSnapshot> compactHashSnapshot;
shared_ptr<const CompactHashSnapshot> ReadCompactHashSnapshot() {
    // Competing candidates usually inspect the same pool. Share the copied
    // hashes for 20 ms, but derive every block's SipHash IDs independently.
    unique_lock snapshotLock(compactSnapshotMutex);
    auto now=GetSteadyTime();
    if (compactHashSnapshot&&now>=compactHashSnapshot->created&&now-compactHashSnapshot->created<20000000ULL) {
        compactScanSnapshotHits++; return compactHashSnapshot;
    }
    size_t count=0;
    { lock_guard lock(txpoolMutex); count=min(maxCompactScanTransactions,txpool.size()); }
    { lock_guard lock(relayMutex); count+=min(maxCompactScanTransactions-count,recentBlockTransactions.size()); }
    auto snapshot=make_shared<CompactHashSnapshot>();
    if (!snapshot->reservation.Reserve(count*sizeof(array<uint8_t,32>))) return {};
    snapshot->hashes.reserve(count);
    { lock_guard lock(txpoolMutex); for (const auto& [hash,tx]:txpool) {
        if (snapshot->hashes.size()==count) break;
        snapshot->hashes.push_back(hash);
    } }
    { lock_guard lock(relayMutex); for (const auto& [hash,tx]:recentBlockTransactions) {
        if (snapshot->hashes.size()==count) break;
        snapshot->hashes.push_back(hash);
    } }
    snapshot->created=GetSteadyTime(); compactScanSnapshots++;
    compactHashSnapshot=snapshot; return snapshot;
}
void ResetCompactHashSnapshot() { lock_guard lock(compactSnapshotMutex); compactHashSnapshot.reset(); }
struct CompactBuildReservation {
    array<uint8_t,32> hash{};
    bool active=false;
    ~CompactBuildReservation() { if (active) { lock_guard lock(relayMutex); compactBuilding.erase(hash); } }
};

uint64_t PeerId(const shared_ptr<Peer>& peer) { return peer->port?peer->key:0; }
size_t CompactEncodedBase(const CompactBlock& block) {
    auto contracts=count(block.contracts.begin(),block.contracts.end(),uint8_t(1));
    return 112+block.hashes.size()*176+block.vdf.size()+(contracts?8+size_t(contracts)*8:0);
}
// Caller holds relayMutex and the block is counted in compactBlocks.
void ClearCompactTransactions(CompactBlock& block) {
    for (auto& tx:block.transactions) {
        block.bytes-=tx.size(); compactBytes-=tx.size(); vector<uint8_t>().swap(tx);
    }
    block.missing=block.transactions.size(); block.encodedBytes=CompactEncodedBase(block);
}
void BeginCompactFullFallback(CompactBlock& block) {
    if (!block.fullFallback) compactFullFallbacks++;
    block.fullFallback=true; block.awaitingVerification=false;
    ClearCompactTransactions(block);
    block.attempts=max(block.attempts,3u); block.lastRequest=0;
}
void RememberBlockSource(const array<uint8_t,32>& hash,uint64_t source) {
    if (!source) return;
    lock_guard lock(relayMutex);
    if (!blockSources.count(hash)) {
        if (sourceOrder.size()==512) { blockSources.erase(sourceOrder.front()); sourceOrder.pop_front(); }
        sourceOrder.push_back(hash); blockSources.emplace(hash,source);
    }
}
void CompleteBlockReceive(const array<uint8_t,32>& hash,bool accepted=true,bool retryInvalidEncoding=false) {
    lock_guard lock(relayMutex); inflightBlocks.erase(hash);
    auto found=compactBlocks.find(hash);
    if (found==compactBlocks.end()) return;
    auto& block=*found->second;
    if (accepted||(block.awaitingVerification&&!retryInvalidEncoding)) {
        compactBytes-=block.bytes; compactBlocks.erase(found);
    } else if (block.awaitingVerification) BeginCompactFullFallback(block);
}
void RememberCompactPeer(CompactBlock& block,const shared_ptr<Peer>& peer) {
    for (const auto& source:block.sources) if (auto known=source.lock();known&&known->key==peer->key) return;
    for (auto& source:block.sources) {
        auto known=source.lock();
        if (!known||known->closePosted.load()) { source=peer; return; }
    }
    if (block.sources.size()<4) block.sources.push_back(peer);
}
vector<uint8_t> CompactRepairRequest(const array<uint8_t,32>& hash,const CompactBlock& block) {
    vector<uint8_t> request(hash.begin(),hash.end());
    if (block.fullFallback) return GenerateMessage(21,request);
    for (size_t i=0;i<block.transactions.size();++i) if (block.transactions[i].empty()) {
        auto offset=request.size(); request.resize(offset+4); WriteU32(request.data()+offset,uint32_t(i));
    }
    return GenerateMessage(40,request);
}
void PruneCompactBlocks() {
    vector<shared_ptr<Peer>> connected;
    { lock_guard lock(peerMutex); for (const auto& [key,peer]:peerPool) if (peer->port&&!peer->closePosted.load()) connected.push_back(peer); }
    vector<pair<shared_ptr<Peer>,vector<uint8_t>>> requests;
    {
        lock_guard lock(relayMutex);
        const auto now=GetSteadyTime();
        for (auto it=compactBlocks.begin();it!=compactBlocks.end();) {
            auto& block=*it->second;
            if (now<block.created||now<block.lastRequest) { ++it; continue; }
            if (block.awaitingVerification) {
                if (now-block.lastRequest<5000000000ULL) { ++it; continue; }
                BeginCompactFullFallback(block);
            }
            if (now-block.lastRequest<uint64_t(compactRepairMs)*1000000) { ++it; continue; }
            if (block.attempts>=6||now-block.created>30000000000ULL) {
                compactBytes-=block.bytes; it=compactBlocks.erase(it); continue;
            }
            if (block.attempts>=3&&!block.fullFallback) BeginCompactFullFallback(block);
            shared_ptr<Peer> source;
            if (block.fullFallback) {
                for (const auto& known:block.sources) {
                    auto candidate=known.lock();
                    if (candidate&&!candidate->closePosted.load()&&!block.requestedSources.count(candidate->key)) { source=std::move(candidate); break; }
                }
                if (!source) for (const auto& candidate:connected) {
                    if (!candidate->closePosted.load()&&!block.requestedSources.count(candidate->key)) { source=candidate; RememberCompactPeer(block,source); break; }
                }
            }
            for (size_t i=0;!source&&i<block.sources.size();++i) {
                auto candidate=block.sources[(block.attempts+i)%block.sources.size()].lock();
                if (candidate&&!candidate->closePosted.load()) { source=std::move(candidate); break; }
            }
            if (!source&&!connected.empty()) {
                source=connected[block.attempts%connected.size()]; RememberCompactPeer(block,source);
                if (!block.fullFallback) BeginCompactFullFallback(block);
            }
            block.lastRequest=now; block.attempts++;
            if (source) {
                block.source=source; block.sourceId=PeerId(source); block.requestedSources.insert(source->key);
                requests.emplace_back(source,CompactRepairRequest(it->first,block)); compactRepairRetries++;
            }
            ++it;
        }
        for (auto it=inflightBlocks.begin();it!=inflightBlocks.end();) {
            if (now>=it->second&&now-it->second>5000000000ULL) it=inflightBlocks.erase(it); else ++it;
        }
    }
    for (auto& [peer,request]:requests) SendData(peer,std::move(request));
}
awaitable<void> MaintainCompactBlocks() {
    steady_timer timer(co_await this_coro::executor);
    while (nodeRunning) {
        timer.expires_after(milliseconds(min(50u,compactRepairMs)));
        co_await timer.async_wait(use_awaitable);
        if (nodeRunning) PruneCompactBlocks();
    }
}
shared_ptr<const vector<uint8_t>> ReadRelayBlock(const array<uint8_t,32>& hash) {
    { lock_guard lock(relayMutex); auto found=relayBlocks.find(hash); if (found!=relayBlocks.end()) return found->second; }
    vector<uint8_t> data;
    { shared_lock lock(dbCommitMutex); data=FindForkBlock(hash); }
    if (data.empty()) {
        lock_guard lock(blockBufferLock);
        for (const auto& slot:BlockBuffer) if (!slot.second.empty()&&slot.first.hash==hash) { data=slot.second; break; }
    }
    return data.empty()?nullptr:make_shared<const vector<uint8_t>>(std::move(data));
}
vector<chain::compact::PrefilledTransaction> SelectCompactPrefilled(const shared_ptr<Peer>& peer,
        const vector<uint8_t>& block,const EvmPayloads& payloads,const vector<array<uint8_t,32>>& hashes,
        const chain::compact::Decoded& compact) {
    vector<chain::compact::PrefilledTransaction> result;
    if (!compactPeerKnowsTransaction) return result;
    const auto limit=min(compactPrefillMax,chain::compact::kMaxPrefilledTransactions);
    const auto byteLimit=min(compactPrefillBytes,chain::compact::kMaxPrefilledBytes);
    unordered_map<uint64_t,size_t> idCounts;
    for (const auto id:compact.ids) ++idCounts[id];
    size_t bytes=0;
    // Later transactions in each sender's nonce sequence are the most likely
    // to have arrived recently. Skip every hash already reported by this peer.
    for (size_t i=hashes.size();i>0&&result.size()<limit;--i) {
        const auto position=i-1;
        if (idCounts[compact.ids[position]]!=1||compactPeerKnowsTransaction(peer,hashes[position])) continue;
        const auto payload=payloads.find(position);
        const auto size=176+(payload==payloads.end()?size_t(0):payload->second.size());
        if (size>byteLimit-bytes) continue;
        vector<uint8_t> tx(block.begin()+112+position*176,block.begin()+112+(position+1)*176);
        if (payload!=payloads.end()) tx.insert(tx.end(),payload->second.begin(),payload->second.end());
        result.push_back({uint32_t(position),std::move(tx)}); bytes+=size;
    }
    reverse(result.begin(),result.end()); return result;
}
void AnnounceBlock(const vector<uint8_t>& data) {
    if (data.size()<112) return;
    vector<shared_ptr<Peer>> peers;
    { lock_guard lock(peerMutex); for (const auto& [key,peer]:peerPool) peers.push_back(peer); }
    if (peers.empty()) return;
    EvmPayloads payloads;
    optional<chain::vdf_consensus::Extension> extension;
    if (!ReadBlockPayloads(data,&payloads,&extension)) return;
    auto num=ReadU64(data.data()+72);
    array<uint8_t,32> hash; memcpy(hash.data(),data.data()+80,32);
    uint64_t source=0;
    {
        lock_guard lock(relayMutex);
        if (announcedBlocks.count(hash)) { relaySuppressed++; return; }
        if (announceOrder.size()==512) { announcedBlocks.erase(announceOrder.front()); announceOrder.pop_front(); }
        announceOrder.push_back(hash); announcedBlocks.emplace(hash,GetSteadyTime());
        if (auto found=blockSources.find(hash);found!=blockSources.end()) source=found->second;
        if (!relayBlocks.count(hash)) {
            while (!relayOrder.empty()&&(relayBlocks.size()>=32||relayBytes+data.size()>maxRelayBytes)) {
                auto oldest=relayBlocks.find(relayOrder.front());
                if (oldest!=relayBlocks.end()) { relayBytes-=oldest->second->size(); relayBlocks.erase(oldest); }
                relayOrder.pop_front();
            }
            relayOrder.push_back(hash); relayBytes+=data.size();
            relayBlocks.emplace(hash,make_shared<const vector<uint8_t>>(data));
        }
        for (size_t i=0;i<num;i++) {
            array<uint8_t,32> txHash; memcpy(txHash.data(),data.data()+112+i*176+80,32);
            if (recentBlockTransactions.count(txHash)) continue;
            auto input=payloads.find(i); auto size=size_t(176)+(input==payloads.end()?0:input->second.size());
            if (size>maxRelayTransactionBytes) continue;
            while (!relayTransactionOrder.empty()&&(recentBlockTransactions.size()>=maxRecentBlockTransactions||relayTransactionBytes+size>maxRelayTransactionBytes)) {
                auto found=recentBlockTransactions.find(relayTransactionOrder.front());relayTransactionBytes-=found->second.size();
                recentBlockTransactions.erase(found);relayTransactionOrder.pop_front();
            }
            vector<uint8_t> tx(data.begin()+112+i*176,data.begin()+112+(i+1)*176);
            if (input!=payloads.end()) tx.insert(tx.end(),input->second.begin(),input->second.end());
            relayTransactionBytes+=size; recentBlockTransactions.emplace(txHash,std::move(tx)); relayTransactionOrder.push_back(txHash);
        }
    }
    vector<uint8_t> compact(112+num*33); memcpy(compact.data(),data.data(),112);
    for (size_t i=0;i<num;i++) {
        memcpy(compact.data()+112+i*33,data.data()+112+i*176+80,32);
        compact[112+i*33+32]=payloads.count(i)?1:0;
    }
    if (extension) {
        const auto encoded=chain::vdf_consensus::Encode(*extension);
        compact.insert(compact.end(),encoded.begin(),encoded.end());
    }
    auto summary=make_shared<const vector<uint8_t>>(GenerateMessage(39,compact));
    shared_ptr<const vector<uint8_t>> full,shortSummary;
    vector<uint8_t> shortPayload;
    vector<array<uint8_t,32>> shortHashes;
    chain::compact::Decoded shortDecoded;
    peers=SelectBroadcastPeers(source,hash,blockRelayPeers);
    peers=PrioritizeVdfBlockPeers(data,std::move(peers),source);
    unordered_set<uint64_t> sent;
    for (const auto& peer:peers) {
        if (source&&PeerId(peer)==source) { relaySuppressed++; continue; }
        if (!sent.insert(PeerId(peer)).second) { relaySuppressed++; continue; }
        if (shortIds&&peer->shortCompact.load()) {
            if (!shortSummary) {
                shortHashes.resize(num); vector<uint8_t> contracts(num);
                for (size_t i=0;i<num;++i) {
                    memcpy(shortHashes[i].data(),data.data()+112+i*176+80,32); contracts[i]=payloads.count(i)?1:0;
                }
                uint64_t nonce=0; randombytes_buf(&nonce,sizeof(nonce));
                shortPayload=chain::compact::Encode(span<const uint8_t>(data).first(112),shortHashes,contracts,nonce);
                shortPayload.insert(shortPayload.end(),compact.begin()+112+num*33,compact.end());
                chain::compact::Decode(shortPayload,maxProtocolTx,chain::vdf_consensus::kMaxEncodedBytes,shortDecoded);
                shortSummary=make_shared<const vector<uint8_t>>(GenerateMessage(51,shortPayload)); compactShortAnnouncements++;
            }
            if (compactPrefill&&peer->prefillCompact.load()) {
                auto entries=SelectCompactPrefilled(peer,data,payloads,shortHashes,shortDecoded);
                if (!entries.empty()) {
                    auto message=make_shared<const vector<uint8_t>>(GenerateMessage(52,chain::compact::EncodePrefilled(shortPayload,entries)));
                    if (TrySendGossip(peer,message)) { compactPrefilledAnnouncements++; compactPrefilledTransactions+=entries.size(); RecordVdfBlockRecipient(data,peer); }
                    continue;
                }
            }
            if (TrySendGossip(peer,shortSummary)) RecordVdfBlockRecipient(data,peer);
        }
        else if (peer->compact) {if (TrySendGossip(peer,summary)) RecordVdfBlockRecipient(data,peer);}
        else {
            if (!full) full=make_shared<const vector<uint8_t>>(GenerateNewBlockMessage(data));
            if (TrySendGossip(peer,full)) RecordVdfBlockRecipient(data,peer);
        }
    }
    compactAnnouncements++;
}
bool CompactRootMatches(const CompactBlock& block) {
    auto root=MerkleCompute(block.hashes);
    if (!block.vdf.empty()) {
        chain::vdf_consensus::Extension extension;
        if (!chain::vdf_consensus::Parse(block.vdf,extension)) return false;
        root=chain::vdf_consensus::MerkleRoot(root,extension);
    }
    return !memcmp(root.data(),block.header.data()+32,32);
}
void RepairCompactRoot(const array<uint8_t,32>& hash,const shared_ptr<CompactBlock>& block) {
    vector<uint8_t> request;
    shared_ptr<Peer> peer;
    {
        lock_guard lock(relayMutex);
        auto found=compactBlocks.find(hash);
        if (found==compactBlocks.end()||found->second!=block||!block->awaitingVerification) return;
        compactRootFailures++; inflightBlocks.erase(hash);
        peer=block->source.lock();
        ClearCompactTransactions(*block); block->awaitingVerification=false;
        if (block->shortEncoding&&!block->repairedAll&&block->missing) {
            // A unique local short-ID match may still be a collision. Fetch all
            // positions once and check the committed Merkle root again.
            block->repairedAll=true;
            fill(block->hashes.begin(),block->hashes.end(),array<uint8_t,32>{});
        } else BeginCompactFullFallback(*block);
        block->lastRequest=peer&&!peer->closePosted.load()?GetSteadyTime():0;
        block->attempts=block->fullFallback?3:1;
        if (peer&&!peer->closePosted.load()) { block->requestedSources.insert(peer->key); request=CompactRepairRequest(hash,*block); }
    }
    if (!request.empty()) { compactRepairRetries++; SendData(peer,std::move(request)); }
}
void SubmitCompactBlock(const array<uint8_t,32>& hash,const shared_ptr<CompactBlock>& block) {
    vector<uint8_t> data;
    CompactBlock root;
    shared_ptr<Peer> peer;
    {
        lock_guard lock(relayMutex);
        auto found=compactBlocks.find(hash);
        if (found==compactBlocks.end()||found->second!=block||!block->awaitingVerification) return;
        if (block->encodedBytes>maxBlockBytes) { BeginCompactFullFallback(*block); inflightBlocks.erase(hash); return; }
        // Copy at most one encoded block while holding the mutex. Merkle
        // hashing and signature/state verification run on this stable copy.
        root.header=block->header; root.hashes=block->hashes; root.vdf=block->vdf;
        data.resize(112+block->hashes.size()*176); memcpy(data.data(),block->header.data(),112);
        EvmPayloads inputs;
        for (size_t i=0;i<block->transactions.size();i++) {
            const auto& tx=block->transactions[i]; memcpy(data.data()+112+i*176,tx.data(),176);
            if (block->contracts[i]) inputs.emplace(i,vector<uint8_t>(tx.begin()+176,tx.end()));
        }
        AppendEvmPayloads(data,inputs); data.insert(data.end(),block->vdf.begin(),block->vdf.end());
        if (data.size()!=block->encodedBytes||data.size()>maxBlockBytes) {
            BeginCompactFullFallback(*block); inflightBlocks.erase(hash); return;
        }
        peer=block->source.lock();
        if (!peer||peer->closePosted.load()) for (const auto& source:block->sources) {
            auto candidate=source.lock();
            if (candidate&&!candidate->closePosted.load()) { peer=std::move(candidate); break; }
        }
        if (!peer||peer->closePosted.load()) { BeginCompactFullFallback(*block); inflightBlocks.erase(hash); return; }
    }
    if (!CompactRootMatches(root)) { RepairCompactRoot(hash,block); return; }
    compactReconstructed++;
    co_spawn(peer->strand,ProcessMessage(peer,2,std::move(data)),[peer](exception_ptr error){if (error) PeerError(peer,error);});
}
void ReceiveCompactBlockEncoding(const shared_ptr<Peer>& peer,const vector<uint8_t>& data,bool shortEncoding,
        const vector<chain::compact::PrefilledTransaction>& prefilled={}) {
    if (!peer->compact||data.size()<112) return;
    auto num=ReadU64(data.data()+72),height=ReadU64(data.data()+64);
    if (num>maxProtocolTx||height<2) return;
    chain::compact::Decoded decoded;
    if (shortEncoding&&!chain::compact::Decode(data,maxProtocolTx,chain::vdf_consensus::kMaxEncodedBytes,decoded)) return;
    const size_t transactionEnd=shortEncoding?decoded.transactionEnd:112+num*33;
    if (data.size()<transactionEnd||data.size()-transactionEnd>chain::vdf_consensus::kMaxEncodedBytes) return;
    optional<chain::vdf_consensus::Extension> extension;
    if (data.size()>transactionEnd) {
        chain::vdf_consensus::Extension decoded;
        if (!chain::vdf_consensus::Parse(span<const uint8_t>(data).subspan(transactionEnd),decoded)) return;
        extension=std::move(decoded);
    }
    if (chain::vdf_consensus::config.enabled!=extension.has_value()||(!num&&!extension)) return;
    if (extension) {
        try {
            if (extension->slot<chain::vdf_consensus::MinimumSlot(height)) return;
            const auto now=chain::vdf_consensus::CurrentSlot();
            if (extension->slot>now&&extension->slot-now>chain::vdf_consensus::config.futureSlots) return;
        } catch (const invalid_argument&) { return; }
    }
    array<uint8_t,32> hash,calculated; memcpy(hash.data(),data.data()+80,32);
    crypto_generichash(calculated.data(),32,data.data(),80,nullptr,0);
    if (hash!=calculated) return;
    if (extension&&!chain::vdf_consensus::VerifySignature(*extension,hash)) return;
    auto current=ReadChainHead().height;
    if ((height>current&&height-current>256)||(current>height&&current-height>256)) return;
    if (KnowsForkBlock(hash)) return;
    { lock_guard lock(blockBufferLock); for (const auto& slot:BlockBuffer) if (!slot.second.empty()&&slot.first.hash==hash) { relaySuppressed++; return; } }
    auto block=make_shared<CompactBlock>(); block->sources.push_back(peer);
    CompactBuildReservation building{hash,false};
    {
        lock_guard lock(relayMutex);
        auto existing=compactBlocks.find(hash);
        if (existing!=compactBlocks.end()) { RememberCompactPeer(*existing->second,peer); relaySuppressed++; return; }
        auto preparing=compactBuilding.find(hash);
        if (preparing!=compactBuilding.end()) { RememberCompactPeer(*preparing->second,peer); relaySuppressed++; return; }
        if (inflightBlocks.count(hash)||compactBlocks.size()+compactBuilding.size()>=maxCompactBlocks) { relaySuppressed++; return; }
        compactBuilding.emplace(hash,block); building.active=true;
    }
    block->header.assign(data.begin(),data.begin()+112);
    block->vdf.assign(data.begin()+transactionEnd,data.end());
    block->hashes.resize(num); block->contracts.resize(num); block->transactions.resize(num);
    block->source=peer; block->sourceId=PeerId(peer); block->created=block->lastRequest=GetSteadyTime();
    block->requestedSources.insert(peer->key); block->attempts=1;
    block->shortEncoding=shortEncoding;
    block->bytes=data.size()+num*(sizeof(vector<uint8_t>)+sizeof(array<uint8_t,32>)+1+(shortEncoding?8:0));
    if (shortEncoding) {
        block->shortIds=std::move(decoded.ids); block->shortKey=decoded.key; block->contracts=std::move(decoded.contracts);
    } else {
        for (size_t i=0;i<num;i++) {
            memcpy(block->hashes[i].data(),data.data()+112+i*33,32); block->contracts[i]=data[112+i*33+32];
            if (block->contracts[i]>1) return;
        }
        if (!CompactRootMatches(*block)) return;
    }
    block->encodedBytes=CompactEncodedBase(*block);
    const size_t minimumInputs=size_t(count(block->contracts.begin(),block->contracts.end(),uint8_t(1)))*45;
    if (block->encodedBytes>maxBlockBytes||minimumInputs>maxBlockBytes-block->encodedBytes) return;
    for (const auto& entry:prefilled) {
        const auto extra=entry.transaction.size()-176;
        if (extra>maxBlockBytes-block->encodedBytes||entry.transaction.size()>maxCompactBytes-block->bytes) return;
        block->transactions[entry.index]=entry.transaction;
        memcpy(block->hashes[entry.index].data(),entry.transaction.data()+80,32);
        block->bytes+=entry.transaction.size(); block->encodedBytes+=extra;
    }
    if (!prefilled.empty()&&prefilled.size()==num&&!CompactRootMatches(*block)) return;
    vector<uint8_t> request(hash.begin(),hash.end());
    chain::compact::ShortIdIndex index(block->shortIds);
    if (shortEncoding&&prefilled.size()!=num) {
        if (auto snapshot=ReadCompactHashSnapshot()) {
            // Hashing the pool snapshot never holds the transaction or relay
            // mutex. A transaction removed before the lookup is fetched by index.
            for (const auto& txHash:snapshot->hashes) {
                auto position=index.Find(chain::compact::ShortId(block->shortKey,txHash));
                if (position) index.Observe(*position,txHash);
            }
            for (size_t i=0;i<num;++i)
                if (block->transactions[i].empty()&&index.MatchedHash(i)) block->hashes[i]=*index.MatchedHash(i);
        }
    }
    {
        lock_guard lock(txpoolMutex);
        for (size_t i=0;i<num;i++) {
            if (!block->transactions[i].empty()) continue;
            if (shortEncoding&&!index.MatchedHash(i)) continue;
            auto tx=txpool.find(block->hashes[i]); auto input=evmPool.find(block->hashes[i]);
            if (tx!=txpool.end()&&bool(input!=evmPool.end())==bool(block->contracts[i])) {
                const auto extra=input==evmPool.end()?0:input->second.size();
                if (extra>maxBlockBytes-block->encodedBytes) continue;
                const auto size=size_t(176)+extra;
                if (size>maxCompactBytes-block->bytes) return;
                auto& encoded=block->transactions[i]; encoded.assign(tx->second.begin(),tx->second.end());
                if (input!=evmPool.end()) encoded.insert(encoded.end(),input->second.begin(),input->second.end());
                block->bytes+=encoded.size(); block->encodedBytes+=extra;
            }
        }
    }
    size_t missing=0;
    {
        lock_guard lock(relayMutex);
        for (size_t i=0;i<num;i++) if (block->transactions[i].empty()) {
            auto known=shortEncoding&&!index.MatchedHash(i)?recentBlockTransactions.end():recentBlockTransactions.find(block->hashes[i]);
            if (known!=recentBlockTransactions.end()&&bool(known->second.size()>176)==bool(block->contracts[i])&&
                    known->second.size()-176<=maxBlockBytes-block->encodedBytes) {
                if (known->second.size()>maxCompactBytes-block->bytes) return;
                block->transactions[i]=known->second; block->bytes+=known->second.size();
                block->encodedBytes+=known->second.size()-176; recentTransactionHits++;
            } else {
                auto offset=request.size(); request.resize(offset+4); WriteU32(request.data()+offset,uint32_t(i)); block->missing++;
            }
        }
        auto existing=compactBlocks.find(hash);
        if (existing!=compactBlocks.end()) { RememberCompactPeer(*existing->second,peer); return; }
        if (inflightBlocks.count(hash)||compactBlocks.size()>=maxCompactBlocks||block->bytes>maxCompactBytes-compactBytes) return;
        compactBytes+=block->bytes; compactBlocks.emplace(hash,block);
        block->lastRequest=GetSteadyTime();
        if (!block->missing) { block->awaitingVerification=true; inflightBlocks[hash]=block->lastRequest; }
        missing=block->missing;
    }
    if (!missing) SubmitCompactBlock(hash,block);
    else { missingTransactions+=missing; SendData(peer,GenerateMessage(40,request)); }
}
void ReceiveCompactBlock(const shared_ptr<Peer>& peer,const vector<uint8_t>& data) { ReceiveCompactBlockEncoding(peer,data,false); }
void ReceiveShortCompactBlock(const shared_ptr<Peer>& peer,const vector<uint8_t>& data) { ReceiveCompactBlockEncoding(peer,data,true); }
void ReceivePrefilledCompactBlock(const shared_ptr<Peer>& peer,const vector<uint8_t>& data) {
    // The sender may already have our capability reply while its reply is
    // still in flight. Remote capability flags gate sending, not decoding.
    if (!shortIds||!compactPrefill||!peer||!peer->compact||!peer->port||peer->closePosted.load()) return;
    chain::compact::PrefilledDecoded decoded;
    if (!chain::compact::DecodePrefilled(data,maxProtocolTx,chain::vdf_consensus::kMaxEncodedBytes,
            221+maxEvmInput,decoded)) return;
    // Check every transaction before touching compactBlocks, source lists or
    // recent transactions. A valid prefix cannot make a malformed frame stick.
    for (const auto& entry:decoded.transactions) {
        array<uint8_t,176> tx; memcpy(tx.data(),entry.transaction.data(),tx.size());
        if (!VerifyTransaction(tx,span<const uint8_t>(entry.transaction).subspan(176))) return;
    }
    vector<uint8_t> compact(decoded.compact.begin(),decoded.compact.end());
    ReceiveCompactBlockEncoding(peer,compact,true,decoded.transactions);
    compactPrefilledReceived+=decoded.transactions.size();
}
awaitable<void> SendBounded(const shared_ptr<Peer>& peer,vector<uint8_t> data) {
    if (data.size()>maxSendBytes) co_return;
    steady_timer timer(peer->strand);
    shared_ptr<SendReservation> reservation;
    while (!peer->closed&&nodeRunning) {
        reservation=sendQueueBudget.TryReserve(peer->sendBudget,data.size(),maxSendBytes,false);
        if (reservation) break;
        timer.expires_after(milliseconds(1)); co_await timer.async_wait(use_awaitable);
    }
    if (!peer->closed&&reservation) QueueData(peer,PendingSend{
        make_shared<const vector<uint8_t>>(std::move(data)),std::move(reservation)});
}
vector<vector<uint8_t>> ReadMissingTransactions(const vector<uint8_t>& request) {
    vector<vector<uint8_t>> replies;
    if (request.size()<=32||(request.size()-32)%4) return replies;
    array<uint8_t,32> hash; memcpy(hash.data(),request.data(),32);
    auto data=ReadRelayBlock(hash); if (!data) return replies;
    auto num=ReadU64(data->data()+72); EvmPayloads inputs; if (!ReadEvmPayloads(*data,inputs)) return replies;
    vector<uint8_t> response(hash.begin(),hash.end());
    unordered_set<uint32_t> indices;
    for (size_t offset=32;offset<request.size();offset+=4) {
        auto index=ReadU32(request.data()+offset); if (index>=num) return {};
        if (!indices.insert(index).second) continue;
        auto input=inputs.find(index); auto length=size_t(176)+(input==inputs.end()?0:input->second.size());
        const size_t responseLimit=min(maxMessageSize,maxSendBytes>5?maxSendBytes-5:0);
        if (32+8+length>responseLimit) return {};
        if (response.size()+8+length>responseLimit) {
            replies.push_back(GenerateMessage(41,response)); response.assign(hash.begin(),hash.end());
        }
        auto at=response.size(); response.resize(at+8+length);
        WriteU32(response.data()+at,index); WriteU32(response.data()+at+4,uint32_t(length));
        memcpy(response.data()+at+8,data->data()+112+index*176,176);
        if (input!=inputs.end()) memcpy(response.data()+at+8+176,input->second.data(),input->second.size());
    }
    if (response.size()>32) replies.push_back(GenerateMessage(41,response));
    return replies;
}
void ReceiveMissingTransactions(const shared_ptr<Peer>& peer,const vector<uint8_t>& data) {
    if (data.size()<32||data.size()>maxMessageSize) return;
    array<uint8_t,32> hash; memcpy(hash.data(),data.data(),32);
    shared_ptr<CompactBlock> block;
    {
        lock_guard lock(relayMutex);
        auto found=compactBlocks.find(hash); if (found==compactBlocks.end()||!found->second->requestedSources.count(peer->key)) return;
        if (found->second->awaitingVerification||found->second->fullFallback) return;
        block=found->second; size_t offset=32;
        struct Entry { uint32_t index,length; size_t offset; array<uint8_t,32> hash; };
        vector<Entry> entries; unordered_set<uint32_t> positions;
        size_t newBytes=0,extraBytes=0;
        while (offset<data.size()) {
            if (data.size()-offset<8) return;
            auto index=ReadU32(data.data()+offset),length=ReadU32(data.data()+offset+4); offset+=8;
            if (index>=block->hashes.size()||length>data.size()-offset||length<176||
                (block->contracts[index]?(length<221||length>221+maxEvmInput):length!=176)) return;
            if (!positions.insert(index).second) return;
            array<uint8_t,32> txHash; memcpy(txHash.data(),data.data()+offset+80,32);
            if (block->shortEncoding) {
                if (chain::compact::ShortId(block->shortKey,txHash)!=block->shortIds[index]) return;
            } else if (txHash!=block->hashes[index]) return;
            if (block->transactions[index].empty()) { newBytes+=length; extraBytes+=length-176; entries.push_back({index,length,offset,txHash}); }
            offset+=length;
        }
        // Validate the complete reply before changing the pending block. A bad
        // trailing record must not leave missing==0 without verification.
        if (extraBytes>maxBlockBytes-block->encodedBytes) {
            BeginCompactFullFallback(*block); inflightBlocks.erase(hash); return;
        }
        if (newBytes>maxCompactBytes-compactBytes) return;
        for (const auto& entry:entries) {
            block->transactions[entry.index].assign(data.begin()+entry.offset,data.begin()+entry.offset+entry.length);
            block->hashes[entry.index]=entry.hash; block->missing--;
        }
        block->bytes+=newBytes; compactBytes+=newBytes; block->encodedBytes+=extraBytes;
        if (block->missing) return;
        block->source=peer; block->sourceId=PeerId(peer);
        block->awaitingVerification=true; block->lastRequest=GetSteadyTime(); inflightBlocks[hash]=block->lastRequest;
    }
    SubmitCompactBlock(hash,block);
}
vector<vector<uint8_t>> ReadBlockReplies(uint8_t type,const vector<uint8_t>& request) {
    if (type==40) return ReadMissingTransactions(request);
    vector<vector<uint8_t>> replies;
    if (type==6) {
        shared_lock lock(dbCommitMutex); auto data=GenerateBlockMessage(ReadU64(request.data()));
        if (!data.empty()) replies.push_back(std::move(data));
        return replies;
    }
    if (type==21) {
        array<uint8_t,32> hash; memcpy(hash.data(),request.data(),32); auto data=ReadRelayBlock(hash);
        if (data) replies.push_back(GenerateMessage(22,*data));
        return replies;
    }
    auto start=ReadU64(request.data()); auto count=unsigned(request[8])|(unsigned(request[9])<<8);
    if (type!=42||!start||!count||count>4||start>UINT64_MAX-count) return replies;
    vector<vector<uint8_t>> blocks;
    {
        shared_lock lock(dbCommitMutex);
        for (unsigned i=0;i<count;i++) {
            auto block=DBReadBlockByHeight(to_string(start+i)); if (block.empty()) break;
            blocks.push_back(std::move(block));
        }
    }
    vector<uint8_t> response(2,0); unsigned packed=0;
    for (const auto& block:blocks) {
        if (response.size()+4+block.size()>maxMessageSize&&packed) {
            response[0]=uint8_t(packed); replies.push_back(GenerateMessage(43,response)); response.assign(2,0); packed=0;
        }
        if (block.size()+6>maxMessageSize) { replies.push_back(GenerateMessage(7,block)); continue; }
        auto at=response.size(); response.resize(at+4+block.size()); WriteU32(response.data()+at,uint32_t(block.size()));
        memcpy(response.data()+at+4,block.data(),block.size()); packed++;
    }
    if (packed) { response[0]=uint8_t(packed); replies.push_back(GenerateMessage(43,response)); }
    return replies;
}
struct BlockReplyReservation {
    bool active=false;
    ~BlockReplyReservation() { if (active) pendingBlockReplies--; }
};
awaitable<vector<vector<uint8_t>>> ReadBlockAsync(shared_ptr<Peer> peer,uint8_t type,vector<uint8_t> request,
        shared_ptr<BlockReplyReservation> reservation,uint64_t queued) {
    return async_initiate<decltype(use_awaitable),void(exception_ptr,vector<vector<uint8_t>>)>(
        [peer,type,queued,request=std::move(request),reservation](auto handler) mutable {
            auto completion=make_shared<decltype(handler)>(std::move(handler));
            post(*queryThreads,[peer,type,queued,request=std::move(request),reservation,completion]() mutable {
                exception_ptr error; vector<vector<uint8_t>> responses;
                auto start=GetSteadyTime(); blockReplyWaitNs+=start-queued; blockReplyTasks++;
                try { responses=ReadBlockReplies(type,request); } catch (...) { error=current_exception(); }
                blockReplyReadNs+=GetSteadyTime()-start;
                post(peer->strand,[completion,error,responses=std::move(responses)]() mutable { (*completion)(error,std::move(responses)); });
            });
        },use_awaitable);
}
awaitable<void> SendBlockReply(shared_ptr<Peer> peer,uint8_t type,vector<uint8_t> request) {
    auto reservation=make_shared<BlockReplyReservation>();
    steady_timer timer(co_await this_coro::executor); auto queued=GetSteadyTime();
    auto pending=pendingBlockReplies.load();
    while (true) {
        if (pending<8&&pendingBlockReplies.compare_exchange_weak(pending,pending+1)) { reservation->active=true; break; }
        if (peer->closed||!nodeRunning) co_return;
        timer.expires_after(milliseconds(1)); co_await timer.async_wait(use_awaitable); pending=pendingBlockReplies.load();
    }
    auto replies=co_await ReadBlockAsync(peer,type,std::move(request),reservation,queued);
    for (auto& reply:replies) { if (peer->closed) break; co_await SendBounded(peer,std::move(reply)); }
}
bool ReadBlockBatch(const vector<uint8_t>& data,vector<vector<uint8_t>>& blocks) {
    if (data.size()<2||data[1]||!data[0]||data[0]>4) return false;
    size_t offset=2;
    for (unsigned i=0;i<data[0];i++) {
        if (data.size()-offset<4) return false;
        auto size=ReadU32(data.data()+offset); offset+=4;
        if (size<112||size>data.size()-offset) return false;
        blocks.emplace_back(data.begin()+offset,data.begin()+offset+size); offset+=size;
        if (i&&(ReadU64(blocks[i].data()+64)!=ReadU64(blocks[i-1].data()+64)+1||memcmp(blocks[i].data(),blocks[i-1].data()+80,32))) return false;
    }
    return offset==data.size();
}
#endif
