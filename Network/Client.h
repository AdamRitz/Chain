//
// Created by 61485 on 2026/4/30.
//

#ifndef CHAIN_CLIENT_H
#define CHAIN_CLIENT_H
#include <deque>
#include <utility>
#include <iostream>
#include <boost/asio.hpp>
#include <nlohmann/json.hpp>
#include <optional>
#include <spdlog/spdlog.h>
using namespace boost::asio;
using namespace std;
using namespace nlohmann;
using tcp = ip::tcp;

// -----------------------------------------------------------节点结构体/节点池/节点锁初始化---------------------------------------------------------------------------------------------
// 节点结构体
// socket: socket 连接
// strand: asio 用于顺序执行的对象
// messageQueue: 消息队列，为支持并发写入 socket 需要用消息队列，否则需要给 socket 上锁。
// writing: 写入状态符
struct Peer:enable_shared_from_this<Peer>{
    tcp::socket socket;
    strand<any_io_executor> strand;
    deque<vector<uint8_t>> messageQueue;
    bool writing = false;
    Peer(tcp::socket sock) : socket(std::move(sock)), strand(make_strand(socket.get_executor())) {}
};

vector<shared_ptr<Peer>> peerPool;
mutex peerMutex;
// -----------------------------------------------------------连接管理函数-----------------------------------------------------------------------------------------------------------
shared_ptr<Peer> AddPeer(tcp::socket socket) {
    auto peer = make_shared<Peer>(std::move(socket));
    lock_guard lock(peerMutex);
    peerPool.push_back(peer);
    return peer;
}

void AddSocketFromPTR(const shared_ptr<Peer>& s) {
    lock_guard lock(peerMutex);
    peerPool.push_back(s);
}

void RemovePeer(const shared_ptr<Peer>& peer) {
    lock_guard lock(peerMutex);
    erase(peerPool, peer);
}
// -----------------------------------------------------------发送消息函数---------------------------------------------------------------------------------------------

awaitable<void> WriteLoop(shared_ptr<Peer> peer) {
    while (!peer->messageQueue.empty()) {
        boost::system::error_code ec;
        co_await async_write(peer->socket, buffer(peer->messageQueue.front()), redirect_error(use_awaitable, ec));
        if (ec) {
            spdlog::info("Send failed: {}", ec.message());
            boost::system::error_code ignored;
            peer->socket.close(ignored);
            peer->messageQueue.clear();
            peer->writing = false;
            co_return;
        }
        peer->messageQueue.pop_front();
    }
    peer->writing = false;
}

awaitable<void> QueueSend(shared_ptr<Peer> peer, std::vector<uint8_t> data) {
    peer->messageQueue.push_back(std::move(data));
    if (!peer->writing) {
        peer->writing = true;
        co_spawn(peer->strand, WriteLoop(peer), detached);
    }
    co_return;
}

void SendData(std::shared_ptr<Peer> peer, std::vector<uint8_t> data) {
    co_spawn(peer->strand, QueueSend(peer, data), detached);
}

awaitable<void> BroadcastData(vector<uint8_t> byte) {
    vector<shared_ptr<Peer>> peers;
    {
        lock_guard lock(peerMutex);
        peers = peerPool;
    }
    for (auto& peeer : peers) {
        SendData(peeer, byte);
    }
    co_return;
}
// -----------------------------------------------------------客户端入口函数---------------------------------------------------------------------------------------------
awaitable<void> Connect(string ipaddr,unsigned short port) {
    auto executor = co_await this_coro::executor;
    boost::system::error_code ec;
    tcp::socket socket(executor);

    tcp::endpoint endpoint(ip::make_address(ipaddr,ec),port);
    if (ec) {spdlog::info(ec.message());co_return;}
    co_await socket.async_connect(endpoint,redirect_error(use_awaitable, ec));
    if (ec) {spdlog::info(ec.message());co_return;}
    AddPeer(move(socket));
    co_return;
}
awaitable<void> ConnectSeed() {
    co_await Connect("127.0.0.1",8089);
    // 以下为测试代码
    auto executor = co_await this_coro::executor;
    steady_timer timer(executor);
    for (;;) {
        vector<uint8_t> byte={1,2,3};
        co_await BroadcastData(byte);
        timer.expires_after(std::chrono::seconds(1));
        co_await timer.async_wait(use_awaitable);
    }
    cout<<"Message Send";
    co_return;
}
// -----------------------------------------------------------客户端定时函数---------------------------------------------------------------------------------------------

awaitable<void> QueryHeight(shared_ptr<Peer>& peer) {
    vector<uint8_t> messageByte={4};
    SendData(peer,messageByte);
}
awaitable<void> QueryBlock( shared_ptr<Peer>& peer,uint64_t blockNum) {
    vector<uint8_t> messageByte;
    messageByte.resize(9);
    uint8_t type = 6;
    memcpy(messageByte.data(),&type,1);
    memcpy(messageByte.data(),&blockNum,8);
    SendData(peer,messageByte);
}

awaitable<void> SyncBlock() {
    co_await QueryHeight(peerPool[0]);
    auto maxHeight = DBReadBlockMaxHeight();
    auto currentHeight = DBReadBlockHeight();
    if (maxHeight>currentHeight) {
        while (currentHeight<maxHeight) {
            QueryBlock(peerPool[0],currentHeight+1);
            currentHeight +=1;
        }
    }
}
#endif //CHAIN_CLIENT_H