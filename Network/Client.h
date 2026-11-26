#ifndef CHAIN_CLIENT_H
#define CHAIN_CLIENT_H
#include <utility>
#include <set>
#include <fstream>
#include <boost/asio.hpp>
#include <deque>
#include <thread>
#include <nlohmann/json.hpp>
#include "../Transaction/Block.h"
#include "../Message/Message.h"
#include "../Tool/Resource.h"
#include "Address.h"
#include "SendBudget.h"
#include "SendPriority.h"
#include "Fragment.h"
#include "EarlyRelay.h"
#include "PeerMaintenance.h"
using namespace std;
using namespace std::chrono;
using namespace boost::asio;
using tcp=ip::tcp;

io_context* nodeIO=nullptr;
unique_ptr<thread_pool> verifyThreads;
unique_ptr<thread_pool> candidateThreads;
unique_ptr<thread_pool> queryThreads;
atomic<size_t> pendingQueries{0};
atomic<uint64_t> queryWaitNs{0},ingressWaitNs{0},syncOwner{0},syncOwnerSkips{0};
size_t syncWindow=8;
atomic<size_t> pendingTxVerify{0},pendingBlockVerify{0},pendingTxBytes{0},pendingBlockBytes{0};
atomic<uint64_t> txQueueWaitNs{0},blockQueueWaitNs{0};
atomic<size_t> pendingVerify{0},peakPendingVerify{0},connectionCount{0};
atomic<size_t> pendingVerifyBytes{0};
atomic<size_t> pendingMetadata{0},pendingBlockReplies{0};
atomic<uint64_t> metadataQueueWaitNs{0},metadataTasks{0},blockTasks{0},blockReplyWaitNs{0};
atomic<uint64_t> duplicateBlockWork{0};
atomic<uint64_t> blockReplyReadNs{0},blockReplyTasks{0};
atomic<uint64_t> queueWaitNs{0},networkBytes{0};
size_t maxPendingVerify=32768;
size_t maxConnections=128;
size_t maxPeers=64;
size_t blockRelayPeers=0,txRelayPeers=0;
bool peerDiscovery=true;
bool shortIds=true,earlyBlockRelay=true;
bool fragmentMessages=true,preExecutionRelay=true;
atomic<uint64_t> earlyRelayedBlocks{0},relayedTransactions{0},skippedRelayTransactions{0};
vector<pair<string,uint16_t>> configuredPeers;
vector<pair<string,uint16_t>> backupPeers;
size_t targetPeers=0;
PeerRetrySchedule peerRetrySchedule;
atomic<uint64_t> peerConnectFailures{0},backupConnectAttempts{0};
string configuredPeerFile;
mutex seedAttemptMutex;
set<pair<string,uint16_t>> seedAttempts;
set<pair<string,uint16_t>> addressAttempts; // protected by peerMutex, like peerPool
set<pair<string,uint16_t>> blockAddressAttempts;
bool relayTransactions=false;
atomic<uint64_t> networkSentBytes{0},localProposals{0};
atomic<uint64_t> txPostProcessNs{0},txPostProcessTasks{0};
array<atomic<uint64_t>,3> prioritySentMessages{},prioritySentBytes{};
atomic<uint64_t> gossipDropped{0},broadcastSelected{0},broadcastExcluded{0};
atomic<uint64_t> blockStepWaitNs{0},blockStepProposals{0},blockTailProposals{0};
size_t maxSendBytes=8*1024*1024;
SendQueueBudget sendQueueBudget(128*1024*1024,8192,1024,4*1024*1024,128,256*1024,16);
unsigned short listenPort=8089;
string listenAddress="127.0.0.1",listenAddressV6;
bool peerProtocolV2=false;
int blockInterval=50;
bool produceBlocks=true;

// ------------------------------------------------节点和发送队列------------------------------------------------
struct PendingSend {
    shared_ptr<const vector<uint8_t>> data;
    shared_ptr<SendReservation> reservation;
    size_t offset=0;
    uint64_t fragmentId=0;
};
struct Peer:enable_shared_from_this<Peer> {
    tcp::socket socket;
    boost::asio::strand<any_io_executor> strand;
    steady_timer deadline;
    PrioritySendQueue<PendingSend> messageQueue;
    shared_ptr<SendPeerBudget> sendBudget=make_shared<SendPeerBudget>();
    atomic<bool> closePosted{false}; // safe to read before posting to the strand
    atomic<bool> shortCompact{false};
    atomic<bool> prefillCompact{false},txInventory{false},fragments{false};
    atomic<bool> blockRelayProtocol{false},capabilitiesReceived{false};
    bool blockOnly=false; // Fixed before this connection enters peerPool.
    bool ordinarySlot=false; // Protected by peerMutex; transferred only when duplicate connections merge.
    bool relayCapabilitiesSent=false; // strand only; receiving support does not wait for the remote stats reply
    chain::fragment::Receiver fragmentReceiver;
    uint64_t nextFragmentId=1;
    size_t queuedBytes=0;
    uint64_t key=0;
    atomic<uint64_t> remoteHeight{0};
    uint16_t port=0;
    ip::address address=ip::address_v4::any();
    bool discoveryV2=false;
    bool outbound=false;
    string remoteInstance; // protected by peerMutex
    bool vdfPeerProtocol=false,vdfChallengeSent=false,vdfChallengeAnswered=false,vdfChainMatched=false; // strand only
    array<uint8_t,32> vdfChallenge{};
    uint64_t vdfChallengeAt=0,vdfHintsAt=0;
    bool vdfAuthenticated=false,vdfReserved=false,vdfPending=false; // protected by peerMutex
    array<uint8_t,32> vdfPeerKey{};
    optional<array<uint8_t,32>> expectedVdfKey;
    uint64_t vdfPendingAt=0;
    array<uint8_t,32> requestedHead{};
    uint64_t requestedHeadAt=0;
    uint64_t admissionRead=0,admissionWrite=0;
    map<uint64_t,PendingSend> admissionReplies;
    bool writing=false,closed=false,syncing=false,compact=false;
    uint64_t clockToken=0,clockSentSteady=0;
    int64_t clockSentLogical=0;
    milliseconds lag{0};
    system_clock::time_point T1{};
    Peer(tcp::socket sock):socket(std::move(sock)),strand(make_strand(socket.get_executor())),deadline(strand) { connectionCount++; }
    ~Peer() { connectionCount--; }
};
unordered_map<uint64_t,shared_ptr<Peer>> peerPool;
mutex peerMutex;
vector<shared_ptr<Peer>> PrioritizeVdfBlockPeers(const vector<uint8_t>& data,vector<shared_ptr<Peer>> peers,uint64_t source);
void RecordVdfBlockRecipient(const vector<uint8_t>& data,const shared_ptr<Peer>& peer);
nlohmann::json GetVdfPeerStats();
awaitable<void> session(uint64_t key,shared_ptr<Peer> peer);
awaitable<void> SyncBlock(shared_ptr<Peer> peer);

