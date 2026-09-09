#include <utility>
#include <boost/asio.hpp>
#include <fstream>
#include <filesystem>
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
void Sender(span<const array<uint8_t,176>> txs,span<const size_t> indices,const string& host,uint16_t port,size_t batch) {
    if (indices.empty()) return;
    io_context io;
    auto socket=ConnectNode(io,host,port);
    for (size_t i=0;i<indices.size();i+=batch) {
        auto num=min(batch,indices.size()-i);
        vector<uint8_t> message(5+num*176);
        message[0]=num==1?1:10;
        WriteU32(message.data()+1,uint32_t(num*176));
        for (size_t j=0;j<num;j++) memcpy(message.data()+5+j*176,txs[indices[i+j]].data(),176);
        write(socket,buffer(message));
    }
    boost::system::error_code ec;
    socket.shutdown(tcp::socket::shutdown_send,ec);
}
int main(int argc,char* argv[]) {
    try {
        if (sodium_init()<0) throw runtime_error("Sodium Init Error");
        string host="127.0.0.1",file,prepare,genesisPath,createWallet,walletPath,receiverText,accountText;
        uint16_t port=8089;
        size_t count=100000,batch=128,connections=8,walletNum=1024,offset=0;
        bool stats=false,countSet=false;
        uint64_t amount=1,nonce=1,initialBalance=1000000000,fee=0;
        for (int i=1;i<argc;i++) {
            string name=argv[i];
            if (name=="--stats") { stats=true; continue; }
            if (i+1>=argc) throw invalid_argument("Missing value");
            string value=argv[++i];
            if (name=="--host") host=value;
            else if (name=="--port") { auto number=stoull(value); if (!number||number>65535) throw invalid_argument("Invalid port"); port=uint16_t(number); }
            else if (name=="--file") file=value;
            else if (name=="--prepare") prepare=value;
            else if (name=="--count") { count=stoull(value); countSet=true; }
            else if (name=="--batch") batch=stoull(value);
            else if (name=="--connections") connections=stoull(value);
            else if (name=="--wallets") walletNum=stoull(value);
            else if (name=="--offset") offset=stoull(value);
            else if (name=="--genesis") genesisPath=value;
            else if (name=="--base-fee") fee=ReadUserNumber(nlohmann::json::parse(value));
            else if (name=="--balance") initialBalance=ReadUserNumber(nlohmann::json::parse(value));
            else if (name=="--create-wallet") createWallet=value;
            else if (name=="--wallet") walletPath=value;
            else if (name=="--receiver") receiverText=value;
            else if (name=="--account") accountText=value;
            else if (name=="--amount") amount=ReadUserNumber(nlohmann::json::parse(value));
            else if (name=="--nonce") nonce=ReadUserNumber(nlohmann::json::parse(value));
            else throw invalid_argument("Unknown argument: "+name);
        }
        if (!count||count>2000000||!batch||batch>6000||!connections||connections>64||!walletNum||walletNum>100000) throw invalid_argument("Invalid sender limits");
        if (!createWallet.empty()) {
            if (filesystem::exists(createWallet)) throw invalid_argument("Wallet file already exists");
            auto wallet=GenerateWallet();
            array<uint8_t,32> seed;
            crypto_sign_ed25519_sk_to_seed(seed.data(),wallet.private_key.data());
            ofstream output(createWallet);
            if (!output) throw runtime_error("Cannot create wallet file");
            output<<nlohmann::json({{"public_key",U32ToHex(wallet.public_key)},{"seed",U32ToHex(seed)}}).dump(2)<<endl;
            if (!output) throw runtime_error("Wallet write failed");
            cout<<nlohmann::json({{"public_key",U32ToHex(wallet.public_key)},{"wallet_file",createWallet}}).dump()<<endl;
            return 0;
        }
        if (!accountText.empty()) {
            auto key=HexToU32(accountText);
            io_context io;
            auto socket=ConnectNode(io,host,port);
            write(socket,buffer(GenerateMessage(15,key)));
            array<uint8_t,5> header;
            read(socket,buffer(header));
            if (header[0]!=16||ReadU32(header.data()+1)!=17) throw runtime_error("Invalid account response");
            array<uint8_t,17> reply;
            read(socket,buffer(reply));
            cout<<nlohmann::json({{"public_key",accountText},{"exists",reply[0]!=0},
                {"balance",ReadU64(reply.data()+1)},{"nonce",ReadU64(reply.data()+9)}}).dump()<<endl;
            return 0;
        }
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
        } else if (!walletPath.empty()) {
            ifstream input(walletPath);
            nlohmann::json saved;
            if (!input||!(input>>saved)) throw runtime_error("Cannot read wallet");
            auto seed=HexToU32(saved.at("seed").get<string>());
            Wallet wallet;
            if (crypto_sign_seed_keypair(wallet.public_key.data(),wallet.private_key.data(),seed.data())!=0) throw runtime_error("Invalid wallet seed");
            if (saved.contains("public_key")&&HexToU32(saved["public_key"].get<string>())!=wallet.public_key) throw runtime_error("Wallet public key mismatch");
            auto receiver=HexToU32(receiverText);
            if (!countSet) count=1;
            if (!amount||!nonce||count-1>UINT64_MAX-nonce) throw invalid_argument("Invalid transfer amount or nonce");
            for (size_t i=0;i<count;i++) txs.push_back(GenerateTx(wallet.public_key,receiver,amount,nonce+i,wallet));
        } else {
            if (prepare.empty()) throw invalid_argument("Use --wallet for funded transfers or --prepare to create a benchmark dataset");
            vector<Wallet> wallets;
            for (size_t i=0;i<walletNum;i++) wallets.push_back(GenerateWallet());
            auto accounts=nlohmann::json::array();
            for (const auto& wallet:wallets) accounts.push_back({{"public_key",U32ToHex(wallet.public_key)},{"balance",initialBalance}});
            SetGenesisUsers({{"base_fee",fee},{"accounts",accounts}});
            if (genesisPath.empty()) genesisPath=prepare+".genesis.json";
            if (filesystem::exists(genesisPath)) throw invalid_argument("Genesis file already exists");
            ofstream genesis(genesisPath);
            if (!genesis) throw runtime_error("Cannot write genesis configuration");
            genesis<<GetGenesisConfig().dump(2)<<endl;
            if (!genesis) throw runtime_error("Genesis write failed");
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
            cout<<nlohmann::json({{"prepared",txs.size()},{"wallets",walletNum},{"genesis",genesisPath}}).dump()<<endl;
            return 0;
        }
        connections=min(connections,txs.size());
        // 同一账户始终走同一连接，保持数据集中的序号顺序，减少大量跨连接未来交易。
        vector<vector<size_t>> groups(connections);
        for (size_t i=0;i<txs.size();i++) groups[ReadU64(txs[i].data())%connections].push_back(i);
        atomic<bool> failed{false};
        mutex errorMutex;
        string errorMessage;
        auto start=GetSteadyTime();
        vector<thread> threads;
        for (size_t i=0;i<connections;i++) {
            threads.emplace_back([&,i] {
                try { Sender(txs,groups[i],host,port,batch); }
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
