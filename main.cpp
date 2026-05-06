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
using namespace std;
using namespace nlohmann;



void Init() {
    if (sodium_init()<0) {
        cout<< "*** Sodium Init Error! ***"<<endl;
    }
}
int main1(int argc,char* argv[]) {
    Init();
    string mode = argv[1];
    io_context io;
    if (mode == "listen") {
        co_spawn(io,ServerInit(),detached);
    }
    else if (mode == "client") {
        co_spawn(io,ConnectSeed(),detached);
    }else {
        cout<<"输入正确的模式";
        return 1;
    }
    io.run();

    //ServerInit();

    // json a=json::parse(R"({"Pi":3.14,"name":"luowenbin"})");
    // auto name=a["Pi"];
    // cout << name << std::endl;
}


int main() {
    InitDB();
    mywallet=GenerateWallet();
    array<uint8_t, 32> sender=mywallet.public_key;
    auto data = GenerateTransaction(sender,sender,1,1,mywallet);
    cout<<"Verify Result:"<<VerifyTransaction(data)<<endl;
    auto hash= StoreTransaction(data);
    DBReadTx(hash);
}