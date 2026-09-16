#include <utility>
#include <boost/asio.hpp>
#include <filesystem>
#include <thread>
#include <set>
#include "../Transaction/Transaction.h"
#include "../Message/Message.h"
using namespace boost::asio;
using tcp=ip::tcp;
Wallet ReadWallet(const string& path) {
    ifstream input(path); nlohmann::json data;
    if (!input||!(input>>data)) throw runtime_error("Cannot read wallet");
    auto seed=HexToU32(data.at("seed").get<string>());
    Wallet wallet;
    crypto_sign_seed_keypair(wallet.public_key.data(),wallet.private_key.data(),seed.data());
    if (data.contains("public_key")&&data["public_key"]!=U32ToHex(wallet.public_key)) throw runtime_error("Wallet public key mismatch");
    return wallet;
}
vector<uint8_t> MakeEvmTx(const Wallet& wallet,span<const uint8_t> target,span<const uint8_t> input,
                          uint64_t nonce,uint64_t amount,uint64_t gas,bool deploy) {
    if (input.size()>maxEvmInput||(!deploy&&target.size()!=20)||!nonce) throw invalid_argument("Invalid EVM transaction");
    vector<uint8_t> tx(221+input.size());
    memcpy(tx.data(),wallet.public_key.data(),32);
    if (!deploy) memcpy(tx.data()+44,target.data(),20);
    WriteU64(tx.data()+64,amount); WriteU64(tx.data()+72,nonce);
    memcpy(tx.data()+176,"EVM1",4); tx[180]=deploy?0:1;
    WriteU64(tx.data()+181,gas); memcpy(tx.data()+189,genesisRoot.data(),32);
    if (!input.empty()) memcpy(tx.data()+221,input.data(),input.size());
    auto hash=EvmTransactionHash(tx,span<const uint8_t>(tx).subspan(176));
    memcpy(tx.data()+80,hash.data(),32);
    crypto_sign_detached(tx.data()+112,nullptr,hash.data(),32,wallet.private_key.data());
    return tx;
}
tcp::socket OpenNode(io_context& io,const string& host,uint16_t port) {
    tcp::socket socket(io); socket.connect({ip::make_address_v4(host),port}); socket.set_option(tcp::no_delay(true));
    array<uint8_t,1> hello{2}; write(socket,buffer(hello)); return socket;
}
nlohmann::json Query(const string& host,uint16_t port,uint8_t type,uint8_t response,span<const uint8_t> data) {
    io_context io; auto socket=OpenNode(io,host,port); write(socket,buffer(GenerateMessage(type,data)));
    array<uint8_t,5> header; read(socket,buffer(header));
    auto size=ReadU32(header.data()+1);
    if (header[0]!=response||size>maxMessageSize) throw runtime_error("Invalid query response");
    string text(size,' '); read(socket,buffer(text)); return nlohmann::json::parse(text);
}
int main(int argc,char** argv) {
    try {
        if (sodium_init()<0) return 1;
        map<string,string> args;
        const set<string> names={"--host","--port","--receipt","--contract","--slot","--wallet","--evm-nonce","--count","--wallets","--batch","--connections","--offset","--file","--gas","--amount","--nonce","--deploy","--input","--call","--genesis","--prepare","--balance"};
        for (int i=1;i<argc;i++) {
            string key=argv[i];
            if (key=="--address") { args[key]="1"; continue; }
            if (!names.count(key)) throw invalid_argument("Unknown argument: "+key);
            if (!key.starts_with("--")||i+1>=argc) throw invalid_argument("Use --name value");
            args[key]=argv[++i];
        }
        auto Number=[&](const string& name,uint64_t fallback) { return args.count(name)?ReadUserNumber(nlohmann::json::parse(args[name])):fallback; };
        auto host=args.count("--host")?args["--host"]:"127.0.0.1";
        auto portValue=Number("--port",8089);
        if (!portValue||portValue>65535) throw invalid_argument("Invalid port");
        auto port=uint16_t(portValue);
        if (args.count("--receipt")) {
            auto hash=HexToU32(args["--receipt"]); cout<<Query(host,port,23,24,hash).dump()<<endl; return 0;
        }
        if (args.count("--contract")) {
            auto address=EvmBytes(args["--contract"]);
            if (address.size()!=20) throw invalid_argument("Contract address must contain 20 bytes");
            if (args.count("--slot")) { auto slot=HexToU32(args["--slot"]); address.insert(address.end(),slot.begin(),slot.end()); }
            cout<<Query(host,port,25,26,address).dump()<<endl; return 0;
        }
        if (args.count("--address")) {
            auto wallet=ReadWallet(args.at("--wallet")); auto address=GetEvmAddress(wallet.public_key);
            auto contract=evmone::state::compute_create_address(address,Number("--evm-nonce",0));
            cout<<nlohmann::json({{"address",EvmHex(address.bytes)},{"create_address",EvmHex(contract.bytes)}}).dump()<<endl; return 0;
        }
        auto count=Number("--count",1),wallets=Number("--wallets",64),batch=Number("--batch",64),connections=Number("--connections",4),offset=Number("--offset",0);
        if (!count||count>2000000||!wallets||wallets>100000||!batch||batch>6000||!connections||connections>64) throw invalid_argument("Invalid sender limits");
        if (offset>2000000||count>2000000-offset) throw invalid_argument("Invalid dataset range");
        vector<vector<uint8_t>> txs;
        if (args.count("--file")) {
            ifstream input(args["--file"],ios::binary);
            if (!input) throw runtime_error("Cannot open EVM dataset");
            for (uint64_t i=0;i<offset+count;i++) {
                array<uint8_t,4> header; input.read(reinterpret_cast<char*>(header.data()),4);
                if (!input) throw runtime_error("EVM dataset too short");
                auto size=ReadU32(header.data());
                if (size<221||size>221+maxEvmInput) throw runtime_error("Invalid EVM record size");
                vector<uint8_t> tx(size); input.read(reinterpret_cast<char*>(tx.data()),size);
                if (!input) throw runtime_error("EVM dataset truncated");
                if (i>=offset) txs.push_back(std::move(tx));
            }
        } else {
            auto gas=Number("--gas",100000),amount=Number("--amount",0),nonce=Number("--nonce",1);
            if (gas<21000||gas>maxEvmGas||!nonce||count-1>UINT64_MAX-nonce) throw invalid_argument("Invalid gas or nonce");
            bool deploy=args.count("--deploy");
            auto input=EvmBytes(deploy?args["--deploy"]:args["--input"]);
            auto target=deploy?vector<uint8_t>{}:EvmBytes(args.at("--call"));
            if (args.count("--wallet")) {
                LoadGenesisUsers(args.at("--genesis")); auto wallet=ReadWallet(args["--wallet"]);
                for (uint64_t i=0;i<count;i++) txs.push_back(MakeEvmTx(wallet,target,input,nonce+i,amount,gas,deploy));
            } else {
                if (!args.count("--prepare")) throw invalid_argument("Use --wallet or --prepare");
                vector<Wallet> keys; auto accounts=nlohmann::json::array();
                if (args.count("--genesis")) { LoadGenesisUsers(args["--genesis"]); accounts=GetGenesisConfig()["accounts"]; }
                for (uint64_t i=0;i<wallets;i++) { keys.push_back(GenerateWallet()); accounts.push_back({{"public_key",U32ToHex(keys.back().public_key)},{"balance",Number("--balance",10000000000ULL)}}); }
                SetGenesisUsers({{"base_fee",baseFee},{"accounts",accounts}});
                auto path=args["--prepare"]+".genesis.json";
                if (filesystem::exists(path)) throw runtime_error("Genesis file already exists");
                ofstream genesis(path); genesis<<GetGenesisConfig().dump(2); if (!genesis) throw runtime_error("Genesis write failed");
                for (uint64_t i=0;i<count;i++) txs.push_back(MakeEvmTx(keys[i%wallets],target,input,nonce+i/wallets,amount,gas,deploy));
            }
        }
        if (args.count("--prepare")) {
            if (filesystem::exists(args["--prepare"])) throw runtime_error("Dataset already exists");
            ofstream out(args["--prepare"],ios::binary);
            for (const auto& tx:txs) { array<uint8_t,4> length; WriteU32(length.data(),uint32_t(tx.size())); out.write(reinterpret_cast<char*>(length.data()),4); out.write(reinterpret_cast<const char*>(tx.data()),tx.size()); }
            if (!out) throw runtime_error("Dataset write failed");
            cout<<nlohmann::json({{"prepared",txs.size()},{"genesis_root",U32ToHex(genesisRoot)}}).dump()<<endl; return 0;
        }
        vector<vector<size_t>> groups(connections);
        for (size_t i=0;i<txs.size();i++) groups[ReadU64(txs[i].data())%connections].push_back(i);
        vector<thread> threads; atomic<bool> failed{false}; auto start=GetSteadyTime();
        for (const auto& group:groups) threads.emplace_back([&,group] {
            try {
                if (group.empty()) return;
                io_context io; auto socket=OpenNode(io,host,port); vector<uint8_t> payload; size_t num=0;
                auto Flush=[&] { if (!payload.empty()) write(socket,buffer(GenerateMessage(18,payload))); payload.clear(); num=0; };
                for (auto index:group) {
                    const auto& tx=txs[index]; if (num>=batch||payload.size()+4+tx.size()>maxMessageSize) Flush();
                    auto end=payload.size(); payload.resize(end+4+tx.size()); WriteU32(payload.data()+end,uint32_t(tx.size())); memcpy(payload.data()+end+4,tx.data(),tx.size()); num++;
                }
                Flush();
            } catch (const exception& e) { cerr<<e.what()<<endl; failed=true; }
        });
        for (auto& thread:threads) thread.join();
        if (failed) throw runtime_error("EVM sender failed");
        double elapsed=double(GetSteadyTime()-start)/1e9;
        auto hash=span<const uint8_t>(txs.front()).subspan(80,32);
        cout<<nlohmann::json({{"sent",txs.size()},{"transaction_hash",EvmHex(hash)},{"send_seconds",elapsed}}).dump()<<endl;
        return 0;
    } catch (const exception& e) { cerr<<"EVM sender: "<<e.what()<<endl; return 1; }
}
