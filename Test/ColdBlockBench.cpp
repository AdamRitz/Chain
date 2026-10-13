// 冷区块验签：交易生成在计时区间之外，每次运行使用新的签名。
#include <iostream>
#include "../Transaction/Block.h"
#include "../Tool/Resource.h"
using namespace std;
int main(int argc,char** argv) {
    try {
        if (sodium_init()<0) return 1;
        size_t workers=argc>1?stoull(argv[1]):4,blocks=argc>2?stoull(argv[2]):20;
        if (!workers||workers>4||!blocks||blocks>1000) throw invalid_argument("Invalid benchmark limits");
        auto owner=GenerateWallet(),receiver=GenerateWallet();vector<vector<uint8_t>> data;
        for (size_t b=0;b<blocks;b++) {
            Block block; block.height=b+2;vector<array<uint8_t,32>> hashes;
            for (size_t i=0;i<6000;i++) {
                block.txs.push_back(GenerateTx(owner.public_key,receiver.public_key,1,b*6000+i+1,owner));
                hashes.push_back(GetTransactionHash(block.txs.back()));
            }
            block.merkleRoot=MerkleCompute(hashes);data.push_back(SerializeBlockALL(block));
        }
        blockVerifyNum=workers;blockVerifyThreads=make_unique<boost::asio::thread_pool>(workers);
        auto cpu=GetProcessResources().value("process_cpu_seconds",0.0); auto start=GetSteadyTime();
        for (const auto& block:data) if (!VerifyBlock(block)) throw runtime_error("Cold block rejected");
        auto seconds=(GetSteadyTime()-start)/1e9;
        auto cpuSeconds=GetProcessResources().value("process_cpu_seconds",0.0)-cpu;
        blockVerifyThreads->join();blockVerifyThreads.reset();
        cout<<nlohmann::json({{"workers",workers},{"blocks",blocks},{"transactions",blocks*6000},{"seconds",seconds},
              {"verify_tps",blocks*6000/seconds},{"cpu_seconds",cpuSeconds},{"peak_jobs",peakBlockVerifyJobs.load()}}).dump()<<'\n';return 0;
    } catch (const exception& e) {cerr<<e.what()<<'\n';return 1;}
}
