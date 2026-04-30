//
// Created by 61485 on 2026/4/30.
//

#ifndef CHAIN_SERVER_H
#define CHAIN_SERVER_H
#include <utility>
#include <iostream>
#include <boost/asio.hpp>
#include <nlohmann/json.hpp>
using namespace boost::asio;
using namespace std;
using namespace nlohmann;
using tcp = ip::tcp;



awaitable<void> session(tcp::socket socket) {
    char data[1024];

    for (;;) {
        boost::system::error_code ec;

        std::size_t n = co_await socket.async_read_some(buffer(data), redirect_error(use_awaitable, ec));

        if (ec) {
            std::cout << "client disconnected: " << ec.message() << std::endl;
            co_return;
        }

        std::string msg(data, n);
        std::cout << "received: " << msg << std::endl;
    }
}

awaitable<void> Listen(unsigned short port) {
    auto executor = co_await this_coro::executor;

    tcp::acceptor acceptor(executor, tcp::endpoint(tcp::v4(), port));

    std::cout << "server listening on port " << port << std::endl;

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
awaitable<void> ServerInit() {
    io_context io;
    co_spawn(io,Listen(8089),detached);
    io.run();

}
#endif //CHAIN_SERVER_H