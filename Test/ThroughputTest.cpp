#include <iostream>
#include "../Network/Server.h"
using namespace std;
void Require(bool ok,const char* name) { if (!ok) throw runtime_error(name); }
int main() {
    try {
        if (sodium_init()<0) return 1;
        auto wallet=GenerateWallet(),receiver=GenerateWallet();
        Block block; block.height=2;
        vector<array<uint8_t,32>> hashes;
        for (uint64_t i=1;i<=512;i++) {
            block.txs.push_back(GenerateTx(wallet.public_key,receiver.public_key,1,i,wallet));
            hashes.push_back(GetTransactionHash(block.txs.back()));
        }
        block.merkleRoot=MerkleCompute(hashes); auto bytes=SerializeBlockALL(block);
        blockVerifyNum=4; blockVerifyThreads=make_unique<boost::asio::thread_pool>(4);
        Require(VerifyBlock(bytes)&&parallelSignatureBlocks>0,"cold block parallel validation");
        bytes[112+176*510+112]^=1;
        Require(!VerifyBlock(bytes),"parallel validation rejects altered signature after cache hit");
        blockVerifyThreads->join(); blockVerifyThreads.reset();
        Require(peakBlockVerifyJobs<=8&&blockVerifyJobs==0,"signature worker budget and completion");

        EvmState parent; evmc::address address{}; address.bytes[19]=5;
        auto& account=parent.insert(address); account.nonce=1;
        for (uint64_t i=1;i<=4096;i++) {
            evmc::bytes32 key{},word{}; WriteU64(key.bytes,i); WriteU64(word.bytes,i);
            account.storage[key]={word,word,EVMC_ACCESS_COLD};
        }
        EvmState overlay; overlay.SetBase(parent);
        evmc::bytes32 key{}; WriteU64(key.bytes,2048);
        auto old=overlay.GetStorage(address,key).current;
        Require(overlay.get_accounts().size()==1&&overlay.get(address).storage.size()==1,"overlay loads one account and one requested slot");
        auto checkpoint=overlay.checkpoint(); overlay.journal_storage_change(address,key,overlay.GetStorage(address,key));
        overlay.GetStorage(address,key).current={}; overlay.rollback(checkpoint);
        Require(overlay.ReadStorage(address,key)==old,"overlay storage rollback");
        overlay.GetStorage(address,key).current={}; overlay.ResetTransaction();
        Require(overlay.GetStorage(address,key).original==evmc::bytes32{},"next transaction original storage is the preceding result");
        Require(parent.ReadStorage(address,key)==old&&parent.get(address).storage.size()==4096,"execution leaves parent state unchanged");
        Require(overlay.HasStorage(address),"create collision sees untouched parent slots");
        overlay.EraseAccount(address); overlay.insert(address);
        Require(overlay.ReadStorage(address,key)==evmc::bytes32{}&&!overlay.HasStorage(address),"deleted and recreated account starts with empty storage");

        auto first=SerializeBlockALL(block); block.previousHash=block.hash; block.height++;
        auto second=SerializeBlockALL(block); vector<uint8_t> batch{2,0};
        for (const auto& item:{first,second}) {
            auto offset=batch.size(); batch.resize(offset+4+item.size()); WriteU32(batch.data()+offset,uint32_t(item.size()));
            memcpy(batch.data()+offset+4,item.data(),item.size());
        }
        vector<vector<uint8_t>> decoded;
        Require(ReadBlockBatch(batch,decoded)&&decoded.size()==2,"ordered batch parsing");
        batch[2+4+first.size()+4]^=1; decoded.clear();
        Require(!ReadBlockBatch(batch,decoded),"batch rejects disconnected parent");
        Require(!VerifyMessageSize(40,33)&&!VerifyMessageSize(42,9)&&!VerifyMessageSize(43,117),"compact and batch framing bounds");
        boost::asio::io_context io;
        AnnounceBlock(first);
        Require(recentBlockTransactions.empty(),"local-only node avoids relay cache allocations");
        auto neighbor=make_shared<Peer>(tcp::socket(io));neighbor->port=5500;neighbor->compact=true;
        auto neighborKey=GetPeerKey(neighbor->address,neighbor->port);peerPool.emplace(neighborKey,neighbor);
        AnnounceBlock(first);
        Require(recentBlockTransactions.size()==block.txs.size()&&recentBlockTransactions.at(hashes[0])==vector<uint8_t>(block.txs[0].begin(),block.txs[0].end()),"recent verified transactions remain available after pool removal");
        Require(relayTransactionBytes<=maxRelayTransactionBytes&&recentBlockTransactions.size()<=maxRecentBlockTransactions,"recent relay cache budget");
        peerPool.clear();neighbor.reset();
        weak_ptr<Peer> alive;
        {
            auto peer=make_shared<Peer>(tcp::socket(io)); alive=peer;
            auto task=ProcessMessage(peer,2,first); peer.reset();
            Require(!alive.expired(),"queued verification owns its connection until coroutine completion");
        }
        Require(alive.expired(),"discarded coroutine releases its connection");
        cout<<"Overlay, execution bounds and batch checks passed\n"; return 0;
    } catch (const exception& e) { cerr<<e.what()<<'\n'; return 1; }
}
