#ifndef CHAIN_CLIENT_H
#define CHAIN_CLIENT_H
#include <utility>
#include <boost/asio.hpp>
#include <deque>
#include <thread>
#include <nlohmann/json.hpp>
#include "../Transaction/Block.h"
#include "../Message/Message.h"
using namespace std;
using namespace std::chrono;
using namespace boost::asio;
using tcp=ip::tcp;

io_context* nodeIO=nullptr;
unique_ptr<thread_pool> verifyThreads;
atomic<size_t> pendingVerify{0},peakPendingVerify{0},connectionCount{0};
atomic<uint64_t> queueWaitNs{0},networkBytes{0};
size_t maxPendingVerify=32768;
size_t maxConnections=128;
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
    bool writing=false,closed=false,syncing=false;
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
    if (peerPool.size()>=64||peerPool.count(key)) return {key,nullptr};
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
        peer->queuedBytes-=data->size();
        peer->messageQueue.pop_front();
    }
    peer->writing=false;
}
void SendData(const shared_ptr<Peer>& peer,shared_ptr<const vector<uint8_t>> data) {
    if (!peer||!data||data->empty()) return;
    post(peer->strand,[peer,data=std::move(data)] {
        if (peer->closed) return;
        if (data->size()>maxSendBytes||peer->queuedBytes>maxSendBytes-data->size()) { ClosePeer(peer); return; }
        peer->queuedBytes+=data->size();
        peer->messageQueue.push_back(data);
        if (!peer->writing) {
            peer->writing=true;
            co_spawn(peer->strand,WriteLoop(peer),[peer](exception_ptr error){if (error) PeerError(peer,error);});
        }
    });
}
void SendData(const shared_ptr<Peer>& peer,vector<uint8_t> data) {
    SendData(peer,make_shared<const vector<uint8_t>>(std::move(data)));
}
// 普通函数只负责投递，调用它就会安排广播；大缓冲区在节点间共享。
void BroadcastData(vector<uint8_t> data) {
    auto shared=make_shared<const vector<uint8_t>>(std::move(data));
    vector<shared_ptr<Peer>> peers;
    {
        lock_guard lock(peerMutex);
        for (const auto& [key,peer]:peerPool) peers.push_back(peer);
    }
    for (const auto& peer:peers) SendData(peer,shared);
}
void QueryHeight(const shared_ptr<Peer>& peer) { SendData(peer,GenerateMessage(4,{})); }
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
    array<uint8_t,3> hello{1,uint8_t(listenPort),uint8_t(listenPort>>8)};
    co_await async_write(peer->socket,buffer(hello),redirect_error(use_awaitable,ec));
    if (ec) co_return;
    auto endpoint=peer->socket.remote_endpoint();
    peer->key=GetPeerKey(ipByte,endpoint.port());
    peer->port=port;
    peer->address=ipByte;
    {
        lock_guard lock(peerMutex);
        if (peerPool.size()>=64||peerPool.count(peer->key)) co_return;
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
    if (data.size()%6!=0||data.size()>64*6) co_return;
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
    while (!peer->closed&&DBReadBlockHeight()<peer->remoteHeight) {
        auto height=DBReadBlockHeight()+1;
        QueryBlock(peer,height);
        auto end=steady_clock::now()+seconds(5);
        while (!peer->closed&&DBReadBlockHeight()<height&&steady_clock::now()<end) {
            timer.expires_after(milliseconds(10));
            co_await timer.async_wait(use_awaitable);
        }
        if (DBReadBlockHeight()<height) break;
    }
    peer->syncing=false;
}
awaitable<void> OpenDiscoveryMode() {
    steady_timer timer(co_await this_coro::executor);
    while (nodeRunning) {
        vector<shared_ptr<Peer>> peers;
        {
            lock_guard lock(peerMutex);
            for (const auto& [key,peer]:peerPool) peers.push_back(peer);
        }
        for (const auto& peer:peers) QueryHeight(peer);
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
        while (true) {
            {
                unique_lock lock(txpoolMutex);
                txpoolCondition.wait(lock,[]{return !nodeRunning||blockReady||(produceBlocks&&!txpool.empty());});
                if (!nodeRunning&&!blockReady&&(!produceBlocks||txpool.empty())) break;
                if (produceBlocks&&!blockReady&&nodeRunning&&txpool.size()<maxBlockTx) {
                    txpoolCondition.wait_for(lock,milliseconds(blockInterval),[]{return !nodeRunning||blockReady||txpool.size()>=maxBlockTx;});
                }
            }
            if (produceBlocks&&!blockReady) {
                auto data=GenerateBlock();
                if (!data.empty()) ProcessBlock(std::move(data));
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
            BroadcastData(GenerateNewBlockMessage(selected.second));
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
    size_t peers;
    {
        lock_guard lock(txpoolMutex);
        poolSize=txpool.size();
    }
    {
        lock_guard lock(peerMutex);
        peers=peerPool.size();
    }
    auto first=firstTxTime.load();
    auto last=lastCommitTime.load();
    double elapsed=last>first&&first?double(last-first)/1e9:0;
    lock_guard lock(dbCommitMutex);
    return {{"received",receivedTx.load()},{"valid",validTx.load()},{"invalid",invalidTx.load()},
        {"duplicate",duplicateTx.load()},{"rejected",rejectedTx.load()},{"committed",committedTx.load()},
        {"pool",poolSize},{"pending",pendingVerify.load()},{"peak_pending",peakPendingVerify.load()},
        {"blocks",blockCount.load()},{"height",DBReadBlockHeight()},{"head",U32ToHex(DBReadCurrentBlock())},
        {"elapsed_seconds",elapsed},{"local_commit_tps",elapsed>0?committedTx.load()/elapsed:0},
        {"verify_worker_ms",verifyNs.load()/1e6},{"pool_worker_ms",poolNs.load()/1e6},
        {"tx_commit_wait_ms",txCommitWaitNs.load()/1e6},{"tx_read_ms",txReadNs.load()/1e6},
        {"tx_pool_wait_ms",txPoolWaitNs.load()/1e6},{"block_commit_ms",blockCommitNs.load()/1e6},
        {"queue_wait_ms",queueWaitNs.load()/1e6},{"block_build_ms",blockBuildNs.load()/1e6},
        {"block_verify_ms",blockVerifyNs.load()/1e6},{"db_write_ms",dbWriteNs.load()/1e6},
        {"db_writes",dbWriteCount.load()},{"network_bytes",networkBytes.load()},{"sync",dbSync},
        {"bloom_useful",options.statistics->getTickerCount(rocksdb::BLOOM_FILTER_USEFUL)},
        {"peers",peers},{"failed",nodeFailed.load()}};
}
#endif
