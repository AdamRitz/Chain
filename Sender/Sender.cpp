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
#include "../Tool/Tool.h"
using  namespace  boost::asio;
using tcp = ip::tcp;


io_context io;
awaitable<tcp::socket> ConnectNode() {
    auto executor = co_await this_coro::executor;
    ip::tcp::socket sock(executor);
    boost::system::error_code ec;
    ip::tcp::endpoint endpoint(ip::make_address("127.0.0.1"),8089);
    co_await sock.async_connect(endpoint,redirect_error(use_awaitable,ec));
    if (ec) {cout << ec.message() << endl;co_return nullptr;}
    array<uint8_t, 1> data;
    data[0] = 2;
    co_await async_write(sock,buffer(data),redirect_error(use_awaitable,ec));
    spdlog::info("SenderConnected");
    co_return std::move(sock);
}
vector<uint8_t> GenerateTxMessage(uint64_t i) {
    auto txData=GenerateTx(mywallet.public_key,mywallet.public_key,1,i,mywallet);
    vector<uint8_t> txByte;
    txByte.resize(txData.size()+1+4);
    int type = 1;
    int len = txData.size();
    memcpy(txByte.data()+0,&type,1);
    memcpy(txByte.data()+1,&len,4);
    memcpy(txByte.data()+5,txData.data(),txData.size());
    return txByte;

}
void InitSodium() {
    if (sodium_init()<0) {
        cout<< "*** Sodium Init Error! ***"<<endl;
    }
    spdlog::info("Sodium Started.");
}
awaitable<void> Sender() {
    boost::system::error_code ec;

    auto sock = co_await ConnectNode();
        for( uint64_t i = 1; i <INT64_MAX; i++) {
            if (i%10000==0){sleep(1);cout<<"Tx send num :"<<i<<endl;}
        auto txByte= GenerateTxMessage(i);
        co_await async_write(sock,buffer(txByte),redirect_error(use_awaitable,ec));
        if (ec) {cout << ec.message() << endl;co_return;}
    }
    co_return;



}

int main() {
    InitSodium();
    InitWallet();

    co_spawn(io,Sender(),detached);

    io.run();
}