atomic<uint64_t> nextPeerKey{1};
uint64_t NewPeerKey() { return nextPeerKey.fetch_add(1); }
pair<uint64_t,shared_ptr<Peer>> AddPeer(tcp::socket socket,uint16_t port=0) {
    auto endpoint=socket.remote_endpoint();
    auto key=NewPeerKey();
    lock_guard lock(peerMutex);
    if (peerPool.size()>=maxPeers||peerPool.count(key)) return {key,nullptr};
    auto peer=make_shared<Peer>(std::move(socket));
    peer->key=key;
    peer->port=port;
    peer->address=CanonicalAddress(endpoint.address());
    peer->ordinarySlot=true;
    peerPool.emplace(key,peer);
    return {key,peer};
}
void RemovePeer(uint64_t key) {
    lock_guard lock(peerMutex);
    peerPool.erase(key);
}
void ClosePeer(const shared_ptr<Peer>& peer) {
    peer->closePosted=true;
    if (peer->closed) return;
    peer->closed=true;
    boost::system::error_code ignored;
    peer->deadline.cancel();
    peer->socket.close(ignored);
    peer->messageQueue.Clear();
    peer->fragmentReceiver.Clear();
    peer->admissionReplies.clear();
    peer->queuedBytes=0;
    if (peer->key) RemovePeer(peer->key);
}
void PeerError(const shared_ptr<Peer>& peer,exception_ptr error) {
    if (error) {
        try { rethrow_exception(error); }
        catch (const exception& e) { spdlog::warn("Peer error: {}",e.what()); }
    }
    ClosePeer(peer);
}
awaitable<void> WriteLoop(shared_ptr<Peer> peer) {
    while (!peer->closed&&!peer->messageQueue.Empty()) {
        // Keep the reservation alive until async_write has completed, even if
        // ClosePeer clears the queue while the write is being cancelled.
        auto queued=peer->messageQueue.Pop();
        auto priority=queued.first;
        auto message=std::move(queued.second);
        auto data=message.data;
        vector<uint8_t> chunk;
        if (message.fragmentId||(peer->fragments&&chain::fragment::Allowed(data->front())&&data->size()>5+chain::fragment::kChunkBytes)) {
            if (!message.fragmentId) message.fragmentId=peer->nextFragmentId++;
            chunk=chain::fragment::Encode(*data,message.fragmentId,message.offset);
        }
        auto bytes=chunk.empty()?span<const uint8_t>(*data):span<const uint8_t>(chunk);
        boost::system::error_code ec;
        co_await async_write(peer->socket,buffer(bytes.data(),bytes.size()),redirect_error(use_awaitable,ec));
        if (ec||peer->closed) { ClosePeer(peer); co_return; }
        networkSentBytes+=bytes.size();
        prioritySentMessages[size_t(priority)]++;
        prioritySentBytes[size_t(priority)]+=bytes.size();
        if (!chunk.empty()) {
            chain::fragment::sentFrames++;
            message.offset+=chunk.size()-5-chain::fragment::kHeaderBytes;
        }
        if (!chunk.empty()&&message.offset<data->size()-5) peer->messageQueue.PushFront(priority,std::move(message));
        else peer->queuedBytes-=data->size();
    }
    peer->writing=false;
}
void QueueData(const shared_ptr<Peer>& peer,PendingSend message) {
        if (peer->closed) return;
        if (message.data->front()==14&&peer->port) peer->relayCapabilitiesSent=true;
        auto size=message.data->size();
        auto priority=MessageSendPriority(message.data->front());
        peer->messageQueue.Push(priority,std::move(message));
        peer->queuedBytes+=size;
        if (!peer->writing) {
            peer->writing=true;
            co_spawn(peer->strand,WriteLoop(peer),[peer](exception_ptr error){if (error) PeerError(peer,error);});
        }
}
void RejectSend(const shared_ptr<Peer>& peer) {
    // Many worker threads may hit one slow peer at once. Only one close handler
    // may be outstanding; rejected frames never enter the executor queue.
    if (!peer->closePosted.exchange(true)) post(peer->strand,[peer] { ClosePeer(peer); });
}
const string& NodeRelayInstance() {
    static const string value=[] {array<uint8_t,32> id{};randombytes_buf(id.data(),id.size());return U32ToHex(id);}();
    return value;
}
void TransferOrdinarySlotLocked(const shared_ptr<Peer>& keep,const shared_ptr<Peer>& drop) {
    if (keep->blockOnly||drop->blockOnly||!drop->ordinarySlot) return;
    keep->ordinarySlot=true;drop->ordinarySlot=false;
    keep->vdfReserved=false;keep->vdfPending=false;
}
void DeduplicatePeer(const shared_ptr<Peer>& peer,const string& instance) {
    if (instance.size()!=64||instance==NodeRelayInstance()||
        !all_of(instance.begin(),instance.end(),[](char c){return (c>='0'&&c<='9')||(c>='a'&&c<='f');})) return;
    vector<shared_ptr<Peer>> close;
    {
        lock_guard lock(peerMutex);peer->remoteInstance=instance;
        const bool preferOutbound=NodeRelayInstance()<instance;
        auto keep=peer;
        for (const auto& [key,other]:peerPool) {
            if (other==peer||other->closePosted||other->blockOnly!=peer->blockOnly||other->address!=peer->address||other->port!=peer->port||other->remoteInstance!=instance) continue;
            if ((other->outbound==preferOutbound&&keep->outbound!=preferOutbound)||
                            (other->outbound==keep->outbound&&other->key<keep->key)) {
                TransferOrdinarySlotLocked(other,keep);close.push_back(keep);keep=other;
            } else {TransferOrdinarySlotLocked(keep,other);close.push_back(other);}
        }
    }
    for (const auto& extra:close) RejectSend(extra);
}
void QueueData(const shared_ptr<Peer>& peer,shared_ptr<const vector<uint8_t>> data) {
    if (peer->closed||!data||data->empty()) return;
    auto reservation=sendQueueBudget.TryReserve(peer->sendBudget,data->size(),maxSendBytes,true,
        MessageSendPriority(data->front())==SendPriority::Urgent);
    if (!reservation) { ClosePeer(peer); return; }
    QueueData(peer,PendingSend{std::move(data),std::move(reservation)});
}
void SendData(const shared_ptr<Peer>& peer,shared_ptr<const vector<uint8_t>> data) {
    if (!peer||!data||data->empty()||peer->closePosted) return;
    if (peer->blockOnly&&(MessageSendPriority(data->front())==SendPriority::Transaction||data->front()==49||data->front()==50)) return;
    auto reservation=sendQueueBudget.TryReserve(peer->sendBudget,data->size(),maxSendBytes,true,
        MessageSendPriority(data->front())==SendPriority::Urgent);
    if (!reservation) { RejectSend(peer); return; }
    post(peer->strand,[peer,message=PendingSend{std::move(data),std::move(reservation)}]() mutable {
        QueueData(peer,std::move(message));
    });
}
void SendData(const shared_ptr<Peer>& peer,vector<uint8_t> data) {
    SendData(peer,make_shared<const vector<uint8_t>>(std::move(data)));
}
bool TrySendGossip(const shared_ptr<Peer>& peer,shared_ptr<const vector<uint8_t>> data) {
    if (!peer||!data||data->empty()||peer->closePosted) return false;
    if (peer->blockOnly&&(MessageSendPriority(data->front())==SendPriority::Transaction||data->front()==49||data->front()==50)) return false;
    auto reservation=sendQueueBudget.TryReserve(peer->sendBudget,data->size(),maxSendBytes,false,
        MessageSendPriority(data->front())==SendPriority::Urgent);
    if (!reservation) { gossipDropped++; return false; }
    post(peer->strand,[peer,message=PendingSend{std::move(data),std::move(reservation)}]() mutable {
        QueueData(peer,std::move(message));
    });
    return true;
}
void SendAdmission(const shared_ptr<Peer>& peer,uint64_t order,vector<uint8_t> results) {
    if (peer->closePosted) return;
    auto data=make_shared<const vector<uint8_t>>(GenerateMessage(45,results));
    auto reservation=sendQueueBudget.TryReserve(peer->sendBudget,data->size(),maxSendBytes,true,true);
    if (!reservation) { RejectSend(peer); return; }
    post(peer->strand,[peer,order,message=PendingSend{std::move(data),std::move(reservation)}]() mutable {
        if (peer->closed) return;
        if (peer->admissionReplies.size()>=128) { ClosePeer(peer); return; }
        peer->admissionReplies.emplace(order,std::move(message));
        while (true) {
            auto found=peer->admissionReplies.find(peer->admissionWrite);
            if (found==peer->admissionReplies.end()) break;
            QueueData(peer,std::move(found->second));
            if (peer->closed) return;
            peer->admissionReplies.erase(found); peer->admissionWrite++;
        }
    });
}
vector<shared_ptr<Peer>> SelectBroadcastPeers(uint64_t source,const array<uint8_t,32>& payloadHash,size_t limit,bool transactions=false) {
    // Every process has its own salt. Hashing the payload also changes the
    // selected neighbors between messages, without depending on map order.
    static const array<uint8_t,32> salt=[] { array<uint8_t,32> value{}; randombytes_buf(value.data(),value.size()); return value; }();
    vector<shared_ptr<Peer>> peers;optional<pair<ip::address,uint16_t>> sourceEndpoint;
    {
        lock_guard lock(peerMutex);
        for (const auto& [key,peer]:peerPool) {
            if (key==source) sourceEndpoint=make_pair(peer->address,peer->port);
            if (peer->blockOnly&&(!peer->vdfAuthenticated||peer->vdfPending)) continue;
            peers.push_back(peer);
        }
    }
    if (!transactions) stable_sort(peers.begin(),peers.end(),[](const auto& a,const auto& b){return a->blockOnly>b->blockOnly;});
    set<pair<ip::address,uint16_t>> sent;
    vector<pair<array<uint8_t,16>,shared_ptr<Peer>>> ranked;
    for (const auto& peer:peers) {
        if (transactions&&peer->blockOnly) continue;
        auto id=make_pair(peer->address,peer->port);
        if (peer->closePosted||!peer->port||peer->key==source||(sourceEndpoint&&id==*sourceEndpoint)||!sent.insert(id).second) continue;
        array<uint8_t,16> score{};
        if (limit) {
            auto endpoint=FormatEndpoint(peer->address.to_string(),peer->port);
            crypto_generichash_state state;
            crypto_generichash_init(&state,salt.data(),salt.size(),score.size());
            crypto_generichash_update(&state,payloadHash.data(),payloadHash.size());
            crypto_generichash_update(&state,reinterpret_cast<const uint8_t*>(endpoint.data()),endpoint.size());
            crypto_generichash_final(&state,score.data(),score.size());
        }
        ranked.emplace_back(score,peer);
    }
    if (limit&&ranked.size()>limit) {
        auto compare=[](const auto& first,const auto& second) { return first.first<second.first; };
        partial_sort(ranked.begin(),ranked.begin()+limit,ranked.end(),compare);
        broadcastExcluded+=ranked.size()-limit;
        ranked.resize(limit);
    }
    peers.clear();peers.reserve(ranked.size());
    for (auto& item:ranked) peers.push_back(std::move(item.second));
    broadcastSelected+=peers.size();
    return peers;
}
// Ordinary gossip shares its bytes across recipients. A full optional queue
// drops this copy; block queries and missing-transaction replies remain active.
void BroadcastData(vector<uint8_t> data,uint64_t source=0) {
    if (data.empty()) return;
    auto limit=MessageSendPriority(data.front())==SendPriority::Transaction?txRelayPeers:0;
    array<uint8_t,32> hash{};
    if (limit) crypto_generichash(hash.data(),hash.size(),data.data(),data.size(),nullptr,0);
    auto peers=SelectBroadcastPeers(source,hash,limit,MessageSendPriority(data.front())==SendPriority::Transaction||data.front()==49||data.front()==50);
    auto shared=make_shared<const vector<uint8_t>>(std::move(data));
    for (const auto& peer:peers) TrySendGossip(peer,shared);
}
struct CompactReceiveReservation;
awaitable<void> ProcessMessage(shared_ptr<Peer> peer,uint8_t type,vector<uint8_t> data,
    shared_ptr<CompactReceiveReservation> compactReceipt={});
