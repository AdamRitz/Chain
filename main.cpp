#include <filesystem>
#include <fstream>
#include <csignal>
#include <yaml-cpp/yaml.h>
#include "Network/Server.h"
#ifdef _WIN32
#include <windows.h>
#include <psapi.h>
#endif
using namespace std;

void InitSodium() {
    if (sodium_init()<0) throw runtime_error("Sodium Init Error");
}
uint64_t ParseNumber(const string& text,uint64_t minimum,uint64_t maximum) {
    uint64_t value=0;
    auto result=from_chars(text.data(),text.data()+text.size(),value);
    if (result.ec!=errc()||result.ptr!=text.data()+text.size()||value<minimum||value>maximum) throw invalid_argument("Invalid argument: "+text);
    return value;
}
int main(int argc,char* argv[]) {
    try {
        string configPath,dataPath="data/accounts-v3",metricsPath,seedHost,genesisPath;
        int ioNum=4,verifyNum=8,runSeconds=0;
        uint16_t seedPort=0;
        bool sync=true;
        for (int i=1;i<argc;i++) {
            if (string(argv[i])=="--config"&&i+1<argc) configPath=argv[++i];
        }
        if (configPath.empty()&&filesystem::exists("config.yaml")) configPath="config.yaml";
        if (configPath.empty()&&filesystem::exists("../config.yaml")) configPath="../config.yaml";
        if (!configPath.empty()) {
            auto node=YAML::LoadFile(configPath)["node"];
            if (node["port"]) listenPort=node["port"].as<uint16_t>();
            if (node["bind"]) listenAddress=node["bind"].as<string>();
            if (node["data"]) dataPath=node["data"].as<string>();
            if (node["genesis"]) genesisPath=node["genesis"].as<string>();
            if (node["io_threads"]) ioNum=node["io_threads"].as<int>();
            if (node["verify_threads"]) verifyNum=node["verify_threads"].as<int>();
            if (node["block_ms"]) blockInterval=node["block_ms"].as<int>();
            if (node["sync"]) sync=node["sync"].as<bool>();
            if (node["produce"]) produceBlocks=node["produce"].as<bool>();
        }
        for (int i=1;i<argc;i++) {
            string name=argv[i];
            if (i+1>=argc) throw invalid_argument("Missing value for "+name);
            string value=argv[++i];
            if (name=="--config") continue;
            if (name=="--data") dataPath=value;
            else if (name=="--genesis") genesisPath=value;
            else if (name=="--bind") listenAddress=value;
            else if (name=="--port") listenPort=uint16_t(ParseNumber(value,1,65535));
            else if (name=="--io-threads") ioNum=int(ParseNumber(value,1,64));
            else if (name=="--verify-threads") verifyNum=int(ParseNumber(value,1,64));
            else if (name=="--block-ms") blockInterval=int(ParseNumber(value,1,5000));
            else if (name=="--max-block-txs") maxBlockTx=ParseNumber(value,1,6000);
            else if (name=="--max-pool") maxPoolTx=ParseNumber(value,6000,2000000);
            else if (name=="--max-pending") maxPendingVerify=ParseNumber(value,6000,1000000);
            else if (name=="--sync") sync=ParseNumber(value,0,1)!=0;
            else if (name=="--produce") produceBlocks=ParseNumber(value,0,1)!=0;
            else if (name=="--run-seconds") runSeconds=int(ParseNumber(value,1,86400));
            else if (name=="--metrics") metricsPath=value;
            else if (name=="--seed") {
                auto colon=value.find(':');
                if (colon==string::npos) throw invalid_argument("Use IPv4:port for --seed");
                seedHost=value.substr(0,colon);
                seedPort=uint16_t(ParseNumber(value.substr(colon+1),1,65535));
            } else throw invalid_argument("Unknown argument: "+name);
        }
        if (ioNum<1||ioNum>64||verifyNum<1||verifyNum>64||blockInterval<1||blockInterval>5000||listenPort==0) throw invalid_argument("Invalid node configuration");
        InitSodium();
        if (!genesisPath.empty()) LoadGenesisUsers(genesisPath);
        InitDB(dataPath,sync);
        InitWallet();
        GenerateGenesisBlock();
        txpool.reserve(maxPoolTx);
        io_context io;
        nodeIO=&io;
        verifyThreads=make_unique<thread_pool>(verifyNum);
        auto guard=make_work_guard(io);
        auto stop=[&] { io.stop(); };
        signal_set signals(io,SIGINT,SIGTERM);
        signals.async_wait([&](const boost::system::error_code& ec,int){if (!ec) stop();});
        steady_timer timer(io);
        if (runSeconds) {
            timer.expires_after(seconds(runSeconds));
            timer.async_wait([&](const boost::system::error_code& ec){if (!ec) stop();});
        }
        auto onError=[&](exception_ptr error) {
            if (!error) return;
            try { rethrow_exception(error); } catch (const exception& e) { spdlog::error("Node: {}",e.what()); }
            nodeFailed=true;
            io.stop();
        };
        co_spawn(io,Listen(listenPort),onError);
        co_spawn(io,OpenDiscoveryMode(),onError);
        if (!seedHost.empty()) co_spawn(make_strand(io),ConnectSeed(seedHost,seedPort),onError);
        spdlog::info("Node ready. IO:{}, Verify:{}, BlockMs:{}, Produce:{}",ioNum,verifyNum,blockInterval,produceBlocks);
        thread blockThread(MainLoop);
        vector<thread> threads;
        for (int i=0;i<ioNum;i++) threads.emplace_back([&] {
            try { io.run(); } catch (...) { onError(current_exception()); }
        });
        for (auto& t:threads) t.join();
        // 先停止网络接收，再完成已经入队的验证，最后提交剩余交易。
        verifyThreads->join();
        nodeRunning=false;
        WakeMainLoop();
        blockThread.join();
        // 所有工作线程结束后，在 io_context 析构前释放全局节点连接。
        vector<shared_ptr<Peer>> peers;
        {
            lock_guard lock(peerMutex);
            for (const auto& [key,peer]:peerPool) peers.push_back(peer);
            peerPool.clear();
        }
        for (const auto& peer:peers) ClosePeer(peer);
        peers.clear();
        auto stats=GetNodeStats();
        stats["io_threads"]=ioNum;
        stats["verify_threads"]=verifyNum;
        stats["hardware_threads"]=thread::hardware_concurrency();
        stats["block_ms"]=blockInterval;
        stats["max_block_txs"]=maxBlockTx;
        stats["failed"]=nodeFailed.load();
#ifdef _WIN32
        FILETIME created,exited,kernel,user;
        if (GetProcessTimes(GetCurrentProcess(),&created,&exited,&kernel,&user)) {
            ULARGE_INTEGER k,u;
            k.LowPart=kernel.dwLowDateTime; k.HighPart=kernel.dwHighDateTime;
            u.LowPart=user.dwLowDateTime; u.HighPart=user.dwHighDateTime;
            stats["process_cpu_seconds"]=(k.QuadPart+u.QuadPart)/1e7;
        }
        PROCESS_MEMORY_COUNTERS memory{};
        if (GetProcessMemoryInfo(GetCurrentProcess(),&memory,sizeof(memory))) stats["peak_working_set_bytes"]=memory.PeakWorkingSetSize;
#endif
        if (!metricsPath.empty()) {
            ofstream output(metricsPath);
            if (!output) throw runtime_error("Cannot write metrics file");
            output<<stats.dump(2)<<endl;
            string dbStats;
            db->GetProperty("rocksdb.stats",&dbStats);
            ofstream dbOutput(metricsPath+".rocksdb.txt");
            dbOutput<<dbStats<<endl;
        }
        cout<<stats.dump()<<endl;
        verifyThreads.reset();
        nodeIO=nullptr;
        db.reset();
        return nodeFailed?1:0;
    } catch (const exception& e) {
        cerr<<"Node failed: "<<e.what()<<endl;
        return 1;
    }
}
