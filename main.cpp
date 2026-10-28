#include <filesystem>
#include <fstream>
#include <csignal>
#include <yaml-cpp/yaml.h>
#include "Network/Server.h"
#include "Tool/Arguments.h"
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
double ParseReal(const string& text,double minimum,double maximum) {
    size_t used=0; auto value=stod(text,&used);
    if (used!=text.size()||!isfinite(value)||value<minimum||value>maximum) throw invalid_argument("Invalid argument: "+text);
    return value;
}
void AddConfiguredPeer(const string& value) {
    configuredPeers.push_back(ParseEndpoint(value));
}
int main(int argc,char* argv[]) {
    try {
        Utf8Arguments unicode(argc,argv);
        string configPath,dataPath="data/accounts-v7",metricsPath,genesisPath,stopFile;
        int ioNum=4,verifyNum=8,candidateNum=0,blockVerifyCount=0,queryNum=1,runSeconds=0;
        bool sync=true,observeClock=false;
        for (int i=1;i<argc;i++) {
            if (string(argv[i])=="--config"&&i+1<argc) configPath=argv[++i];
        }
        if (configPath.empty()&&filesystem::exists("config.yaml")) configPath="config.yaml";
        if (configPath.empty()&&filesystem::exists("../config.yaml")) configPath="../config.yaml";
        if (!configPath.empty()) {
            ifstream configFile(Utf8Path(configPath));if(!configFile)throw runtime_error("Cannot open node configuration");
            auto node=YAML::Load(configFile)["node"];
            if (node["port"]) listenPort=node["port"].as<uint16_t>();
            if (node["bind"]) listenAddress=node["bind"].as<string>();
            if (node["bind_v6"]) listenAddressV6=node["bind_v6"].as<string>();
            if (node["peer_protocol"]) {auto version=node["peer_protocol"].as<unsigned>();if(version<1||version>2)throw invalid_argument("Invalid peer protocol");peerProtocolV2=version==2;}
            if (node["data"]) dataPath=node["data"].as<string>();
            if (node["genesis"]) genesisPath=node["genesis"].as<string>();
            if (node["io_threads"]) ioNum=node["io_threads"].as<int>();
            if (node["verify_threads"]) verifyNum=node["verify_threads"].as<int>();
            if (node["candidate_threads"]) candidateNum=node["candidate_threads"].as<int>();
            if (node["block_verify_threads"]) blockVerifyCount=node["block_verify_threads"].as<int>();
            if (node["query_threads"]) queryNum=node["query_threads"].as<int>();
            if (node["block_ms"]) blockInterval=node["block_ms"].as<int>();
            if (node["sync"]) sync=node["sync"].as<bool>();
            if (node["produce"]) produceBlocks=node["produce"].as<bool>();
            if (node["peers"]) for (const auto& peer:node["peers"]) AddConfiguredPeer(peer.as<string>());
        }
        for (int i=1;i<argc;i++) {
            string name=argv[i];
            if (i+1>=argc) throw invalid_argument("Missing value for "+name);
            string value=argv[++i];
            if (name=="--config") continue;
            if (name=="--data") dataPath=value;
            else if (name=="--genesis") genesisPath=value;
            else if (name=="--stop-file") stopFile=value;
            else if (name=="--bind") listenAddress=value;
            else if (name=="--bind-v6") { if (!ip::make_address(value).is_v6()) throw invalid_argument("--bind-v6 requires IPv6");listenAddressV6=value; }
            else if (name=="--peer-protocol") peerProtocolV2=ParseNumber(value,1,2)==2;
            else if (name=="--port") listenPort=uint16_t(ParseNumber(value,1,65535));
            else if (name=="--io-threads") ioNum=int(ParseNumber(value,1,64));
            else if (name=="--verify-threads") verifyNum=int(ParseNumber(value,1,64));
            else if (name=="--candidate-threads") candidateNum=int(ParseNumber(value,1,64));
            else if (name=="--block-verify-threads") blockVerifyCount=int(ParseNumber(value,1,64));
            else if (name=="--query-threads") queryNum=int(ParseNumber(value,1,4));
            else if (name=="--signature-cache") maxVerifiedPerBucket=(ParseNumber(value,16,4194304)+15)/16;
            else if (name=="--recent-tx-cache") {
                maxRecentBlockTransactions=ParseNumber(value,16,2097152);
                maxRelayTransactionBytes=max(size_t(32*1024*1024),maxRecentBlockTransactions*176);
            }
            else if (name=="--recent-tx-bytes") maxRelayTransactionBytes=ParseNumber(value,176,512*1024*1024);
            else if (name=="--sync-window") syncWindow=ParseNumber(value,4,64);
            else if (name=="--block-ms") blockInterval=int(ParseNumber(value,1,5000));
            else if (name=="--max-block-txs") maxBlockTx=ParseNumber(value,1,maxProtocolTx);
            else if (name=="--max-pool") maxPoolTx=ParseNumber(value,6000,2000000);
            else if (name=="--max-pending") maxPendingVerify=ParseNumber(value,6000,1000000);
            else if (name=="--sync") sync=ParseNumber(value,0,1)!=0;
            else if (name=="--db-profile") dbProfile=ParseNumber(value,0,1)!=0;
            else if (name=="--produce") produceBlocks=ParseNumber(value,0,1)!=0;
            else if (name=="--run-seconds") runSeconds=int(ParseNumber(value,1,86400));
            else if (name=="--metrics") metricsPath=value;
            else if (name=="--seed") AddConfiguredPeer(value);
            else if (name=="--max-peers") maxPeers=ParseNumber(value,1,1024);
            else if (name=="--max-connections") maxConnections=ParseNumber(value,2,4096);
            else if (name=="--relay-transactions") relayTransactions=ParseNumber(value,0,1)!=0;
            else if (name=="--experiment-index") experimentIndex=ParseNumber(value,0,1)!=0;
            else if (name=="--clock-observe") observeClock=ParseNumber(value,0,1)!=0;
            else if (name=="--clock-sync") { nodeClock.enabled=ParseNumber(value,0,1)!=0; observeClock=true; }
            else if (name=="--clock-offset-ms") nodeClock.offsetUs=int64_t(ParseReal(value,-10000,10000)*1000);
            else if (name=="--clock-drift-ppm") nodeClock.driftPpm=ParseReal(value,-5000,5000);
            else if (name=="--sync-period-ms") nodeClock.periodMs=int(ParseNumber(value,10,10000));
            else if (name=="--sync-gain") nodeClock.gain=ParseReal(value,0.001,1);
            else if (name=="--round-ms") nodeClock.roundMs=int(ParseNumber(value,0,5000));
            else if (name=="--candidate-ms") nodeClock.candidateMs=int(ParseNumber(value,0,5000));
            else throw invalid_argument("Unknown argument: "+name);
        }
        if (ioNum<1||ioNum>64||verifyNum<1||verifyNum>64||blockInterval<1||blockInterval>5000||listenPort==0) throw invalid_argument("Invalid node configuration");
        if (!candidateNum) candidateNum=min(4,verifyNum);
        if (!blockVerifyCount) blockVerifyCount=min(4,verifyNum);
        if (candidateNum<1||candidateNum>64||blockVerifyCount<1||blockVerifyCount>64||queryNum<1||queryNum>4) throw invalid_argument("Invalid worker configuration");
        if (nodeClock.roundMs&&!nodeClock.candidateMs) nodeClock.candidateMs=max(1,nodeClock.roundMs/4);
        if (nodeClock.candidateMs>=nodeClock.roundMs&&nodeClock.candidateMs) throw invalid_argument("Candidate window must be shorter than round");
        InitTime();
        InitSodium();
        if (!genesisPath.empty()) LoadGenesisUsers(genesisPath);
        InitDB(dataPath,sync);
        InitWallet();
        GenerateGenesisBlock();
        txpool.reserve(maxPoolTx);
        for (auto& cache:verifiedTransactions) { cache.hashes.reserve(maxVerifiedPerBucket); cache.pending.reserve(32); }
        io_context io;
        nodeIO=&io;
        verifyThreads=make_unique<thread_pool>(verifyNum);
        candidateThreads=make_unique<thread_pool>(candidateNum);
        queryThreads=make_unique<thread_pool>(queryNum);
        blockVerifyNum=size_t(blockVerifyCount);
        blockVerifyThreads=make_unique<thread_pool>(blockVerifyNum);
        auto guard=make_work_guard(io);
        auto stop=[&] { io.stop(); };
        signal_set signals(io,SIGINT,SIGTERM);
        signals.async_wait([&](const boost::system::error_code& ec,int){if (!ec) stop();});
        steady_timer timer(io);
        if (runSeconds) {
            timer.expires_after(seconds(runSeconds));
            timer.async_wait([&](const boost::system::error_code& ec){if (!ec) stop();});
        }
        steady_timer stopTimer(io);
        function<void()> pollStop;
        if (!stopFile.empty()) {
            pollStop=[&] {
                if (filesystem::exists(Utf8Path(stopFile))) { stop();return; }
                stopTimer.expires_after(milliseconds(200));
                stopTimer.async_wait([&](boost::system::error_code ec){if (!ec) pollStop();});
            };
            pollStop();
        }
        auto onError=[&](exception_ptr error) {
            if (!error) return;
            try { rethrow_exception(error); } catch (const exception& e) { spdlog::error("Node: {}",e.what()); }
            nodeFailed=true;
            io.stop();
        };
        co_spawn(io,Listen(listenPort,listenAddress),onError);
        if (!listenAddressV6.empty()) co_spawn(io,Listen(listenPort,listenAddressV6),onError);
        co_spawn(make_strand(io),OpenDiscoveryMode(),onError);
        if (observeClock) co_spawn(make_strand(io),SynchronizeClock(),onError);
        spdlog::info("Node ready. IO:{}, Verify:{}, BlockMs:{}, Produce:{}",ioNum,verifyNum,blockInterval,produceBlocks);
        thread blockThread(MainLoop);
        vector<thread> threads;
        for (int i=0;i<ioNum;i++) threads.emplace_back([&] {
            try { io.run(); } catch (...) { onError(current_exception()); }
        });
        for (auto& t:threads) t.join();
        // 先停止网络接收，再完成已经入队的验证，最后提交剩余交易。
        verifyThreads->join();
        candidateThreads->join();
        queryThreads->join();
        queryThreads.reset();
        candidateThreads.reset();
        nodeRunning=false;
        WakeMainLoop();
        blockThread.join();
        blockVerifyThreads->join();
        blockVerifyThreads.reset();
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
        stats["candidate_threads"]=candidateNum;
        stats["block_verify_threads"]=blockVerifyCount;
        stats["query_threads"]=queryNum;
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
            ofstream output(Utf8Path(metricsPath));
            if (!output) throw runtime_error("Cannot write metrics file");
            output<<stats.dump(2)<<endl;
            string dbStats;
            db->GetProperty("rocksdb.stats",&dbStats);
            ofstream dbOutput(Utf8Path(metricsPath+".rocksdb.txt"));
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
