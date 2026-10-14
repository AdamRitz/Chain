#ifndef CHAIN_NETWORK_BLOCKS_H
#define CHAIN_NETWORK_BLOCKS_H
// 摘要保留区块头、交易哈希和输入类型，缺少的完整交易按索引补传。
struct CompactBlock {
    vector<uint8_t> header;
    vector<array<uint8_t,32>> hashes;
    vector<uint8_t> contracts;
    vector<vector<uint8_t>> transactions;
    weak_ptr<Peer> source;
    uint64_t sourceId=0,created=0;
    size_t missing=0,bytes=0;
};
mutex relayMutex;
unordered_map<array<uint8_t,32>,shared_ptr<CompactBlock>,GetMapHash> compactBlocks;
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

uint64_t PeerId(const shared_ptr<Peer>& peer) { return peer->port?GetPeerKey(peer->address,peer->port):0; }
void RememberBlockSource(const array<uint8_t,32>& hash,uint64_t source) {
    if (!source) return;
    lock_guard lock(relayMutex);
    if (!blockSources.count(hash)) {
        if (sourceOrder.size()==512) { blockSources.erase(sourceOrder.front()); sourceOrder.pop_front(); }
        sourceOrder.push_back(hash); blockSources.emplace(hash,source);
    }
}
void CompleteBlockReceive(const array<uint8_t,32>& hash) {
    lock_guard lock(relayMutex); inflightBlocks.erase(hash);
}
void PruneCompactBlocks() {
    auto now=GetSteadyTime();
    lock_guard lock(relayMutex);
    for (auto it=compactBlocks.begin();it!=compactBlocks.end();) {
        if (now-it->second->created>5000000000ULL||it->second->source.expired()) {
            compactBytes-=it->second->bytes; it=compactBlocks.erase(it);
        } else ++it;
    }
    for (auto it=inflightBlocks.begin();it!=inflightBlocks.end();) {
        if (now-it->second>5000000000ULL) it=inflightBlocks.erase(it); else ++it;
    }
}
shared_ptr<const vector<uint8_t>> ReadRelayBlock(const array<uint8_t,32>& hash) {
    { lock_guard lock(relayMutex); auto found=relayBlocks.find(hash); if (found!=relayBlocks.end()) return found->second; }
    auto data=FindForkBlock(hash);
    if (data.empty()) {
        lock_guard lock(blockBufferLock);
        for (const auto& slot:BlockBuffer) if (!slot.second.empty()&&slot.first.hash==hash) { data=slot.second; break; }
    }
    return data.empty()?nullptr:make_shared<const vector<uint8_t>>(std::move(data));
}
void AnnounceBlock(const vector<uint8_t>& data) {
    if (data.size()<112) return;
    vector<shared_ptr<Peer>> peers;
    { lock_guard lock(peerMutex); for (const auto& [key,peer]:peerPool) peers.push_back(peer); }
    if (peers.empty()) return;
    EvmPayloads payloads; if (!ReadEvmPayloads(data,payloads)) return;
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
    auto summary=make_shared<const vector<uint8_t>>(GenerateMessage(39,compact));
    shared_ptr<const vector<uint8_t>> full;
    unordered_set<uint64_t> sent;
    for (const auto& peer:peers) {
        if (source&&PeerId(peer)==source) { relaySuppressed++; continue; }
        if (!sent.insert(PeerId(peer)).second) { relaySuppressed++; continue; }
        if (peer->compact) SendData(peer,summary);
        else {
            if (!full) full=make_shared<const vector<uint8_t>>(GenerateNewBlockMessage(data));
            SendData(peer,full);
        }
    }
    compactAnnouncements++;
}
void SubmitCompactBlock(const array<uint8_t,32>& hash,const shared_ptr<CompactBlock>& block) {
    vector<uint8_t> data(112+block->hashes.size()*176); memcpy(data.data(),block->header.data(),112);
    EvmPayloads inputs;
    for (size_t i=0;i<block->transactions.size();i++) {
        const auto& tx=block->transactions[i]; memcpy(data.data()+112+i*176,tx.data(),176);
        if (block->contracts[i]) inputs.emplace(i,vector<uint8_t>(tx.begin()+176,tx.end()));
    }
    AppendEvmPayloads(data,inputs);
    if (data.size()>maxBlockBytes) { CompleteBlockReceive(hash); return; }
    auto peer=block->source.lock();
    if (!peer) { CompleteBlockReceive(hash); return; }
    compactReconstructed++;
    co_spawn(peer->strand,ProcessMessage(peer,2,std::move(data)),[peer](exception_ptr error){if (error) PeerError(peer,error);});
}
void ReceiveCompactBlock(const shared_ptr<Peer>& peer,const vector<uint8_t>& data) {
    if (!peer->compact||data.size()<112) return;
    auto num=ReadU64(data.data()+72),height=ReadU64(data.data()+64);
    if (!num||num>maxProtocolTx||data.size()!=112+num*33||height<2) return;
    array<uint8_t,32> hash,calculated; memcpy(hash.data(),data.data()+80,32);
    crypto_generichash(calculated.data(),32,data.data(),80,nullptr,0);
    if (hash!=calculated) return;
    { shared_lock lock(dbCommitMutex);
      auto current=DBReadBlockHeight();
      if ((height>current&&height-current>256)||(current>height&&current-height>256)||!DBReadBlockHeader(hash).empty()) return; }
    if (KnowsForkBlock(hash)) return;
    { lock_guard lock(blockBufferLock); for (const auto& slot:BlockBuffer) if (!slot.second.empty()&&slot.first.hash==hash) { relaySuppressed++; return; } }
    {
        lock_guard lock(relayMutex);
        if (compactBlocks.count(hash)||inflightBlocks.count(hash)||compactBlocks.size()>=maxCompactBlocks) { relaySuppressed++; return; }
    }
    auto block=make_shared<CompactBlock>(); block->header.assign(data.begin(),data.begin()+112);
    block->hashes.resize(num); block->contracts.resize(num); block->transactions.resize(num);
    block->source=peer; block->sourceId=PeerId(peer); block->created=GetSteadyTime();
    block->bytes=data.size()+num*sizeof(vector<uint8_t>);
    for (size_t i=0;i<num;i++) {
        memcpy(block->hashes[i].data(),data.data()+112+i*33,32); block->contracts[i]=data[112+i*33+32];
        if (block->contracts[i]>1) return;
    }
    array<uint8_t,32> root; memcpy(root.data(),data.data()+32,32);
    if (MerkleCompute(block->hashes)!=root) return;
    vector<uint8_t> request(hash.begin(),hash.end());
    {
        lock_guard lock(txpoolMutex);
        for (size_t i=0;i<num;i++) {
            auto tx=txpool.find(block->hashes[i]); auto input=evmPool.find(block->hashes[i]);
            if (tx!=txpool.end()&&bool(input!=evmPool.end())==bool(block->contracts[i])) {
                auto& encoded=block->transactions[i]; encoded.assign(tx->second.begin(),tx->second.end());
                if (input!=evmPool.end()) encoded.insert(encoded.end(),input->second.begin(),input->second.end());
                block->bytes+=encoded.size();
            }
        }
    }
    {
        lock_guard lock(relayMutex);
        for (size_t i=0;i<num;i++) if (block->transactions[i].empty()) {
            auto known=recentBlockTransactions.find(block->hashes[i]);
            if (known!=recentBlockTransactions.end()&&bool(known->second.size()>176)==bool(block->contracts[i])) {
                block->transactions[i]=known->second; block->bytes+=known->second.size(); recentTransactionHits++;
            } else {
                auto offset=request.size(); request.resize(offset+4); WriteU32(request.data()+offset,uint32_t(i)); block->missing++;
            }
        }
        if (compactBlocks.count(hash)||inflightBlocks.count(hash)||compactBlocks.size()>=maxCompactBlocks||block->bytes>maxCompactBytes-compactBytes) return;
        if (block->missing) { compactBytes+=block->bytes; compactBlocks.emplace(hash,block); }
        else inflightBlocks[hash]=GetSteadyTime();
    }
    if (!block->missing) SubmitCompactBlock(hash,block);
    else { missingTransactions+=block->missing; SendData(peer,GenerateMessage(40,request)); }
}
awaitable<void> SendBounded(const shared_ptr<Peer>& peer,vector<uint8_t> data) {
    if (data.size()>maxSendBytes) co_return;
    steady_timer timer(peer->strand);
    while (!peer->closed&&peer->queuedBytes>maxSendBytes-data.size()) {
        timer.expires_after(milliseconds(1)); co_await timer.async_wait(use_awaitable);
    }
    if (!peer->closed) QueueData(peer,make_shared<const vector<uint8_t>>(std::move(data)));
}
awaitable<void> SendMissingTransactions(const shared_ptr<Peer>& peer,const vector<uint8_t>& request) {
    array<uint8_t,32> hash; memcpy(hash.data(),request.data(),32);
    auto data=ReadRelayBlock(hash); if (!data) co_return;
    auto num=ReadU64(data->data()+72); EvmPayloads inputs; if (!ReadEvmPayloads(*data,inputs)) co_return;
    vector<uint8_t> response(hash.begin(),hash.end());
    for (size_t offset=32;offset<request.size();offset+=4) {
        auto index=ReadU32(request.data()+offset); if (index>=num) co_return;
        auto input=inputs.find(index); auto length=size_t(176)+(input==inputs.end()?0:input->second.size());
        if (response.size()+8+length>maxMessageSize) {
            co_await SendBounded(peer,GenerateMessage(41,response)); response.assign(hash.begin(),hash.end());
        }
        auto at=response.size(); response.resize(at+8+length);
        WriteU32(response.data()+at,index); WriteU32(response.data()+at+4,uint32_t(length));
        memcpy(response.data()+at+8,data->data()+112+index*176,176);
        if (input!=inputs.end()) memcpy(response.data()+at+8+176,input->second.data(),input->second.size());
    }
    if (response.size()>32) co_await SendBounded(peer,GenerateMessage(41,response));
}
void ReceiveMissingTransactions(const shared_ptr<Peer>& peer,const vector<uint8_t>& data) {
    array<uint8_t,32> hash; memcpy(hash.data(),data.data(),32);
    shared_ptr<CompactBlock> block;
    {
        lock_guard lock(relayMutex);
        auto found=compactBlocks.find(hash); if (found==compactBlocks.end()||found->second->sourceId!=PeerId(peer)) return;
        block=found->second; size_t offset=32;
        while (offset<data.size()) {
            if (data.size()-offset<8) return;
            auto index=ReadU32(data.data()+offset),length=ReadU32(data.data()+offset+4); offset+=8;
            if (index>=block->hashes.size()||length>data.size()-offset||length<176||
                (block->contracts[index]?(length<221||length>221+maxEvmInput):length!=176)) return;
            if (memcmp(data.data()+offset+80,block->hashes[index].data(),32)) return;
            auto& tx=block->transactions[index];
            if (tx.empty()) {
                if (length>maxCompactBytes-compactBytes) return;
                tx.assign(data.begin()+offset,data.begin()+offset+length); block->bytes+=length; compactBytes+=length; block->missing--;
            }
            offset+=length;
        }
        if (block->missing) return;
        compactBytes-=block->bytes; compactBlocks.erase(found); inflightBlocks[hash]=GetSteadyTime();
    }
    SubmitCompactBlock(hash,block);
}
awaitable<void> SendBlockBatch(const shared_ptr<Peer>& peer,const vector<uint8_t>& request) {
    auto start=ReadU64(request.data()); auto count=unsigned(request[8])|(unsigned(request[9])<<8);
    if (!peer->compact||!start||!count||count>4||start>UINT64_MAX-count) co_return;
    vector<uint8_t> response(2,0); unsigned packed=0;
    for (unsigned i=0;i<count;i++) {
        vector<uint8_t> block;
        { shared_lock lock(dbCommitMutex); block=DBReadBlockByHeight(to_string(start+i)); }
        if (block.empty()) break;
        if (response.size()+4+block.size()>maxMessageSize&&packed) {
            response[0]=uint8_t(packed); co_await SendBounded(peer,GenerateMessage(43,response)); response.assign(2,0); packed=0;
        }
        if (block.size()+6>maxMessageSize) { co_await SendBounded(peer,GenerateMessage(7,block)); continue; }
        auto at=response.size(); response.resize(at+4+block.size()); WriteU32(response.data()+at,uint32_t(block.size()));
        memcpy(response.data()+at+4,block.data(),block.size()); packed++;
    }
    if (packed) { response[0]=uint8_t(packed); co_await SendBounded(peer,GenerateMessage(43,response)); }
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
