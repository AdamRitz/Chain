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
using namespace boost::asio;
using namespace std;
using namespace nlohmann;

using tcp = ip::tcp;
vector<tcp::socket> socketpool;

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
awaitable<void> Send(string msg) {
    msg += "\n";
    for (auto it = socketpool.begin(); it != socketpool.end();) {
        boost::system::error_code ec;
        size_t n = co_await async_write(*it, buffer(msg), redirect_error(use_awaitable, ec));
        if (ec) {
            cout << "send failed: " << ec.message() << endl;

            boost::system::error_code ignored;
            it->close(ignored);

            it = socketpool.erase(it);
            continue;
        }
        cout << "sent bytes: " << n << endl;
        ++it;
    }

    co_return;
}
awaitable<void> ConnectSeed() {
    auto s = co_await Connect("127.0.0.1",8089);
    if (s.has_value()) {
        socketpool.push_back(std::move(*s));
    }
    auto executor = co_await this_coro::executor;
    steady_timer timer(executor);

    for (;;) {
        co_await Send("6666");
        timer.expires_after(std::chrono::seconds(1));
        co_await timer.async_wait(use_awaitable);
    }
    cout<<"Message Send";
    co_return;
}


#endif //CHAIN_CLIENT_H