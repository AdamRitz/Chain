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
using namespace boost::asio;
using namespace std;
using namespace nlohmann;
using tcp = ip::tcp;



awaitable<void> session(ip::tcp::socket socket) {
    array<uint8_t, 1024> message;

    for (;;) {
        boost::system::error_code ec;
        std::size_t n = co_await socket.async_read_some(buffer(message), redirect_error(use_awaitable, ec));
        if (ec) {
            std::cout << "client disconnected: " << ec.message() << std::endl;
            co_return;
        }
        // 读取消息头：类型和消息长度
        // 消息类型大小为 1 字节 uint8_t
        // 消息大小字段为 4 字节 uint32_t
        uint8_t type = 0;
        uint32_t size = 0;
        int offset = 0;
        memcpy(&type, message.data(), 1);
        offset += 1;
        memcpy(&size,message.data() + offset, 4);
        offset += 4;
        // 正式处理消息
        // type = 1 代表交易消息
        if (type == 1) {
            array<uint8_t,176> byte{};
            memcpy(byte.data(), message.data() + offset, size);
            ProcessTx(byte);
        }
        // type = 2 代表区块消息
        else if (type == 2) {
            vector<uint8_t> byte{};
            byte.resize(size);
            memcpy(byte.data(), message.data() + offset, size);
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
            co_await(SendData(socket,byte)) ;
        }
        // type = 5 接收 QueryHeight 返回值
        else if (type==5) {
            uint64_t maxHeight = 0;
            memcpy(&maxHeight, message.data()+offset, 8);
            if (maxHeight >= DBReadBlockHeight()) {
                DBWriteBlockMaxHeight(maxHeight);
            }
        }
        // type = 6 接收 QueryBlock 请求
        // 消息： type || length || blockNum || blockByte
        else if (type == 6) {
            // 反序列化得到查询的区块编号 blockNum
            uint64_t blockNum = 0;
            memcpy(&blockNum, message.data()+offset, 8);
            // 序列化返回的区块编号
            auto byte= GenerateBlockMessage(blockNum);
            co_await (SendData(socket,byte));
        }
        // type = 7 接收 QueryBlock 响应
        else if (type == 7)
        {
            // 获取消息长度
            uint32_t length = 0;
            memcpy(&length, message.data()+offset, 4);
            offset += 4;
            uint64_t blockNum = 0;
            memcpy(&blockNum, message.data()+offset, 8);
            offset += 8;
            vector<uint8_t> blockByte{};
            blockByte.resize(length-8);
            memcpy(blockByte.data(), message.data()+offset, length-8);
            co_await (SendData(socket,blockByte)) ;
        }
        // 其余消息类型丢弃
        co_return;
    }
}

awaitable<void> Listen(unsigned short port) {

    auto executor = co_await this_coro::executor;

    tcp::acceptor acceptor(executor, tcp::endpoint(tcp::v4(), port));

    spdlog::info("Server started on {}", port);

    for (;;) {
        boost::system::error_code ec;

        tcp::socket socket = co_await acceptor.async_accept(redirect_error(use_awaitable, ec));

        if (ec) {
            std::cout << "accept failed: " << ec.message() << std::endl;
            continue;
        }

        std::cout << "new client connected" << std::endl;

        co_spawn(executor, session(std::move(socket)), detached);
    }
}


#endif //CHAIN_SERVER_H