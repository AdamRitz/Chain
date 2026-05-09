//
// Created by 61485 on 2026/4/30.
//

#ifndef CHAIN_CLIENT_H
#define CHAIN_CLIENT_H
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
vector<tcp::socket> socketPool;
// -----------------------------------------------------------发送消息函数---------------------------------------------------------------------------------------------
awaitable<void> BroadcastData(span<uint8_t> byte) {
    for (size_t i = 0; i < socketPool.size();) {
        boost::system::error_code ec;
        size_t n = co_await async_write(socketPool[i], buffer(byte), redirect_error(use_awaitable, ec));
        if (ec) {
            spdlog::info("Broadcast data failed.");
            boost::system::error_code ignored;
            socketPool[i].close(ignored);
            socketPool.erase(socketPool.begin() + i);
        } else {
            ++i;
        }
    }
    co_return;
}
awaitable<void> SendData(tcp::socket& socket,const span<uint8_t>& data) {
    boost::system::error_code ec;
    size_t n = co_await async_write(socket, data, redirect_error(use_awaitable, ec));
    if (ec) {
        spdlog::info("Send data failed.");
    }
    co_return;
}

// -----------------------------------------------------------客户端入口函数---------------------------------------------------------------------------------------------
awaitable<optional<tcp::socket>> Connect(string ipaddr,unsigned short port) {
    auto executor = co_await this_coro::executor;
    tcp::socket socket(executor);
    boost::system::error_code ec;
    tcp::endpoint endpoint(ip::make_address(ipaddr,ec),port);
    if (ec) {cout << ec.message() << endl;}
    co_await socket.async_connect(endpoint,redirect_error(use_awaitable, ec));
    if (ec) {cout << ec.message() << endl;co_return nullopt;}
    cout<<"连接成功"<<endl;
    co_return std::move(socket);
}
awaitable<void> ConnectSeed() {
    auto s = co_await Connect("127.0.0.1",8089);
    if (s.has_value()) {
        socketPool.push_back(std::move(*s));
    }
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

awaitable<void> QueryHeight(tcp::socket& socket) {
    vector<uint8_t> messageByte={4};
    co_await SendData(socket,messageByte);
}
awaitable<void> QueryBlock( tcp::socket& socket,uint64_t blockNum) {
    vector<uint8_t> messageByte;
    messageByte.resize(9);
    uint8_t type = 6;
    memcpy(messageByte.data(),&type,1);
    memcpy(messageByte.data(),&blockNum,8);
    co_await SendData(socket,messageByte);
}

awaitable<void> SyncBlock() {
    co_await QueryHeight(socketPool[0]);
    auto maxHeight = DBReadBlockMaxHeight();
    auto currentHeight = DBReadBlockHeight();
    if (maxHeight>currentHeight) {
        while (currentHeight<maxHeight) {
            QueryBlock(socketPool[0],currentHeight+1);
            currentHeight +=1;
        }
    }


}
#endif //CHAIN_CLIENT_H