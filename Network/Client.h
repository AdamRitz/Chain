#ifndef CHAIN_CLIENT_H
#define CHAIN_CLIENT_H
#include <utility>
#include <boost/asio.hpp>
#include <deque>
#include <thread>
#include <nlohmann/json.hpp>
#include "../Transaction/Block.h"
#include "../Message/Message.h"
#include "../Tool/Resource.h"
using namespace std;
using namespace std::chrono;
using namespace boost::asio;
using tcp=ip::tcp;

io_context* nodeIO=nullptr;
unique_ptr<thread_pool> verifyThreads;
unique_ptr<thread_pool> candidateThreads;
atomic<size_t> pendingTxVerify{0},pendingBlockVerify{0},pendingTxBytes{0},pendingBlockBytes{0};
atomic<uint64_t> txQueueWaitNs{0},blockQueueWaitNs{0};
atomic<size_t> pendingVerify{0},peakPendingVerify{0},connectionCount{0};
atomic<size_t> pendingVerifyBytes{0};
atomic<uint64_t> queueWaitNs{0},networkBytes{0};
size_t maxPendingVerify=32768;
size_t maxConnections=128;
size_t maxPeers=64;
vector<pair<string,uint16_t>> configuredPeers;
bool relayTransactions=false;
atomic<uint64_t> networkSentBytes{0},localProposals{0};
size_t maxSendBytes=8*1024*1024;
unsigned short listenPort=8089;
string listenAddress="127.0.0.1";
int blockInterval=50;
bool produceBlocks=true;

