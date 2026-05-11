//
// Created by 61485 on 2026/4/29.
//
#include <iostream>
#include <boost/asio.hpp>
#include <functional>
#include <iostream>
#include <system_error>
#include <fstream>
#include <chrono>
#include "../Transaction/Transaction.h"
#include "../Key/Key.h"
#include "../Network/Client.h"
using  namespace  boost::asio;
using tcp = ip::tcp;


io_context io;
awaitable<tcp::socket> ConnectNode() {
    auto executor = co_await this_coro::executor;
    ip::tcp::socket sock(executor);
    ip::tcp::endpoint endpoint(ip::make_address("127.0.0.1"),8089);
    co_await sock.async_connect(endpoint);
    co_return std::move(sock);
}
awaitable<void> Sender() {
    auto txByte=GenerateTx(mywallet.public_key,mywallet.public_key,1,1,mywallet);
    auto sock = co_await ConnectNode();
    co_await async_write(sock,buffer(txByte));
    co_return;
}

int main() {
    InitWallet();

    io.run();


}