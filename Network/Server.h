#ifndef CHAIN_SERVER_H
#define CHAIN_SERVER_H
#include "Client.h"
using namespace std;
using namespace boost::asio;

// 每个连接的读、写、关闭都在该连接的 strand 上运行。
awaitable<bool> ReadData(const shared_ptr<Peer>& peer,mutable_buffer data,int timeout=30) {
    if (peer->closed) co_return false;
    peer->deadline.expires_after(seconds(timeout));
    peer->deadline.async_wait([peer](boost::system::error_code ec){if (!ec) ClosePeer(peer);});
    boost::system::error_code ec;
    co_await async_read(peer->socket,data,redirect_error(use_awaitable,ec));
    peer->deadline.cancel();
    if (ec) { ClosePeer(peer); co_return false; }
    co_return true;
}
bool ReserveVerify(size_t num) {
    auto current=pendingVerify.load();
    do {
        if (num>maxPendingVerify||current>maxPendingVerify-num) return false;
    } while (!pendingVerify.compare_exchange_weak(current,current+num));
    auto peak=peakPendingVerify.load();
    while (peak<current+num&&!peakPendingVerify.compare_exchange_weak(peak,current+num)) {}
    return true;
}
awaitable<void> ProcessMessage(const shared_ptr<Peer>& peer,uint8_t type,vector<uint8_t> data) {
    auto num=(type==1||type==10)?data.size()/176:max(size_t(1),(data.size()-112)/176);
    steady_timer timer(co_await this_coro::executor);
    auto queuedAt=GetSteadyTime();
    uint64_t expected=0;
    if (type==1||type==10) firstTxTime.compare_exchange_strong(expected,queuedAt);
    // 队列满时暂停该连接读取，利用 TCP 背压，不无限堆积异步任务。
    while (!ReserveVerify(num)) {
        if (peer->closed||!nodeRunning) co_return;
        timer.expires_after(milliseconds(1));
        co_await timer.async_wait(use_awaitable);
    }
    post(*verifyThreads,[type,num,queuedAt,data=std::move(data)]() mutable {
        queueWaitNs+=GetSteadyTime()-queuedAt;
        try {
            if (type==1||type==10) {
                auto added=ProcessTxPackage(data);
                if (added&&!produceBlocks) BroadcastData(GenerateMessage(10,data));
            } else ProcessBlock(std::move(data));
        } catch (const exception& e) {
            spdlog::error("Verification worker: {}",e.what());
            nodeFailed=true;
            nodeRunning=false;
            if (nodeIO) nodeIO->stop();
        }
        pendingVerify-=num;
        WakeMainLoop();
    });
}
awaitable<void> session(uint64_t key,shared_ptr<Peer> peer) {
    (void)key;
    while (!peer->closed) {
        array<uint8_t,5> header;
        if (!co_await ReadData(peer,buffer(header))) co_return;
        auto type=header[0];
        auto size=ReadU32(header.data()+1);
        if (!VerifyMessageSize(type,size)) { ClosePeer(peer); co_return; }
        vector<uint8_t> data(size);
        if (size&&!co_await ReadData(peer,buffer(data))) co_return;
        networkBytes+=size+5;
        if (type==1||type==10||type==2||type==7) {
            co_await ProcessMessage(peer,type,std::move(data));
        } else if (type==4) {
            SendData(peer,GenerateHeightMessage(5));
        } else if (type==5||type==12) {
            peer->remoteHeight=ReadU64(data.data());
            if (peer->port&&!peer->syncing&&peer->remoteHeight>DBReadBlockHeight()) {
                peer->syncing=true;
                co_spawn(peer->strand,SyncBlock(peer),[peer](exception_ptr error){if (error) PeerError(peer,error);});
            }
        } else if (type==6) {
            SendData(peer,GenerateBlockMessage(ReadU64(data.data())));
        } else if (type==8) {
            SendData(peer,GenerateDiscoverMessage());
        } else if (type==9) {
            co_await ProcessDiscoverMessage(std::move(data));
        } else if (type==11) {
            ProcessTxTimeACKMessage(data,system_clock::now(),peer);
        } else if (type==13) {
            auto stats=GetNodeStats().dump();
            SendData(peer,GenerateMessage(14,span<const uint8_t>(reinterpret_cast<const uint8_t*>(stats.data()),stats.size())));
        }
    }
}
awaitable<void> AcceptPeer(shared_ptr<Peer> peer) {
    array<uint8_t,1> type;
    if (!co_await ReadData(peer,buffer(type),3)) co_return;
    if (type[0]!=1&&type[0]!=2) { ClosePeer(peer); co_return; }
    if (type[0]==1) {
        array<uint8_t,2> port;
        if (!co_await ReadData(peer,buffer(port),3)) co_return;
        peer->port=uint16_t(port[0])|(uint16_t(port[1])<<8);
        if (!peer->port) { ClosePeer(peer); co_return; }
        auto endpoint=peer->socket.remote_endpoint();
        peer->key=GetPeerKey(endpoint.address().to_v4().to_bytes(),endpoint.port());
        peer->address=endpoint.address().to_v4().to_bytes();
        {
            lock_guard lock(peerMutex);
            if (peerPool.size()>=64||peerPool.count(peer->key)) { peer->key=0; co_return; }
            peerPool.emplace(peer->key,peer);
        }
        QueryHeight(peer);
    }
    co_await session(peer->key,peer);
}
awaitable<void> Listen(unsigned short port) {
    auto executor=co_await this_coro::executor;
    tcp::acceptor acceptor(executor,tcp::endpoint(ip::make_address_v4(listenAddress),port));
    spdlog::info("Server started on {}",port);
    while (nodeRunning) {
        boost::system::error_code ec;
        auto socket=co_await acceptor.async_accept(redirect_error(use_awaitable,ec));
        if (ec) {
            if (!nodeRunning) co_return;
            throw boost::system::system_error(ec);
        }
        if (connectionCount>=maxConnections) { socket.close(ec); continue; }
        socket.set_option(tcp::no_delay(true),ec);
        auto peer=make_shared<Peer>(std::move(socket));
        co_spawn(peer->strand,AcceptPeer(peer),[peer](exception_ptr error){PeerError(peer,error);});
    }
}
#endif
