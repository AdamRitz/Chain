#include <utility>
#include <boost/asio.hpp>
#include <fstream>
#include <thread>
#include <nlohmann/json.hpp>
#include "../Transaction/Transaction.h"
#include "../Message/Message.h"
using namespace std;
using namespace boost::asio;
using tcp=ip::tcp;

tcp::socket ConnectNode(io_context& io,const string& host,uint16_t port) {
    tcp::socket socket(io);
    socket.connect(tcp::endpoint(ip::make_address_v4(host),port));
    socket.set_option(tcp::no_delay(true));
    array<uint8_t,1> hello{2};
    write(socket,buffer(hello));
    return socket;
}
vector<uint8_t> GenerateTxMessage(const array<uint8_t,176>& tx) { return GenerateMessage(1,tx); }
void Sender(span<const array<uint8_t,176>> txs,const string& host,uint16_t port,size_t batch) {
    io_context io;
    auto socket=ConnectNode(io,host,port);
    for (size_t i=0;i<txs.size();i+=batch) {
        auto num=min(batch,txs.size()-i);
        auto data=span<const uint8_t>(reinterpret_cast<const uint8_t*>(txs.data()+i),num*176);
        auto message=GenerateMessage(num==1?1:10,data);
        write(socket,buffer(message));
    }
    boost::system::error_code ec;
    socket.shutdown(tcp::socket::shutdown_send,ec);
}
int main(int argc,char* argv[]) {
    try {
        if (sodium_init()<0) throw runtime_error("Sodium Init Error");
        string host="127.0.0.1",file,prepare;
        uint16_t port=8089;
        size_t count=100000,batch=128,connections=8,walletNum=1024,offset=0;
        bool stats=false;
        for (int i=1;i<argc;i++) {
            string name=argv[i];
            if (name=="--stats") { stats=true; continue; }
            if (i+1>=argc) throw invalid_argument("Missing value");
            string value=argv[++i];
            if (name=="--host") host=value;
            else if (name=="--port") { auto number=stoull(value); if (!number||number>65535) throw invalid_argument("Invalid port"); port=uint16_t(number); }
            else if (name=="--file") file=value;
            else if (name=="--prepare") prepare=value;
            else if (name=="--count") count=stoull(value);
            else if (name=="--batch") batch=stoull(value);
            else if (name=="--connections") connections=stoull(value);
            else if (name=="--wallets") walletNum=stoull(value);
            else if (name=="--offset") offset=stoull(value);
            else throw invalid_argument("Unknown argument: "+name);
        }
        if (!count||count>2000000||!batch||batch>6000||!connections||connections>64||!walletNum||walletNum>100000) throw invalid_argument("Invalid sender limits");
        if (stats) {
            io_context io;
            auto socket=ConnectNode(io,host,port);
            write(socket,buffer(GenerateMessage(13,{})));
            array<uint8_t,5> header;
            read(socket,buffer(header));
            auto size=ReadU32(header.data()+1);
            if (header[0]!=14||size>16384) throw runtime_error("Invalid stats response");
            string response(size,' ');
            read(socket,buffer(response));
            cout<<response<<endl;
            return 0;
        }
        vector<array<uint8_t,176>> txs;
        if (!file.empty()) {
            ifstream input(file,ios::binary|ios::ate);
            if (!input) throw runtime_error("Cannot open dataset");
            auto length=input.tellg();
            if (length<0||uint64_t(length)%176!=0) throw runtime_error("Invalid dataset length");
            auto total=uint64_t(length)/176;
            if (offset>total||count>total-offset) throw runtime_error("Dataset is too small");
            txs.resize(count);
            input.seekg(offset*176);
            input.read(reinterpret_cast<char*>(txs.data()),count*176);
            if (!input) throw runtime_error("Dataset read failed");
        } else {
            vector<Wallet> wallets;
            for (size_t i=0;i<walletNum;i++) wallets.push_back(GenerateWallet());
            txs.reserve(count);
            for (size_t i=0;i<count;i++) {
                const auto& wallet=wallets[i%walletNum];
                txs.push_back(GenerateTx(wallet.public_key,wallets[(i+1)%walletNum].public_key,1,i/walletNum+1,wallet));
            }
        }
        if (!prepare.empty()) {
            ofstream output(prepare,ios::binary);
            if (!output) throw runtime_error("Cannot write dataset");
            output.write(reinterpret_cast<const char*>(txs.data()),txs.size()*176);
            if (!output) throw runtime_error("Dataset write failed");
            cout<<nlohmann::json({{"prepared",txs.size()},{"wallets",walletNum}}).dump()<<endl;
            return 0;
        }
        connections=min(connections,txs.size());
        atomic<bool> failed{false};
        mutex errorMutex;
        string errorMessage;
        auto start=GetSteadyTime();
        vector<thread> threads;
        for (size_t i=0;i<connections;i++) {
            auto begin=txs.size()*i/connections;
            auto end=txs.size()*(i+1)/connections;
            threads.emplace_back([&,begin,end] {
                try { Sender(span<const array<uint8_t,176>>(txs).subspan(begin,end-begin),host,port,batch); }
                catch (const exception& e) { failed=true; lock_guard lock(errorMutex); errorMessage=e.what(); }
            });
        }
        for (auto& t:threads) t.join();
        if (failed) throw runtime_error(errorMessage);
        double elapsed=double(GetSteadyTime()-start)/1e9;
        cout<<nlohmann::json({{"sent",txs.size()},{"send_seconds",elapsed},{"send_tps",txs.size()/elapsed}}).dump()<<endl;
        return 0;
    } catch (const exception& e) {
        cerr<<"Sender failed: "<<e.what()<<endl;
        return 1;
    }
}
