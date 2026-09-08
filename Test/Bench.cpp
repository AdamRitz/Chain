#ifdef _WIN32
#include <windows.h>
#endif
#include <fstream>
#include <filesystem>
#include <thread>
#include <nlohmann/json.hpp>
#include "../Transaction/Transaction.h"
using namespace std;

double GetCPUTime() {
#ifdef _WIN32
    FILETIME created,exited,kernel,user;
    if (GetProcessTimes(GetCurrentProcess(),&created,&exited,&kernel,&user)) {
        ULARGE_INTEGER k,u;
        k.LowPart=kernel.dwLowDateTime; k.HighPart=kernel.dwHighDateTime;
        u.LowPart=user.dwLowDateTime; u.HighPart=user.dwHighDateTime;
        return (k.QuadPart+u.QuadPart)/1e7;
    }
#endif
    return 0;
}
int main(int argc,char* argv[]) {
    try {
        if (sodium_init()<0) return 1;
        spdlog::set_level(spdlog::level::off);
        string file,mode="verify",dbPath;
        size_t count=100000,numThreads=1,batchSize=6000;
        bool sync=false;
        for (int i=1;i<argc;i+=2) {
            if (i+1>=argc) throw invalid_argument("Missing argument");
            string name=argv[i],value=argv[i+1];
            if (name=="--file") file=value;
            else if (name=="--mode") mode=value;
            else if (name=="--threads") numThreads=stoull(value);
            else if (name=="--count") count=stoull(value);
            else if (name=="--data") dbPath=value;
            else if (name=="--sync") sync=stoull(value)!=0;
            else if (name=="--batch") batchSize=stoull(value);
            else throw invalid_argument("Unknown argument");
        }
        if (!numThreads||numThreads>64||!count||count>2000000||!batchSize||batchSize>6000) throw invalid_argument("Invalid benchmark limits");
        vector<array<uint8_t,176>> txs(count);
        ifstream input(file,ios::binary);
        if (!input||!input.read(reinterpret_cast<char*>(txs.data()),txs.size()*176)) throw runtime_error("Cannot read dataset");
        atomic<size_t> next{0},success{0};
        atomic<uint64_t> guard{0};
        double cpuStart=GetCPUTime();
        auto start=GetSteadyTime();
        if (mode=="verify"||mode=="hash") {
            vector<thread> threads;
            for (size_t t=0;t<numThreads;t++) threads.emplace_back([&] {
                size_t passed=0;
                uint64_t value=0;
                for (;;) {
                    size_t begin=next.fetch_add(128);
                    if (begin>=txs.size()) break;
                    auto end=min(begin+128,txs.size());
                    for (size_t i=begin;i<end;i++) {
                        if (mode=="verify") passed+=VerifyTransaction(txs[i]);
                        else {
                            array<uint8_t,32> hash;
                            crypto_generichash(hash.data(),32,txs[i].data(),80,nullptr,0);
                            value+=hash[0];
                            passed++;
                        }
                    }
                }
                success+=passed;
                guard+=value;
            });
            for (auto& thread:threads) thread.join();
        } else if (mode=="db-put"||mode=="db-batch") {
            if (dbPath.empty()||filesystem::exists(dbPath)) throw invalid_argument("Use a NEW --data directory");
            InitDB(dbPath,sync);
            cpuStart=GetCPUTime();
            start=GetSteadyTime();
            if (mode=="db-put") {
                for (const auto& tx:txs) DBWriteTx(GetTransactionHash(tx),tx);
            } else {
                for (size_t i=0;i<txs.size();i+=batchSize) {
                    rocksdb::WriteBatch batch;
                    for (size_t j=i;j<min(i+batchSize,txs.size());j++) {
                        const auto& tx=txs[j];
                        batch.Put(DBHashKey("tx/",GetTransactionHash(tx)),rocksdb::Slice(reinterpret_cast<const char*>(tx.data()),176));
                    }
                    DBWriteBatch(batch);
                }
            }
            success=txs.size();
        } else throw invalid_argument("Invalid mode");
        auto elapsed=double(GetSteadyTime()-start)/1e9;
        auto cpu=GetCPUTime()-cpuStart;
        nlohmann::json result={{"mode",mode},{"threads",numThreads},{"count",count},{"success",success.load()},
            {"seconds",elapsed},{"tps",success.load()/elapsed},{"cpu_seconds",cpu},{"average_cpu_cores",cpu/elapsed},
            {"db_sync",sync},{"db_batch",batchSize},{"guard",guard.load()}};
        cout<<result.dump()<<endl;
        db.reset();
        return success==count?0:1;
    } catch (const exception& e) {
        cerr<<e.what()<<endl;
        return 1;
    }
}
