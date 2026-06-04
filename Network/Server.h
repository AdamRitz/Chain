//
// Created by 61485 on 2026/4/30.
//

#ifndef CHAIN_SERVER_H
#define CHAIN_SERVER_H
#include <utility>
#include <iostream>
#include <boost/asio.hpp>
#include <nlohmann/json.hpp>
#include "../Message/Message.h"
#include "Client.h"
#include "../Transaction/Block.h"
#include "../Transaction/Transaction.h"
#include "../Crypto/VRF.h"

using namespace boost::asio;
using namespace std;
using namespace nlohmann;
using tcp = ip::tcp;


// ------------------------------------------------------------------监听函数--------------------------------------------------------------------------------------------------------
awaitable<void> session(uint64_t key,shared_ptr<Peer> peer) {
    array<uint8_t, 5> header;

    for (;;) {
        boost::system::error_code ec;
        // 读取消息头：类型和消息长度
        // 消息类型大小为 1 字节 uint8_t
        // 消息大小字段为 4 字节 uint32_t
        // 此处必须用 async_read 而不能用 async_read_some ，后者只是从 Socket 读取一些数据就返回（TCP 半包问题）。前者是必须读取到多少字节才返回。
        co_await async_read(peer->socket,buffer(header), redirect_error(use_awaitable, ec));
        if (ec) {
            spdlog::info("Client Disconnected");
            peer->socket.close();
            RemovePeer(key);
            co_return;
        }
        // 处理消息头
        uint8_t type = 0;
        uint32_t size = 0;
        int offset = 0;
        memcpy(&type, header.data(), 1);
        offset += 1;
        memcpy(&size,header.data() + offset, 4);
        offset += 4;
        // 正式处理消息
        vector<uint8_t> message;
        message.resize(size);
        co_await async_read(peer->socket,buffer(message), redirect_error(use_awaitable, ec));
        // type = 1 代表交易消息
        if (type == 1) {
            if (size != 176) {spdlog::info("Read Wrong Tx");continue;}
            array<uint8_t,176> byte{};
            memcpy(byte.data(), message.data() , size);
            ProcessTx(byte);
        }
        // type = 2 代表区块消息
        else if (type == 2) {
            vector<uint8_t> byte{};
            byte.resize(size);
            memcpy(byte.data(), message.data() , size);
            ProcessBlock(byte);
        }
        // type = 3 表示时间消息
        else if (type == 3) {

        }
        // type = 4 接收 QueryHeight 请求（区块链高度查询）
        else if (type == 4) {
            uint64_t height = DBReadBlockHeight();
            // 发送高度消息
            vector<uint8_t> byte{};
            byte.resize(8);
            memcpy(byte.data(), &height, 8);
            SendData(peer,byte);
        }
        // type = 5 接收 QueryHeight 返回值
        else if (type==5) {
            uint64_t maxHeight = 0;
            memcpy(&maxHeight, message.data(), 8);
            if (maxHeight >= DBReadBlockHeight()) {
                DBWriteBlockMaxHeight(maxHeight);
            }
        }
        // type = 6 接收 QueryBlock 请求
        // 消息： type || length || blockNum || blockByte
        else if (type == 6) {
            // 反序列化得到查询的区块编号 blockNum
            uint64_t blockNum = 0;
            memcpy(&blockNum, message.data(), 8);
            // 序列化返回的区块编号
            auto byte= GenerateBlockMessage(blockNum);
            SendData(peer,byte);
        }
        // type = 7 接收 QueryBlock 响应
        // 消息：MessageHeader || Payload
        // Payload : BlockHeader || Txs
        else if (type == 7)
        {
            array<uint8_t,32> blockhash{};
            memcpy(blockhash.data(), message.data()+80, 32);
            vector<uint8_t> blockByte{};
            blockByte.resize(size);
            memcpy(blockByte.data(), message.data(), size);
            DBWriteBlockALL(blockhash,blockByte);

        }
        // type = 8 接收节点数据请求
        else if (type == 8) {
            SendData(peer,GenerateDiscoverMessage());
        }
        // type = 9 接收节点数据返回
        else if (type == 9) {
            ProcessDiscoverMessage(message);
        }
        // type = 10 收到带时间戳的打包交易消息，进行处理并发送 ACK 消息
        //
        else if (type == 10) {
            auto T2 = GetTime();
            ProcessTxPackage(message);
            auto time = GenerateTxTimeACKMessage(T2);
            SendData(peer,time);
        }
        // type = 11 打包交易消息的 ACK
        else if (type == 11) {
            auto T4 = system_clock::now();
            ProcessTxTimeACKMessage(message,T4,peer);

        }
        // 其余消息逻辑需要解决
    }
}

awaitable<void> Listen(unsigned short port) {

    auto executor = co_await this_coro::executor;

    tcp::acceptor acceptor(executor, tcp::endpoint(tcp::v4(), port));

    spdlog::info("Server started on {}", port);

    for (;;) {
        boost::system::error_code ec;

        tcp::socket socket = co_await acceptor.async_accept(redirect_error(use_awaitable, ec));

        if (ec) {spdlog::info("New Client Connection Failed");continue;}
        // 读取连接的类型
        // 1 - 正常节点
        // 2 - Sender 只发交易，不进入 peerPool
        array<uint8_t, 1> type;
        co_await async_read(socket, buffer(type), redirect_error(use_awaitable, ec));
        if (type[0]==1) {
            // 考虑是否将连接自身的节点加入 socket 池
            auto [key,peer] = AddPeer(std::move(socket));
            spdlog::info("New Client Connected");
            co_spawn(executor, session(key,peer), detached);
        }
        else if (type[0]==2) {
            spdlog::info("Sender Connected");
            auto peer =make_shared<Peer>(std::move(socket));
            co_spawn(executor, session(0,peer), detached);

        }

    }
}

// ------------------------------------------------------------------工具函数--------------------------------------------------------------------------------------------------------
bool IsLocalPeer(tcp::socket& socket) {
    boost::system::error_code ec;
    auto ep = socket.remote_endpoint(ec);
    if (ec) {
        return false;
    }

    return ep.address().is_loopback();
}
#endif //CHAIN_SERVER_H