// ------------------------------------------------节点和发送队列------------------------------------------------
struct Peer:enable_shared_from_this<Peer> {
    tcp::socket socket;
    boost::asio::strand<any_io_executor> strand;
    steady_timer deadline;
    deque<shared_ptr<const vector<uint8_t>>> messageQueue;
    size_t queuedBytes=0;
    uint64_t key=0,remoteHeight=0;
    uint16_t port=0;
    array<uint8_t,4> address{};
    array<uint8_t,32> requestedHead{};
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
awaitable<void> session(uint64_t key,shared_ptr<Peer> peer);
awaitable<void> SyncBlock(shared_ptr<Peer> peer);

uint64_t GetPeerKey(const array<uint8_t,4>& ipByte,uint16_t port) {
    return uint64_t(ipByte[0])|(uint64_t(ipByte[1])<<8)|(uint64_t(ipByte[2])<<16)|(uint64_t(ipByte[3])<<24)|(uint64_t(port)<<32);
}
pair<uint64_t,shared_ptr<Peer>> AddPeer(tcp::socket socket,uint16_t port=0) {
    auto endpoint=socket.remote_endpoint();
    auto key=GetPeerKey(endpoint.address().to_v4().to_bytes(),endpoint.port());
    lock_guard lock(peerMutex);
    if (peerPool.size()>=maxPeers||peerPool.count(key)) return {key,nullptr};
    auto peer=make_shared<Peer>(std::move(socket));
    peer->key=key;
    peer->port=port;
    peer->address=endpoint.address().to_v4().to_bytes();
    peerPool.emplace(key,peer);
    return {key,peer};
}
void RemovePeer(uint64_t key) {
    lock_guard lock(peerMutex);
    peerPool.erase(key);
}
void ClosePeer(const shared_ptr<Peer>& peer) {
    if (peer->closed) return;
    peer->closed=true;
    boost::system::error_code ignored;
    peer->deadline.cancel();
    peer->socket.close(ignored);
    peer->messageQueue.clear();
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
    while (!peer->closed&&!peer->messageQueue.empty()) {
        auto data=peer->messageQueue.front();
        boost::system::error_code ec;
        co_await async_write(peer->socket,buffer(*data),redirect_error(use_awaitable,ec));
        if (ec||peer->closed) { ClosePeer(peer); co_return; }
        networkSentBytes+=data->size();
        peer->queuedBytes-=data->size();
        peer->messageQueue.pop_front();
    }
    peer->writing=false;
}
void QueueData(const shared_ptr<Peer>& peer,shared_ptr<const vector<uint8_t>> data) {
        if (peer->closed) return;
        if (data->size()>maxSendBytes||peer->queuedBytes>maxSendBytes-data->size()) { ClosePeer(peer); return; }
        peer->queuedBytes+=data->size();
        peer->messageQueue.push_back(data);
        if (!peer->writing) {
            peer->writing=true;
            co_spawn(peer->strand,WriteLoop(peer),[peer](exception_ptr error){if (error) PeerError(peer,error);});
        }
}
void SendData(const shared_ptr<Peer>& peer,shared_ptr<const vector<uint8_t>> data) {
    if (!peer||!data||data->empty()) return;
    post(peer->strand,[peer,data=std::move(data)] { QueueData(peer,data); });
}
void SendData(const shared_ptr<Peer>& peer,vector<uint8_t> data) {
    SendData(peer,make_shared<const vector<uint8_t>>(std::move(data)));
}
// 普通函数只负责投递，调用它就会安排广播；大缓冲区在节点间共享。
void BroadcastData(vector<uint8_t> data,uint64_t source=0) {
    auto shared=make_shared<const vector<uint8_t>>(std::move(data));
    vector<shared_ptr<Peer>> peers;
    {
        lock_guard lock(peerMutex);
        for (const auto& [key,peer]:peerPool) peers.push_back(peer);
    }
    unordered_set<uint64_t> sent;
    for (const auto& peer:peers) {
        auto id=GetPeerKey(peer->address,peer->port);
        if ((!source||id!=source)&&sent.insert(id).second) SendData(peer,shared);
    }
}
awaitable<void> ProcessMessage(shared_ptr<Peer> peer,uint8_t type,vector<uint8_t> data);
#include "Blocks.h"
void QueryHeight(const shared_ptr<Peer>& peer) { SendData(peer,GenerateMessage(19,{})); }
void QueryBlock(const shared_ptr<Peer>& peer,uint64_t height) {
    array<uint8_t,8> data;
    WriteU64(data.data(),height);
    SendData(peer,GenerateMessage(6,data));
}

// ------------------------------------------------连接、发现和同步------------------------------------------------
awaitable<void> Connect(array<uint8_t,4> ipByte,uint16_t port) {
    auto executor=co_await this_coro::executor;
    if (!port||connectionCount>=maxConnections) co_return;
    {
        lock_guard lock(peerMutex);
        for (const auto& [key,peer]:peerPool) {
            if (peer->port==port&&peer->address==ipByte) co_return;
        }
    }
    auto peer=make_shared<Peer>(tcp::socket(executor));
    boost::system::error_code ec;
    peer->deadline.expires_after(seconds(5));
    peer->deadline.async_wait([peer](boost::system::error_code error){if (!error) ClosePeer(peer);});
    co_await peer->socket.async_connect(tcp::endpoint(ip::address_v4(ipByte),port),redirect_error(use_awaitable,ec));
    peer->deadline.cancel();
    if (ec) co_return;
    peer->socket.set_option(tcp::no_delay(true),ec);
    array<uint8_t,3> hello{3,uint8_t(listenPort),uint8_t(listenPort>>8)};
    co_await async_write(peer->socket,buffer(hello),redirect_error(use_awaitable,ec));
    if (ec) co_return;
    auto endpoint=peer->socket.remote_endpoint();
    peer->key=GetPeerKey(ipByte,endpoint.port());
    peer->port=port;
    peer->address=ipByte;
    peer->compact=true;
    {
        lock_guard lock(peerMutex);
        if (peerPool.size()>=maxPeers||peerPool.count(peer->key)) co_return;
        peerPool.emplace(peer->key,peer);
    }
    co_spawn(peer->strand,session(peer->key,peer),[peer](exception_ptr error){PeerError(peer,error);});
    QueryHeight(peer);
}
awaitable<void> ConnectSeed(const string& host="127.0.0.1",uint16_t port=8089) {
    co_await Connect(ip::make_address_v4(host).to_bytes(),port);
}
vector<uint8_t> GenerateDiscoverMessage() {
    vector<uint8_t> data;
    lock_guard lock(peerMutex);
    data.reserve(peerPool.size()*6);
    for (const auto& [key,peer]:peerPool) {
        if (!peer->port) continue;
        auto addr=peer->address;
        data.insert(data.end(),addr.begin(),addr.end());
        data.push_back(uint8_t(peer->port));
        data.push_back(uint8_t(peer->port>>8));
    }
    return GenerateMessage(9,data);
}
awaitable<void> ProcessDiscoverMessage(vector<uint8_t> data) {
    if (data.size()%6!=0||data.size()>1024*6) co_return;
    auto executor=co_await this_coro::executor;
    for (size_t offset=0;offset<data.size();offset+=6) {
        array<uint8_t,4> addr;
        memcpy(addr.data(),data.data()+offset,4);
        auto port=uint16_t(data[offset+4])|(uint16_t(data[offset+5])<<8);
        if (ip::address_v4(addr).is_loopback()&&port==listenPort) continue;
        co_spawn(executor,Connect(addr,port),[](exception_ptr error) {
            if (error) { try { rethrow_exception(error); } catch (const exception& e) { spdlog::warn("Connect: {}",e.what()); } }
        });
    }
}
awaitable<void> SyncBlock(shared_ptr<Peer> peer) {
    steady_timer timer(co_await this_coro::executor);
    uint64_t requested=DBReadBlockHeight(),progress=requested;
    auto lastProgress=steady_clock::now();
    unsigned retries=0;
    while (!peer->closed&&DBReadBlockHeight()<peer->remoteHeight) {
        auto height=DBReadBlockHeight();
        if (height!=progress) { progress=height; lastProgress=steady_clock::now(); retries=0; }
        if (steady_clock::now()-lastProgress>seconds(3)) {
            if (++retries>3) break;
            requested=height; lastProgress=steady_clock::now(); syncRetries++;
            peer->requestedHead={}; QueryHeight(peer);
        }
        requested=max(requested,height);
        auto limit=min(peer->remoteHeight,height+8);
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
    peer->syncing=false;
}
awaitable<void> OpenDiscoveryMode() {
    steady_timer timer(co_await this_coro::executor);
    while (nodeRunning) {
        PruneCompactBlocks();
        vector<shared_ptr<Peer>> peers;
        {
            lock_guard lock(peerMutex);
            for (const auto& [key,peer]:peerPool) peers.push_back(peer);
        }
        for (const auto& peer:peers) QueryHeight(peer);
        for (const auto& [host,port]:configuredPeers) co_await Connect(ip::make_address_v4(host).to_bytes(),port);
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
void MainLoop() {
    try {
        int64_t lastRound=-1;
        while (true) {
            if (ProcessForks()) AnnounceBlock(DBReadBlockByHash(DBReadCurrentBlock()));
            {
                unique_lock lock(txpoolMutex);
                txpoolCondition.wait(lock,[]{return !nodeRunning||forkReady||blockReady||(produceBlocks&&readyPoolTx>0);});
                if (forkReady) continue;
                if (!nodeRunning&&!blockReady&&(!produceBlocks||txpool.empty())) break;
                if (!nodeClock.roundMs&&produceBlocks&&!blockReady&&nodeRunning&&readyPoolTx<maxBlockTx) {
                    txpoolCondition.wait_for(lock,milliseconds(blockInterval),[]{return !nodeRunning||forkReady||blockReady||readyPoolTx>=maxBlockTx;});
                }
                if (forkReady) continue;
            }
            if (nodeClock.roundMs&&nodeRunning) {
                auto period=int64_t(nodeClock.roundMs)*1000;
                auto round=max(lastRound+1,GetLogicalTimeUs()/period+1);
                WaitLogicalTime(round*period);
                lastRound=round;
                if (produceBlocks) {
                    auto data=GenerateBlock();
                    if (!data.empty()&&ProcessBlock(data)) { localProposals++; AnnounceBlock(data); }
                }
                WaitLogicalTime(round*period+int64_t(nodeClock.candidateMs)*1000);
            } else if (produceBlocks&&!blockReady) {
                auto data=GenerateBlock();
                if (data.empty()&&!nodeRunning) break;
                if (!data.empty()) { localProposals++; ProcessBlock(std::move(data)); }
            }
            pair<Block,vector<uint8_t>> selected;
            {
                lock_guard lock(blockBufferLock);
                selected=std::move(BlockBuffer[0]);
                BlockBuffer[0]={};
                blockReady=false;
            }
            if (selected.second.empty()) continue;
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
    {
        lock_guard lock(txpoolMutex);
        poolSize=txpool.size();
        readySize=readyPoolTx;
    }
    {
        lock_guard lock(peerMutex);
        peers=peerPool.size();
    }
    auto first=firstTxTime.load();
    auto last=lastCommitTime.load();
    double elapsed=last>first&&first?double(last-first)/1e9:0;
    shared_lock lock(dbCommitMutex);
    nlohmann::json stats={{"received",receivedTx.load()},{"valid",validTx.load()},{"invalid",invalidTx.load()},
        {"duplicate",duplicateTx.load()},{"rejected",rejectedTx.load()},{"committed",committedTx.load()},
        {"pool",poolSize},{"ready_pool",readySize},{"pending",pendingVerify.load()},{"peak_pending",peakPendingVerify.load()},
        {"blocks",blockCount.load()},{"height",DBReadBlockHeight()},{"head",U32ToHex(DBReadCurrentBlock())},
        {"elapsed_seconds",elapsed},{"local_commit_tps",elapsed>0?committedTx.load()/elapsed:0},
        {"verify_worker_ms",verifyNs.load()/1e6},{"pool_worker_ms",poolNs.load()/1e6},
        {"signature_checks",signatureChecks.load()},{"signature_cache_hits",signatureCacheHits.load()},
        {"duplicate_blocks",duplicateBlocks.load()},{"pool_refresh_ms",poolRefreshNs.load()/1e6},
        {"tx_commit_wait_ms",txCommitWaitNs.load()/1e6},{"tx_read_ms",txReadNs.load()/1e6},
        {"tx_pool_wait_ms",txPoolWaitNs.load()/1e6},{"block_commit_ms",blockCommitNs.load()/1e6},
        {"queue_wait_ms",queueWaitNs.load()/1e6},{"block_build_ms",blockBuildNs.load()/1e6},
        {"block_verify_ms",blockVerifyNs.load()/1e6},{"db_write_ms",dbWriteNs.load()/1e6},
        {"db_writes",dbWriteCount.load()},{"network_bytes",networkBytes.load()},{"sync",dbSync},
        {"bloom_useful",options.statistics->getTickerCount(rocksdb::BLOOM_FILTER_USEFUL)},
        {"users",users.size()},{"base_fee",baseFee},{"user_burned",userBurned},
        {"user_supply",genesisSupply-userBurned},{"invalid_user_tx",invalidUserTx.load()},
        {"conflict_tx",conflictTx.load()},{"invalid_user_blocks",invalidUserBlocks.load()},
        {"user_check_ms",userCheckNs.load()/1e6},{"user_writes",userWriteCount.load()},
        {"chain_transactions",DBReadNumber("ChainTx")},{"reorganizations",reorgCount.load()},{"max_reorg_depth",maxReorgDepth.load()},
        {"evm_accounts",evmState.get_accounts().size()}, {"peers",peers},{"failed",nodeFailed.load()},
        {"network_sent_bytes",networkSentBytes.load()},{"local_proposals",localProposals.load()},
        {"compact_announcements",compactAnnouncements.load()},{"compact_reconstructed",compactReconstructed.load()},
        {"missing_transactions",missingTransactions.load()},{"relay_suppressed",relaySuppressed.load()},
        {"sync_requests",syncRequests.load()},{"sync_retries",syncRetries.load()},{"sync_blocks",syncBlocks.load()},
        {"recent_transaction_hits",recentTransactionHits.load()},
        {"execution_cache_hits",executionCacheHits.load()},{"execution_cache_misses",executionCacheMisses.load()},
        {"candidate_execute_ms",candidateExecuteNs.load()/1e6},{"commit_execute_ms",commitExecuteNs.load()/1e6},
        {"evm_account_writes",evmAccountWrites.load()},{"evm_slot_writes",evmSlotWrites.load()},{"evm_persist_ms",evmPersistNs.load()/1e6},
        {"cold_signature_ms",coldSignatureNs.load()/1e6},{"parallel_signature_blocks",parallelSignatureBlocks.load()},
        {"peak_block_verify_jobs",peakBlockVerifyJobs.load()},
        {"commit_lock_wait_ms",commitLockWaitNs.load()/1e6},{"candidate_lock_wait_ms",candidateLockWaitNs.load()/1e6},
        {"gap_accepted",gapAcceptedTx.load()},{"pool_busy",poolBusyTx.load()},
        {"pending_tx_verify",pendingTxVerify.load()},{"pending_block_verify",pendingBlockVerify.load()},
        {"tx_queue_wait_ms",txQueueWaitNs.load()/1e6},{"block_queue_wait_ms",blockQueueWaitNs.load()/1e6},
        {"fork_attempts",forkAttempts.load()},{"fork_walk_ms",forkWalkNs.load()/1e6},
        {"fork_lock_wait_ms",forkLockWaitNs.load()/1e6},{"fork_backup_keys",forkBackupKeys.load()},
        {"fork_backup_bytes",forkBackupBytes.load()},{"replayed_transactions",replayedTx.load()},
        {"clock",GetClockStats()},{"experiment_index",experimentIndex}};
    lock.unlock();
    stats.update(GetProcessResources());
    return stats;
}
#endif