#include "Blocks.h"
#include "TransactionRelay.h"
#include "TxWork.h"
#include "Vdf.h"
#include "VdfPeers.h"
EarlyRelayBudget earlyRelayBudget;
atomic<uint64_t> preExecutionRelayedBlocks{0},preExecutionRelayLimited{0};
void RelayBeforeExecution(const vector<uint8_t>& data,const Block& block,uint64_t source) {
    if (!preExecutionRelay||!block.vdf||!VdfConsensus::config.enabled) return;
    {
        shared_lock lock(dbCommitMutex);
        if (!DBValidateVdfBlock(data)) return;
    }
    if (!earlyRelayBudget.Reserve(block.height,block.vdf->slot,block.vdf->producer,data.size())) { preExecutionRelayLimited++;return; }
    if (source) RememberBlockSource(block.hash,source);
    AnnounceBlock(data);preExecutionRelayedBlocks++;
}
awaitable<void> MaintainTransactionRelay() {
    steady_timer timer(co_await this_coro::executor);
    while (nodeRunning) {
        TxInventoryMaintenance();
        timer.expires_after(milliseconds(100));co_await timer.async_wait(use_awaitable);
    }
}
void QueryHeight(const shared_ptr<Peer>& peer) { SendData(peer,GenerateMessage(19,{})); }
void QueryBlock(const shared_ptr<Peer>& peer,uint64_t height) {
    array<uint8_t,8> data;
    WriteU64(data.data(),height);
    SendData(peer,GenerateMessage(6,data));
}

