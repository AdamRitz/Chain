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
#include "../Transaction/Block.h"
#include "../Message/Message.h"

using namespace boost::asio;
using namespace std;
using namespace std::chrono;
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
    system_clock::time_point T1;
    long long lag;
    bool writing = false;
    Peer(tcp::socket sock) : socket(std::move(sock)), strand(make_strand(socket.get_executor())) {}
};

unordered_map<uint64_t,shared_ptr<Peer>> peerPool;
mutex peerMutex;

// -----------------------------------------------------------连接管理函数-----------------------------------------------------------------------------------------------------------
pair<uint64_t,shared_ptr<Peer>> AddPeer(tcp::socket socket) {
    auto ip = socket.remote_endpoint().address().to_v4().to_bytes();
    uint16_t port = socket.remote_endpoint().port();
    uint64_t key =0;
    memcpy(&key, &ip, 4);
    memcpy(&key+4, &port, 2);
    lock_guard lock(peerMutex);
    if (peerPool.count(key)!=0 )return make_pair(key,nullptr);
    auto peer = make_shared<Peer>(std::move(socket));
    peerPool.emplace(key,peer);
    return make_pair(key,peer);
}

void RemovePeer(const uint64_t key) {
    lock_guard lock(peerMutex);
    peerPool.erase(key);
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
    unordered_map<uint64_t,shared_ptr<Peer>> peers;
    {
        lock_guard lock(peerMutex);
        peers = peerPool;
    }
    for (auto& peer : peers) {
        SendData(peer.second, byte);
    }
    co_return;
}
// -----------------------------------------------------------客户端入口函数---------------------------------------------------------------------------------------------
awaitable<void> Connect(array<uint8_t, 4> ipByte,uint16_t port) {
    // 初始化 ec
    boost::system::error_code ec;
    // 创造地址
    ip::address_v4 addr(ipByte);
    tcp::endpoint endpoint(addr,port);
    if (ec) {spdlog::info(ec.message());co_return;}
    // 检查重连逻辑
    auto ip = endpoint.address().to_v4().to_bytes();
    uint64_t key =0;
    memcpy(&key, &ip, 4);
    memcpy(&key+4, &port, 2);
    lock_guard lock(peerMutex);
    if (peerPool.count(key)!=0)co_return;
    // 初始化 executor 和 ec
    auto executor = co_await this_coro::executor;

    tcp::socket socket(executor);
    co_await socket.async_connect(endpoint,redirect_error(use_awaitable, ec));
    if (ec) {spdlog::info(ec.message());co_return;}
    array<uint8_t, 1> data;
    data[0] = 1;
    co_await async_write(socket,buffer(data),redirect_error(use_awaitable,ec));
    AddPeer(move(socket));
    co_return;
}
awaitable<void> ConnectSeed() {
    co_await Connect(ip::make_address("127.0.0.1").to_v4().to_bytes(),8089);
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

// -----------------------------------------------------------消息函数---------------------------------------------------------------------------------------------
// 节点发现消息生成函数：当一个节点向本节点请求节点信息时候，返回此数据。
// 主要作用为打包节点池的数据，序列化为 vector。
vector<uint8_t> GenerateDiscoverMessage() {
    // 消息头填充
    vector<uint8_t> message;
    lock_guard lock(peerMutex);
    uint32_t size = peerPool.size()*6;
    message.resize(size);

    int offset = 0;
    uint8_t type = 9;
    memcpy(message.data(),&type,1);
    offset += 1;
    memcpy(message.data(),&size,4);
    offset += 4;

    // 消息体填充
    for (auto i : peerPool) {
        memcpy(message.data()+offset,i.second->socket.remote_endpoint().address().to_v4().to_bytes().data(),4);
        offset += 4;
        uint16_t port  = i.second->socket.remote_endpoint().port();
        memcpy(message.data()+offset,&port,2);
        offset += 2;
    }
    return message;
}
awaitable<void> ProcessDiscoverMessage(vector<uint8_t> message) {
    auto executor = co_await this_coro::executor;
    auto len = message.size();
    if (len%6!=0) {
        co_return;
    }
    int offset = 0;
    for (int i=0;i<=len/6-1;i++) {
        array<uint8_t,4> ipByte;
        uint16_t port;
        memcpy(ipByte.data(),message.data()+offset,4);
        memcpy(&port,message.data()+offset+4,2);
        offset+=6;
        co_spawn(executor,Connect(ipByte,port));
    }
}
vector<uint8_t> GenerateTxTimeMessage() {
    // 复制交易池
    unordered_map<array<uint8_t, 32>, array<uint8_t, 176>, GetMapHash> txPoolCopy;
    vector<uint8_t> message;
    {
        lock_guard lock(txpoolMutex);
        txPoolCopy = txpool;
    }
    int num = txPoolCopy.size();
    message.resize(5+num*176);
    // 填充 type
    uint8_t type = 10;
    int offset = 0;
    memcpy(message.data()+offset,&type,1);
    offset += 1;
    // 填充 length
    uint32_t length=64;
    memcpy(message.data()+offset,&length,4);
    // 填充消息体
    for (auto pair: txPoolCopy) {
        memcpy(message.data()+offset,pair.second.data(),pair.second.size());
        offset += pair.second.size();
    }
    return message;
}
// 填充交易
void ProcessTxTimeACKMessage(vector<uint8_t> message,system_clock::time_point T4,shared_ptr<Peer> peer) {
    long long T2Data,T3Data;
    memcpy(&T2Data,message.data(),8);
    memcpy(&T3Data,message.data()+8,8);
    system_clock::time_point T2{milliseconds(T2Data)};
    system_clock::time_point T3{milliseconds(T3Data)};
    auto duration = (T2-peer->T1+T3-T4).count()/2;
    peer->lag=duration;
}

// -----------------------------------------------------------循环函数---------------------------------------------------------------------------------------------
// 需要实现的有：过时的区块不再接收 - 过时的定义为 如当前高度为 L，则小于等于 L 的区块都不接收 逻辑已经实现在 ProcessBlock
//
void OpenShareTxTimeMode() {
    while (true) {
        unordered_map<uint64_t,shared_ptr<Peer>> peers;
        {
            lock_guard lock(peerMutex);
            peers = peerPool;
        }
        for (auto i : peers) {
            SendData(i.second,GenerateTxTimeMessage());
            i.second->T1=system_clock::now();
        }
        sleep(2);
    }
}
void MainLoop() {

    auto bias = steady_clock::now()-steady_clock::now();
    while (true) {
        // 等待 200 ms
        auto end = steady_clock::now() + milliseconds(200);
        while (steady_clock::now()+bias < end) {
            this_thread::sleep_for(milliseconds(10));
        }

        // 500 ms 处理区块
        end = steady_clock::now() + milliseconds(500);
        pair<Block,vector<uint8_t>> block;
        // 更新当前区块 Hash 和 高度
        {
            lock_guard lock(blockBufferLock);
            block = BlockBuffer[0];
            BlockBuffer[0]=BlockBuffer[1];
            BlockBuffer[1]=BlockBuffer[2];
        }
        // 验证 previousHash
        array<uint8_t, 32> previousHash=DBReadCurrentBlock();
        if (block.first.previousHash==previousHash) {
            DBWriteBlockHeight(block.first.height);
            auto height  =block.first.height ;
            DBWriteCurrentBlock(block.first.hash);
            // 写入区块
            DBWriteBlockALL(block.first.hash,block.second);
            int num = 0;
            // 写入交易
            for (auto tx : block.first.txs) {
                num++;
                array<uint8_t,32> txHash;
                memcpy(txHash.data(),tx.data()+80,32);
                DBWriteTx(txHash,tx);
            }
            spdlog::info("New Block Confirmed! Height:{},TxNum:{},Hash:{}",block.first.height,num,U32ToHex(block.first.hash));
            while (steady_clock::now() < end) {
                // 等待时间流逝
            }
        }
        bias = steady_clock::now()-steady_clock::now();
        if (steady_clock::now() > end) {
            bias = bias + (steady_clock::now()-end);
        }
        while (steady_clock::now() < end) {
            this_thread::sleep_for(milliseconds(10));
        }

    }
}

void OpenDiscoveryMode() {
    while (true) {
        bool enough = true;
        {
            lock_guard lock(peerMutex);
            if (!peerPool.size()<10) {
                enough = false;
            }
        }
        if (enough == false) {
            unordered_map<uint64_t,shared_ptr<Peer>> peers;
            {
                lock_guard lock(peerMutex);
                peers = peerPool;
            }
            for (auto i : peers) {
                SendData(i.second,GenerateRequestDiscoveryMessage());
            }
            {
                lock_guard lock(peerMutex);
                if (!peerPool.size()>20) break;
            }
        }
        sleep(60);
    }
}

#endif //CHAIN_CLIENT_H