#include <utility>
#include <iostream>
#include "Network/Server.h"
#include <nlohmann/json.hpp>
#include "Network/Client.h"
using namespace std;
using namespace nlohmann;




int main(int argc,char* argv[]) {
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