// ------------------------------------------------连接、发现和同步------------------------------------------------
size_t ConnectedPeerCountLocked(bool includeAttempts=false) {
    set<pair<string,uint16_t>> endpoints;
    for (const auto& [key,peer]:peerPool)
        if (!peer->closePosted&&peer->port&&peer->ordinarySlot) endpoints.emplace(peer->address.to_string(),peer->port);
    if (includeAttempts) endpoints.insert(addressAttempts.begin(),addressAttempts.end());
    return endpoints.size();
}
awaitable<void> Connect(ip::address address,uint16_t port,unsigned delayMs=0,bool backup=false,
        optional<array<uint8_t,32>> expectedVdfKey=nullopt,bool blockOnly=false) {
    address=CanonicalAddress(address);
    auto executor=co_await this_coro::executor;
    if (!port||connectionCount>=VdfConnectionLimit()) co_return;
    if (!peerRetrySchedule.Ready({address.to_string(),port},GetSteadyTime())) co_return;
    {
        lock_guard lock(peerMutex);
        for (const auto& [key,peer]:peerPool) {
            if (!peer->closePosted&&peer->blockOnly==blockOnly&&peer->port==port&&peer->address==address) co_return;
        }
        if (backup&&(!targetPeers||ConnectedPeerCountLocked(true)>=targetPeers)) co_return;
        auto& attempts=blockOnly?blockAddressAttempts:addressAttempts;
        if (attempts.size()>=32||!attempts.emplace(address.to_string(),port).second) co_return;
    }
    struct ReleaseAddress { string address; uint16_t port; bool block; ~ReleaseAddress() { lock_guard lock(peerMutex);(block?blockAddressAttempts:addressAttempts).erase({address,port}); } } release{address.to_string(),port,blockOnly};
    struct RecordAttempt { pair<string,uint16_t> endpoint;bool success=false;~RecordAttempt(){peerRetrySchedule.Record(endpoint,success,GetSteadyTime());if(!success)peerConnectFailures++;} } attempt{{address.to_string(),port}};
    if (backup) backupConnectAttempts++;
    if (delayMs) { steady_timer stagger(executor);stagger.expires_after(milliseconds(delayMs));co_await stagger.async_wait(use_awaitable); }
    auto peer=make_shared<Peer>(tcp::socket(executor));
    peer->outbound=true;
    peer->blockOnly=blockOnly;
    boost::system::error_code ec;
    peer->deadline.expires_after(seconds(5));
    peer->deadline.async_wait([peer](boost::system::error_code error){if (!error) ClosePeer(peer);});
    co_await peer->socket.async_connect(tcp::endpoint(address,port),redirect_error(use_awaitable,ec));
    peer->deadline.cancel();
    if (ec) co_return;
    peer->socket.set_option(tcp::no_delay(true),ec);
    peer->discoveryV2=blockOnly||address.is_v6()||peerProtocolV2;
    array<uint8_t,3> hello{uint8_t(blockOnly?5:(peer->discoveryV2?4:3)),uint8_t(listenPort),uint8_t(listenPort>>8)};
    co_await async_write(peer->socket,buffer(hello),redirect_error(use_awaitable,ec));
    if (ec) co_return;
    peer->key=NewPeerKey();
    peer->port=port;
    peer->address=address;
    peer->compact=true;
    peer->expectedVdfKey=expectedVdfKey;
    {
        lock_guard lock(peerMutex);
        if (backup&&ConnectedPeerCountLocked()>=targetPeers) {attempt.success=true;co_return;}
        if (!AdmitVdfPeerLocked(peer)||peerPool.count(peer->key)) co_return;
        peerPool.emplace(peer->key,peer);
    }
    attempt.success=true;
    co_spawn(peer->strand,session(peer->key,peer),[peer](exception_ptr error){PeerError(peer,error);});
    QueryHeight(peer);
    SendData(peer,GenerateMessage(13,{}));
    if (VdfConsensus::config.enabled&&!peer->blockOnly) SendData(peer,GenerateMessage(50,{}));
}
awaitable<void> MaintainVdfProducerPeers() {
    steady_timer timer(co_await this_coro::executor);
    while (nodeRunning) {
        auto targets=RefreshVdfProducerPeers();
        for (const auto& target:targets)
            co_spawn(timer.get_executor(),Connect(target.address,target.port,0,false,target.publicKey,target.blockOnly),[](exception_ptr){});
        timer.expires_after(milliseconds(200));
        co_await timer.async_wait(use_awaitable);
    }
}
awaitable<void> ConnectSeed(string host="127.0.0.1",uint16_t port=8089,bool backup=false) {
    auto executor=co_await this_coro::executor;
    auto resolver=make_shared<tcp::resolver>(executor);steady_timer deadline(executor);boost::system::error_code ec;
    deadline.expires_after(seconds(3));deadline.async_wait([resolver](boost::system::error_code error){if (!error) resolver->cancel();});
    auto entries=co_await resolver->async_resolve(host,to_string(port),redirect_error(use_awaitable,ec));
    deadline.cancel();
    if (ec) co_return;
    vector<ip::address> v4,v6;
    for (const auto& entry:entries) {
        auto address=CanonicalAddress(entry.endpoint().address()); auto& list=address.is_v6()?v6:v4;
        if (list.size()<4&&find(list.begin(),list.end(),address)==list.end()) list.push_back(address);
    }
    unsigned delay=0;
    for (size_t i=0;i<max(v4.size(),v6.size());i++) {
        if (i<v6.size()) { co_spawn(executor,Connect(v6[i],port,delay,backup),[](exception_ptr){});delay+=150; }
        if (i<v4.size()) { co_spawn(executor,Connect(v4[i],port,delay,backup),[](exception_ptr){});delay+=150; }
    }
}
// Local gateway-owned file only. No remote query may change seed configuration.
void ReloadConfiguredPeers() {
    if (configuredPeerFile.empty()) return;
    try {
        if (std::filesystem::file_size(configuredPeerFile)>65536) return;
        ifstream input(configuredPeerFile); auto values=nlohmann::json::parse(input);
        if (!values.is_array()||values.size()>1024) return;
        vector<pair<string,uint16_t>> peers;
        for (const auto& value:values) {
            auto endpoint=ParseEndpoint(value.get<string>());
            if (find(peers.begin(),peers.end(),endpoint)==peers.end()) peers.push_back(endpoint);
        }
        configuredPeers=std::move(peers);
    } catch (...) { /* An incomplete/invalid update keeps the prior entries. */ }
}
vector<uint8_t> GenerateDiscoverMessage(bool extended=false) {
    vector<uint8_t> data;
    lock_guard lock(peerMutex);
    data.reserve(peerPool.size()*6);
    for (const auto& [key,peer]:peerPool) {
        if (!peer->port) continue;
        auto address=CanonicalAddress(peer->address);
        if (address.is_v6()&&(address.to_v6().is_link_local()||address.to_v6().scope_id())) continue;
        if (!extended&&address.is_v6()) continue;
        if (extended) data.push_back(address.is_v4()?4:6);
        if (address.is_v4()) { auto bytes=address.to_v4().to_bytes();data.insert(data.end(),bytes.begin(),bytes.end()); }
        else { auto bytes=address.to_v6().to_bytes();data.insert(data.end(),bytes.begin(),bytes.end()); }
        data.push_back(uint8_t(peer->port));
        data.push_back(uint8_t(peer->port>>8));
    }
    return GenerateMessage(extended?48:9,data);
}
awaitable<void> ProcessDiscoverMessage(vector<uint8_t> data,bool extended=false) {
    if (!peerDiscovery||data.size()>1024*19||(!extended&&data.size()%6)) co_return;
    vector<pair<ip::address,uint16_t>> entries;
    for (size_t offset=0;offset<data.size();) {
        auto family=extended?data[offset++]:4;size_t length=family==4?4:family==6?16:0;
        if (!length||data.size()-offset<length+2||entries.size()>=1024) co_return;
        ip::address address;
        if (family==4) { ip::address_v4::bytes_type bytes;memcpy(bytes.data(),data.data()+offset,4);address=ip::address_v4(bytes); }
        else { ip::address_v6::bytes_type bytes;memcpy(bytes.data(),data.data()+offset,16);address=ip::address_v6(bytes); }
        offset+=length;auto port=uint16_t(data[offset])|(uint16_t(data[offset+1])<<8);offset+=2;
        address=CanonicalAddress(address);
        if (!port||address.is_unspecified()||address.is_multicast()||(address.is_v6()&&address.to_v6().is_link_local())) continue;
        if (address.is_loopback()&&port==listenPort) continue;
        entries.emplace_back(address,port);
    }
    auto executor=co_await this_coro::executor;
    for (const auto& [address,port]:entries)
        co_spawn(executor,Connect(address,port),[](exception_ptr error) {
            if (error) { try { rethrow_exception(error); } catch (const exception& e) { spdlog::warn("Connect: {}",e.what()); } }
        });
}
struct SyncCompletion {
    shared_ptr<Peer> peer;
    ~SyncCompletion() { peer->syncing=false; auto owner=peer->key; syncOwner.compare_exchange_strong(owner,0); }
};
awaitable<void> SyncBlock(shared_ptr<Peer> peer) {
    SyncCompletion complete{peer};
    steady_timer timer(co_await this_coro::executor);
    uint64_t requested=ReadChainHead().height,progress=requested;
    auto lastProgress=steady_clock::now();
    unsigned retries=0;
    while (!peer->closed&&ReadChainHead().height<peer->remoteHeight) {
        auto height=ReadChainHead().height;
        if (height!=progress) { progress=height; lastProgress=steady_clock::now(); retries=0; }
        if (steady_clock::now()-lastProgress>seconds(3)) {
            if (++retries>3) break;
            requested=height; lastProgress=steady_clock::now(); syncRetries++;
            peer->requestedHead={}; QueryHeight(peer);
        }
        requested=max(requested,height);
        auto limit=min(peer->remoteHeight.load(),height+syncWindow);
        while (requested<limit) {
            auto count=uint16_t(min(uint64_t(4),limit-requested));
            if (peer->compact) {
                array<uint8_t,10> request; WriteU64(request.data(),requested+1); request[8]=uint8_t(count); request[9]=uint8_t(count>>8);
                SendData(peer,GenerateMessage(42,request)); syncRequests++;
            } else for (uint16_t i=1;i<=count;i++) QueryBlock(peer,requested+i);
            requested+=count;
        }
        timer.expires_after(milliseconds(2)); co_await timer.async_wait(use_awaitable);
    }
}
void StartSync(const shared_ptr<Peer>& peer) {
    if (peer->syncing||peer->closed||!peer->key) return;
    uint64_t owner=0;
    if (!syncOwner.compare_exchange_strong(owner,peer->key)) { syncOwnerSkips++; return; }
    peer->syncing=true;
    try { co_spawn(peer->strand,SyncBlock(peer),[peer](exception_ptr error){if (error) PeerError(peer,error);}); }
    catch (...) { peer->syncing=false; owner=peer->key; syncOwner.compare_exchange_strong(owner,0); throw; }
}
awaitable<void> OpenDiscoveryMode() {
    steady_timer timer(co_await this_coro::executor);
    size_t seedCursor=0;
    size_t backupCursor=0;uint64_t belowTargetSince=0;
    while (nodeRunning) {
        PruneCompactBlocks();
        vector<shared_ptr<Peer>> peers;
        set<pair<ip::address,uint16_t>> connectedEndpoints;
        {
            lock_guard lock(peerMutex);
            for (const auto& [key,peer]:peerPool) {
                peers.push_back(peer);
                if (!peer->closePosted&&peer->port&&peer->ordinarySlot) connectedEndpoints.emplace(peer->address,peer->port);
            }
        }
        for (const auto& peer:peers) QueryHeight(peer);
        // Bound concurrent seed attempts so an unreachable first address does
        // not hold back a reachable IPv4/IPv6 fallback or the discovery loop.
        ReloadConfiguredPeers(); unsigned delay=0;
        if (!configuredPeers.empty()) seedCursor%=configuredPeers.size();
        for (size_t visited=0;visited<configuredPeers.size();++visited) {
            const auto endpoint=configuredPeers[seedCursor];
            { lock_guard lock(seedAttemptMutex);
              if (seedAttempts.size()>=8) break;
              seedCursor=(seedCursor+1)%configuredPeers.size();
              if (!seedAttempts.insert(endpoint).second) continue; }
            co_spawn(timer.get_executor(),[endpoint,wait=delay]() -> awaitable<void> {
                struct Release { pair<string,uint16_t> endpoint; ~Release() { lock_guard lock(seedAttemptMutex);seedAttempts.erase(endpoint); } } release{endpoint};
                steady_timer stagger(co_await this_coro::executor); stagger.expires_after(milliseconds(wait));
                co_await stagger.async_wait(use_awaitable);
                co_await ConnectSeed(endpoint.first,endpoint.second);
            },[](exception_ptr){});
            delay+=150;
        }
        auto now=GetSteadyTime();
        if (targetPeers&&connectedEndpoints.size()<targetPeers) {
            if (!belowTargetSince) belowTargetSince=now;
            if (now-belowTargetSince>=5'000'000'000ULL&&!backupPeers.empty()) {
                const auto endpoint=backupPeers[(backupCursor++)%backupPeers.size()];
                co_spawn(timer.get_executor(),ConnectSeed(endpoint.first,endpoint.second,true),[](exception_ptr){});
            }
        } else belowTargetSince=0;
        timer.expires_after(seconds(1));
        co_await timer.async_wait(use_awaitable);
    }
}
vector<uint8_t> GenerateTxsMessage() {
    vector<uint8_t> data;
    lock_guard lock(txpoolMutex);
    data.reserve(min(size_t(256),txpool.size())*176);
    for (const auto& [hash,tx]:txpool) {
        if (data.size()==256*176) break;
        data.insert(data.end(),tx.begin(),tx.end());
    }
    return GenerateMessage(10,data);
}
awaitable<void> SynchronizeClock() {
    steady_timer timer(co_await this_coro::executor);
    while (nodeRunning) {
        vector<shared_ptr<Peer>> peers;
        { lock_guard lock(peerMutex); for (const auto& [key,peer]:peerPool) peers.push_back(peer); }
        for (const auto& peer:peers) post(peer->strand,[peer] {
            if (peer->closed) return;
            auto now=GetSteadyTime();
            if (peer->clockToken&&now-peer->clockSentSteady<1000000000) return;
            peer->clockSentSteady=now; peer->clockSentLogical=GetLogicalTimeUs(); peer->clockToken=now;
            array<uint8_t,8> request; WriteU64(request.data(),now);
            SendData(peer,GenerateMessage(27,request));
        });
        timer.expires_after(milliseconds(nodeClock.periodMs));
        co_await timer.async_wait(use_awaitable);
        AdjustClock();
    }
}
void WaitLogicalTime(int64_t target) {
    while (nodeRunning) {
        auto remaining=target-GetLogicalTimeUs();
        if (remaining<=0) break;
        unique_lock lock(txpoolMutex);
        txpoolCondition.wait_for(lock,microseconds(min(int64_t(5000),remaining)));
    }
}
void ProcessTxTimeACKMessage(const vector<uint8_t>& data,system_clock::time_point T4,const shared_ptr<Peer>& peer) {
    if (data.size()!=16||peer->T1==system_clock::time_point{}) return;
    auto T2=system_clock::time_point(milliseconds(ReadU64(data.data())));
    auto T3=system_clock::time_point(milliseconds(ReadU64(data.data()+8)));
    // lag 只保存 RTT/2；时钟偏移不用于调整出块时间。
    peer->lag=duration_cast<milliseconds>((T4-peer->T1)-(T3-T2))/2;
}

