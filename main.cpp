#include <utility>
#include <iostream>
#include "Network/Server.h"
#include <nlohmann/json.hpp>
#include "Network/Client.h"
#include "Key/Key.h"
#include "Transaction/Transaction.h"
#include "DB/DB.h"
#include <boost/asio.hpp>
#include "Tool/Tool.h"
#include "Transaction/Block.h"
#include <spdlog/spdlog.h>
#include "./Time/Time.h"
using namespace std;
using namespace nlohmann;
io_context io;


void InitSodium() {
    if (sodium_init()<0) {
        cout<< "*** Sodium Init Error! ***"<<endl;
    }
    spdlog::info("Sodium Started.");
}


int TestTx() {
    InitDB();
    mywallet=GenerateWallet();
    array<uint8_t, 32> sender=mywallet.public_key;
    auto data = GenerateTx(sender,sender,1,1,mywallet);
    cout<<"Verify Result:"<<VerifyTransaction(data)<<endl;
    DBWriteTx(GetTransactionHash(data),data);
    DBReadTx(GetTransactionHash(data));
}

void TestBlock() {
    InitDB();
    mywallet=GenerateWallet();
    GenerateGenesisBlock();
    for (int i=0;i<=80;i++) {
        array<uint8_t, 32> sender=mywallet.public_key;
        auto data = GenerateTx(sender,sender,i,1,mywallet);
        txpool[GetTransactionHash(data)] = data;
    }

    auto block = GenerateBlock();
    cout<< "Result:"<<VerifyBlock(block);

}
void Sender() {
    cout<<"Sender Started."<<endl;
    while (true) {
        for (int i=0;i<=101;i++) {
            auto txByte= GenerateTx(mywallet.public_key,mywallet.public_key,rand(),rand(),mywallet);
            lock_guard<mutex> lock(txpoolMutex);
            txpool.emplace(GetTransactionHash(txByte),txByte);
        }
        sleep(1);
    }


}
int main(int argc,char* argv[]) {
    spdlog::info("node starting...");
    // 初始化区域
    InitSodium(); // 初始化随机数
    InitDB(); // 初始化数据库
    InitWallet(); // 初始化钱包
    co_spawn(io,Listen(8089),detached); // 启动服务端
    //co_spawn(io,ConnectSeed(),detached); // 启动客户端
    std::thread ioThread([](){io.run();}); // 启动上下文
    GenerateGenesisBlock();
    // 启动 Sender 线程
    //thread t2(Sender);
    // 启动打包线程
    thread t1(PeriodSendBlock);
    t1.detach();
    sleep(10000);
    //ServerInit();

    // json a=json::parse(R"({"Pi":3.14,"name":"luowenbin"})");
    // auto name=a["Pi"];
    // cout << name << std::endl;
}

// int main() {
//
//     TestBlock();
// }