// ------------------------------------------------出块循环------------------------------------------------
// Each deadline is frozen in monotonic time; incoming messages and clock
// corrections cannot extend an already scheduled round.
bool WaitBlockSchedule(steady_clock::time_point deadline) {
    unique_lock lock(txpoolMutex);
    while (nodeRunning&&!forkReady&&steady_clock::now()<deadline)
        txpoolCondition.wait_until(lock,min(deadline,steady_clock::now()+milliseconds(5)));
    return nodeRunning&&!forkReady;
}
vector<uint8_t> GenerateSteppedBlock(steady_clock::time_point deadline,bool stopOnCandidate=false) {
    size_t attemptedReady=SIZE_MAX;
    while (true) {
        unique_lock lock(txpoolMutex);
        if (forkReady||(stopOnCandidate&&blockReady)) return {};
        if (!nodeRunning||steady_clock::now()>=deadline) {
            lock.unlock();
            return GenerateBlock(blockTxStep,true);
        }
        auto ready=readyPoolTx;
        // The largest permitted step multiple is a full block. Smaller
        // multiples keep collecting transactions until the send time.
        auto fullCount=(maxBlockTx/blockTxStep)*blockTxStep;
        if (fullCount&&ready>=fullCount&&ready!=attemptedReady) {
            attemptedReady=ready;
            lock.unlock();
            auto data=GenerateBlock(blockTxStep,false);
            if (!data.empty()&&ReadU64(data.data()+72)>=fullCount) return data;
            // Ready nonces can exceed the actually selectable byte/gas/balance
            // budget. Retry on pool progress or at the fixed partial deadline.
            continue;
        }
        auto start=GetSteadyTime();
        txpoolCondition.wait_until(lock,min(deadline,steady_clock::now()+milliseconds(5)),[&] {
            return !nodeRunning||forkReady||(stopOnCandidate&&blockReady)||
                (fullCount&&readyPoolTx>=fullCount&&readyPoolTx!=attemptedReady);
        });
        blockStepWaitNs+=GetSteadyTime()-start;
    }
}
void RecordSteppedProposal(uint64_t txCount) {
    if (blockTxStep>1) {
        if (txCount<blockTxStep) blockTailProposals++;
        else blockStepProposals++;
    }
}
void MainLoop() {
    if (VdfConsensus::config.enabled) { RunVdfBlockLoop(); return; }
    try {
        int64_t lastRound=-1;
        while (true) {
            if (ProcessForks()) AnnounceBlock(DBReadBlockByHash(DBReadCurrentBlock()));
            {
                unique_lock lock(txpoolMutex);
                txpoolCondition.wait(lock,[]{return !nodeRunning||forkReady||blockReady||(produceBlocks&&readyPoolTx>0);});
                if (forkReady) continue;
                if (!nodeRunning&&!blockReady&&(!produceBlocks||txpool.empty())) break;
                if (blockTxStep<=1&&!nodeClock.roundMs&&produceBlocks&&!blockReady&&nodeRunning&&readyPoolTx<maxBlockTx) {
                    txpoolCondition.wait_for(lock,milliseconds(blockInterval),[]{return !nodeRunning||forkReady||blockReady||readyPoolTx>=maxBlockTx;});
                }
                if (forkReady) continue;
            }
            if (nodeClock.roundMs&&nodeRunning) {
                auto period=int64_t(nodeClock.roundMs)*1000;
                if (blockTxStep>1&&produceBlocks) {
                    auto logicalNow=GetLogicalTimeUs();
                    auto steadyNow=steady_clock::now();
                    auto window=int64_t(nodeClock.candidateMs)*1000;
                    auto round=max(lastRound+1,logicalNow/period);
                    if (round*period+period-window<=logicalNow) ++round;
                    auto roundStart=steadyNow+microseconds(round*period-logicalNow);
                    auto deadline=roundStart+microseconds(period-window);
                    WaitBlockSchedule(roundStart);
                    if (forkReady) continue;
                    lastRound=round;
                    auto data=GenerateSteppedBlock(deadline);
                    if (forkReady) continue;
                    if (data.empty()&&!nodeRunning&&!blockReady) break;
                    if (!data.empty()&&ProcessBlock(data)) {
                        localProposals++; RecordSteppedProposal(ReadU64(data.data()+72)); AnnounceBlock(data);
                    }
                    // Preserve a full propagation window even when construction
                    // or execution itself takes longer than its scheduled start.
                    WaitBlockSchedule(steady_clock::now()+microseconds(window));
                    if (forkReady) continue;
                } else {
                    auto round=max(lastRound+1,GetLogicalTimeUs()/period+1);
                    WaitLogicalTime(round*period);
                    lastRound=round;
                    if (produceBlocks) {
                        auto data=GenerateBlock();
                        if (!data.empty()&&ProcessBlock(data)) { localProposals++; AnnounceBlock(data); }
                    }
                    WaitLogicalTime(round*period+int64_t(nodeClock.candidateMs)*1000);
                }
            } else if (produceBlocks&&!blockReady) {
                auto data=blockTxStep>1?GenerateSteppedBlock(steady_clock::now()+milliseconds(blockInterval),true):GenerateBlock();
                if (forkReady) continue;
                if (data.empty()&&!nodeRunning&&!blockReady) break;
                if (!data.empty()) {
                    localProposals++; auto count=ReadU64(data.data()+72);
                    if (ProcessBlock(std::move(data))) RecordSteppedProposal(count);
                }
            }
            pair<Block,vector<uint8_t>> selected;
            {
                lock_guard lock(blockBufferLock);
                selected=std::move(BlockBuffer[0]);
                BlockBuffer[0]={};
                blockReady=false;
            }
            if (selected.second.empty()) continue;
            // 已收到连续后继时复用分支批次，一次同步写盘发布整段状态。
            if (forkReady||HasForkChild(selected.first.hash)) {
                QueueForkBlock(selected.first,selected.second,false); forkReady=true;
                if (ProcessForks()) { AnnounceBlock(DBReadBlockByHash(DBReadCurrentBlock())); continue; }
            }
            if (!CommitBlock(selected.first,selected.second)) {
                spdlog::warn("Rejected candidate at height {}",selected.first.height);
                continue;
            }
            AnnounceBlock(selected.second);
            forkReady=true;
            spdlog::debug("Local block committed. Height:{}, TxNum:{}",selected.first.height,selected.first.txNum);
        }
    } catch (const exception& e) {
        spdlog::error("MainLoop stopped: {}",e.what());
        nodeFailed=true;
        nodeRunning=false;
        if (nodeIO) nodeIO->stop();
    }
}
nlohmann::json GetNodeStats() {
    size_t poolSize;
    size_t readySize;
    size_t peers;
    size_t shortCompactPeers=0;
    {
        lock_guard lock(txpoolMutex);
        poolSize=txpool.size();
        readySize=readyPoolTx;
    }
    {
        lock_guard lock(peerMutex);
        peers=peerPool.size();
        for (const auto& [key,peer]:peerPool) shortCompactPeers+=shortIds&&peer->shortCompact.load();
    }
    auto first=firstTxTime.load();
    auto last=lastCommitTime.load();
    double elapsed=last>first&&first?double(last-first)/1e9:0;
    auto snapshot=ReadChainHead();
    auto height=snapshot.height,chainTx=snapshot.transactions,burned=snapshot.burned;
    auto userCount=snapshot.users,evmCount=snapshot.contracts; auto head=snapshot.hash;
    auto sends=sendQueueBudget.Snapshot();
    nlohmann::json stats={{"relay_protocol",shortIds?2:1},{"relay_instance",NodeRelayInstance()},{"vdf_peer_protocol",VdfProducerMeshEnabled()?1:0},{"vdf_block_connection",VdfProducerMeshEnabled()},{"compact_prefill",shortIds&&compactPrefill},{"tx_inventory",txInventory},{"message_fragments",fragmentMessages},
        {"vdf",GetVdfStats()},{"received",receivedTx.load()},{"valid",validTx.load()},{"invalid",invalidTx.load()},
        {"duplicate",duplicateTx.load()},{"rejected",rejectedTx.load()},{"committed",committedTx.load()},
        {"pool",poolSize},{"ready_pool",readySize},{"pending",pendingVerify.load()},{"peak_pending",peakPendingVerify.load()},
        {"blocks",blockCount.load()},{"height",height},{"head",U32ToHex(head)},
        {"elapsed_seconds",elapsed},{"local_commit_tps",elapsed>0?committedTx.load()/elapsed:0},
        {"verify_worker_ms",verifyNs.load()/1e6},{"pool_worker_ms",poolNs.load()/1e6},
        {"tx_postprocess_ms",txPostProcessNs.load()/1e6},{"tx_postprocess_tasks",txPostProcessTasks.load()},
        {"signature_checks",signatureChecks.load()},{"signature_cache_hits",signatureCacheHits.load()},
        {"duplicate_blocks",duplicateBlocks.load()},{"pool_refresh_ms",poolRefreshNs.load()/1e6},
        {"tx_commit_wait_ms",txCommitWaitNs.load()/1e6},{"tx_read_ms",txReadNs.load()/1e6},
        {"tx_pool_wait_ms",txPoolWaitNs.load()/1e6},{"block_commit_ms",blockCommitNs.load()/1e6},
        {"queue_wait_ms",queueWaitNs.load()/1e6},{"block_build_ms",blockBuildNs.load()/1e6},
        {"block_verify_ms",blockVerifyNs.load()/1e6},{"db_write_ms",dbWriteNs.load()/1e6},
        {"db_writes",dbWriteCount.load()},{"network_bytes",networkBytes.load()},{"sync",dbSync},
        {"db_prepare_ms",dbPrepareNs.load()/1e6},{"db_batch_bytes",dbBatchBytes.load()},
        {"db_profiled_writes",dbProfileWrites.load()},{"db_wal_sample_ms",dbWalNs.load()/1e6},
        {"db_memtable_sample_ms",dbMemtableNs.load()/1e6},{"db_delay_sample_ms",dbDelayNs.load()/1e6},
        {"db_other_sample_ms",dbOtherNs.load()/1e6},{"db_write_stalls_ms",options.statistics->getTickerCount(rocksdb::STALL_MICROS)/1000.0},
        {"bloom_useful",options.statistics->getTickerCount(rocksdb::BLOOM_FILTER_USEFUL)},
        {"users",userCount},{"base_fee",baseFee},{"user_burned",burned},
        {"user_supply",genesisSupply-burned},{"invalid_user_tx",invalidUserTx.load()},
        {"conflict_tx",conflictTx.load()},{"invalid_user_blocks",invalidUserBlocks.load()},
        {"user_check_ms",userCheckNs.load()/1e6},{"user_writes",userWriteCount.load()},
        {"chain_transactions",chainTx},{"reorganizations",reorgCount.load()},{"max_reorg_depth",maxReorgDepth.load()},
        {"evm_accounts",evmCount}, {"peers",peers},{"short_compact_peers",shortCompactPeers},{"failed",nodeFailed.load()},
        {"network_sent_bytes",networkSentBytes.load()},{"local_proposals",localProposals.load()},
        {"block_tx_step",blockTxStep},{"block_step_wait_ms",blockStepWaitNs.load()/1e6},
        {"block_step_proposals",blockStepProposals.load()},{"block_tail_proposals",blockTailProposals.load()},
        {"send_queued_bytes",sends.bytes},{"send_queued_tasks",sends.tasks},
        {"send_peak_bytes",sends.peakBytes},{"send_peak_tasks",sends.peakTasks},
        {"send_byte_limit",sends.byteLimit},{"send_task_limit",sends.taskLimit},
        {"send_peer_task_limit",sends.peerTaskLimit},{"send_rejected",sends.rejected},
        {"send_control_byte_reserve",sends.controlByteReserve},{"send_control_task_reserve",sends.controlTaskReserve},
        {"send_peer_control_byte_reserve",sends.peerControlByteReserve},{"send_peer_control_task_reserve",sends.peerControlTaskReserve},
        {"send_urgent_messages",prioritySentMessages[0].load()},{"send_transaction_messages",prioritySentMessages[1].load()},
        {"send_bulk_messages",prioritySentMessages[2].load()},{"send_urgent_bytes",prioritySentBytes[0].load()},
        {"send_transaction_bytes",prioritySentBytes[1].load()},{"send_bulk_bytes",prioritySentBytes[2].load()},
        {"gossip_dropped",gossipDropped.load()},{"broadcast_selected",broadcastSelected.load()},{"broadcast_excluded",broadcastExcluded.load()},
        {"block_relay_peers",blockRelayPeers},{"tx_relay_peers",txRelayPeers},{"peer_discovery",peerDiscovery},
        {"target_peers",targetPeers},{"peer_connect_failures",peerConnectFailures.load()},{"backup_connect_attempts",backupConnectAttempts.load()},
        {"early_block_relay",earlyBlockRelay},{"early_relayed_blocks",earlyRelayedBlocks.load()},
        {"pre_execution_relay",preExecutionRelay},{"pre_execution_relay_attempts",preExecutionRelayedBlocks.load()},
        {"pre_execution_relay_limited",preExecutionRelayLimited.load()},
        {"fragment_sent",chain::fragment::sentFrames.load()},{"fragment_received",chain::fragment::receivedFrames.load()},
        {"fragment_completed",chain::fragment::completedMessages.load()},{"fragment_buffered_bytes",chain::fragment::bufferedBytes.load()},
        {"tx_push_peers",txPushPeers},{"tx_inventory_announced",txInventoryAnnounced.load()},
        {"tx_inventory_requests",txInventoryRequests.load()},{"tx_inventory_retries",txInventoryRetries.load()},
        {"tx_inventory_expired",txInventoryExpired.load()},{"tx_inventory_dropped",txInventoryDropped.load()},
        {"tx_inventory_replies",txInventoryReplies.load()},{"tx_inventory_body_sent",txInventoryBodySent.load()},
        {"tx_inventory_hash_sent",txInventoryHashSent.load()},{"tx_inventory_known_skipped",txInventoryKnownSkipped.load()},
        {"compact_prefilled_announcements",compactPrefilledAnnouncements.load()},{"compact_prefilled_transactions",compactPrefilledTransactions.load()},
        {"compact_prefilled_received",compactPrefilledReceived.load()},{"compact_scan_snapshots",compactScanSnapshots.load()},
        {"compact_scan_snapshot_hits",compactScanSnapshotHits.load()},
        {"relayed_transactions",relayedTransactions.load()},{"skipped_relay_transactions",skippedRelayTransactions.load()},
        {"compact_announcements",compactAnnouncements.load()},{"compact_reconstructed",compactReconstructed.load()},
        {"compact_short_announcements",compactShortAnnouncements.load()},{"compact_repair_retries",compactRepairRetries.load()},
        {"compact_full_fallbacks",compactFullFallbacks.load()},{"compact_root_failures",compactRootFailures.load()},
        {"compact_repair_ms",compactRepairMs},
        {"missing_transactions",missingTransactions.load()},{"relay_suppressed",relaySuppressed.load()},
        {"sync_requests",syncRequests.load()},{"sync_retries",syncRetries.load()},{"sync_blocks",syncBlocks.load()},
        {"recent_transaction_hits",recentTransactionHits.load()},
        {"execution_cache_hits",executionCacheHits.load()},{"execution_cache_misses",executionCacheMisses.load()},
        {"execution_prefix_reuse",executionPrefixReuse},{"execution_prefix_hits",executionPrefixHits.load()},
        {"execution_prefix_transactions",executionPrefixTransactions.load()},{"native_execution_threads",nativeExecutionThreads},
        {"native_parallel_blocks",nativeParallelBlocks.load()},{"native_parallel_transactions",nativeParallelTransactions.load()},
        {"candidate_rank_skips",candidateRankSkips.load()},
        {"candidate_execution_cache_hits",candidateExecutionCacheHits.load()},
        {"candidate_executions",candidateExecutions.load()},
        {"candidate_execute_ms",candidateExecuteNs.load()/1e6},{"commit_execute_ms",commitExecuteNs.load()/1e6},
        {"evm_account_writes",evmAccountWrites.load()},{"evm_slot_writes",evmSlotWrites.load()},{"evm_persist_ms",evmPersistNs.load()/1e6},
        {"cold_signature_ms",coldSignatureNs.load()/1e6},{"parallel_signature_blocks",parallelSignatureBlocks.load()},
        {"peak_block_verify_jobs",peakBlockVerifyJobs.load()},
        {"commit_lock_wait_ms",commitLockWaitNs.load()/1e6},{"candidate_lock_wait_ms",candidateLockWaitNs.load()/1e6},
        {"gap_accepted",gapAcceptedTx.load()},{"pool_busy",poolBusyTx.load()},
        {"pending_tx_verify",pendingTxVerify.load()},{"pending_block_verify",pendingBlockVerify.load()},
        {"pending_metadata",pendingMetadata.load()},{"metadata_queue_wait_ms",metadataQueueWaitNs.load()/1e6},
        {"metadata_tasks",metadataTasks.load()},{"block_tasks",blockTasks.load()},
        {"duplicate_block_work",duplicateBlockWork.load()},
        {"pool_refresh_users",poolRefreshUsers.load()},{"pool_refresh_transactions",poolRefreshTx.load()},
        {"tx_queue_wait_ms",txQueueWaitNs.load()/1e6},{"block_queue_wait_ms",blockQueueWaitNs.load()/1e6},
        {"fork_attempts",forkAttempts.load()},{"fork_walk_ms",forkWalkNs.load()/1e6},
        {"fork_lock_wait_ms",forkLockWaitNs.load()/1e6},{"fork_backup_keys",forkBackupKeys.load()},
        {"fork_backup_bytes",forkBackupBytes.load()},{"replayed_transactions",replayedTx.load()},
        {"fork_rank_skips",forkRankSkips.load()},{"fork_parent_skips",forkParentSkips.load()},
        {"branch_append_transactions",branchAppendTx.load()},{"fork_reexecute_transactions",forkReexecuteTx.load()},
        {"grouped_commit_blocks",groupedCommitBlocks.load()},
        {"grouped_commit_batches",groupedCommitBatches.load()},{"fork_schedules",forkSchedules.load()},
        {"fork_selections",forkSelections.load()},{"fork_invalid_candidates",forkInvalidCandidates.load()},
        {"signature_cache_evictions",signatureCacheEvictions.load()},{"signature_shared_checks",signatureSharedChecks.load()},
        {"signature_wait_ms",signatureWaitNs.load()/1e6},{"signature_cache_capacity",maxVerifiedPerBucket*verifiedTransactions.size()},
        {"recent_tx_capacity",maxRecentBlockTransactions},{"recent_tx_bytes_limit",maxRelayTransactionBytes},
        {"sync_owner_skips",syncOwnerSkips.load()},{"sync_window",syncWindow},
        {"pending_queries",pendingQueries.load()},{"query_wait_ms",queryWaitNs.load()/1e6},
        {"pending_block_replies",pendingBlockReplies.load()},{"block_reply_wait_ms",blockReplyWaitNs.load()/1e6},
        {"block_reply_read_ms",blockReplyReadNs.load()/1e6},{"block_reply_tasks",blockReplyTasks.load()},
        {"state_version",snapshot.version},
        {"ingress_wait_ms",ingressWaitNs.load()/1e6},{"clock",GetClockStats()},{"experiment_index",experimentIndex}};
    stats.update(GetProcessResources());
    stats.update(TxBodyWaitStats());
    stats.update(TxWorkStats());
    return stats;
}
#endif
