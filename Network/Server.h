#ifndef CHAIN_SERVER_H
#define CHAIN_SERVER_H
#include "Client.h"
using namespace std;
using namespace boost::asio;

// 每个连接的读、写、关闭都在该连接的 strand 上运行。
awaitable<bool> ReadData(const shared_ptr<Peer>& peer,mutable_buffer data,int timeout=30) {
    if (peer->closed) co_return false;
    peer->deadline.expires_after(seconds(timeout));
    peer->deadline.async_wait([peer](boost::system::error_code ec){if (!ec) ClosePeer(peer);});
    boost::system::error_code ec;
    co_await async_read(peer->socket,data,redirect_error(use_awaitable,ec));
    peer->deadline.cancel();
    if (ec) { ClosePeer(peer); co_return false; }
    co_return true;
}
bool ReserveVerify(size_t num) {
    auto current=pendingVerify.load();
    do {
        if (num>maxPendingVerify||current>maxPendingVerify-num) return false;
    } while (!pendingVerify.compare_exchange_weak(current,current+num));
    auto peak=peakPendingVerify.load();
    while (peak<current+num&&!peakPendingVerify.compare_exchange_weak(peak,current+num)) {}
    return true;
}
awaitable<void> ProcessMessage(const shared_ptr<Peer>& peer,uint8_t type,vector<uint8_t> data) {
    auto num=(type==1||type==10)?data.size()/176:(type==17||type==18)?1:max(size_t(1),min(size_t(6000),(data.size()-112)/176));
    if (type==18) {
        num=0; size_t offset=0;
        while (offset<data.size()) {
            if (data.size()-offset<4) { ClosePeer(peer); co_return; }
            auto length=ReadU32(data.data()+offset); offset+=4;
            if (length<221||length>221+maxEvmInput||length>data.size()-offset||++num>6000) { ClosePeer(peer); co_return; }
            offset+=length;
        }
    }
    steady_timer timer(co_await this_coro::executor);
    auto queuedAt=GetSteadyTime();
    uint64_t expected=0;
    if (type==1||type==10) firstTxTime.compare_exchange_strong(expected,queuedAt);
    // 队列满时暂停该连接读取，利用 TCP 背压，不无限堆积异步任务。
    bool reserved=false;
    while (!reserved) {
        auto bytes=pendingVerifyBytes.load();
        if (bytes<=64*1024*1024-data.size()&&pendingVerifyBytes.compare_exchange_weak(bytes,bytes+data.size())) {
            reserved=ReserveVerify(num);
            if (!reserved) pendingVerifyBytes-=data.size();
        }
        if (reserved) break;
        if (peer->closed||!nodeRunning) co_return;
        timer.expires_after(milliseconds(1));
        co_await timer.async_wait(use_awaitable);
    }
    auto messageBytes=data.size();
    post(*verifyThreads,[peer,type,num,messageBytes,queuedAt,data=std::move(data)]() mutable {
        queueWaitNs+=GetSteadyTime()-queuedAt;
        try {
            if (type==1||type==10) {
                auto added=ProcessTxPackage(data);
                if (added&&(!produceBlocks||relayTransactions)) BroadcastData(GenerateMessage(10,data));
            } else if (type==17) {
                if (ProcessEvmTx(data)&&(!produceBlocks||relayTransactions)) BroadcastData(GenerateMessage(17,data));
            } else if (type==18) {
                size_t offset=0;
                while (data.size()-offset>=4) {
                    auto size=ReadU32(data.data()+offset); offset+=4;
                    if (size<221||size>176+45+maxEvmInput||size>data.size()-offset) break;
                    auto tx=span<const uint8_t>(data).subspan(offset,size);
                    if (ProcessEvmTx(tx)&&(!produceBlocks||relayTransactions)) BroadcastData(GenerateMessage(17,tx));
                    offset+=size;
                }
            } else {
                array<uint8_t,32> hash; memcpy(hash.data(),data.data()+80,32);
                auto accepted=ProcessBlock(std::move(data));
                if (peer->port&&(accepted||!FindForkBlock(hash).empty())) {
                    auto missing=FindMissingParent(hash);
                    if (missing!=array<uint8_t,32>{}) SendData(peer,GenerateMessage(21,missing));
                }
            }
        } catch (const exception& e) {
            spdlog::error("Verification worker: {}",e.what());
            nodeFailed=true;
            nodeRunning=false;
            if (nodeIO) nodeIO->stop();
        }
        pendingVerify-=num;
        pendingVerifyBytes-=messageBytes;
        WakeMainLoop();
    });
}
awaitable<void> session(uint64_t key,shared_ptr<Peer> peer) {
    (void)key;
    while (!peer->closed) {
        array<uint8_t,5> header;
        if (!co_await ReadData(peer,buffer(header))) co_return;
        auto type=header[0];
        auto size=ReadU32(header.data()+1);
        if (!VerifyMessageSize(type,size)) { ClosePeer(peer); co_return; }
        vector<uint8_t> data(size);
        if (size&&!co_await ReadData(peer,buffer(data))) co_return;
        networkBytes+=size+5;
        if (type==1||type==10||type==2||type==7||type==17||type==18||type==22) {
            co_await ProcessMessage(peer,type,std::move(data));
        } else if (type==27) {
            auto received=GetLogicalTimeUs();
            array<uint8_t,24> response;
            memcpy(response.data(),data.data(),8);
            WriteU64(response.data()+8,uint64_t(received));
            WriteU64(response.data()+16,uint64_t(GetLogicalTimeUs()));
            SendData(peer,GenerateMessage(28,response));
        } else if (type==28&&peer->port&&peer->clockToken&&ReadU64(data.data())==peer->clockToken) {
            RecordClockSample(peer->key,peer->clockSentLogical,int64_t(ReadU64(data.data()+8)),
                int64_t(ReadU64(data.data()+16)),GetLogicalTimeUs(),int64_t((GetSteadyTime()-peer->clockSentSteady)/1000));
            peer->clockToken=0;
        } else if (type==29||type==31||type==33||type==35||type==37) {
            nlohmann::json reply;
            if (type==33) reply=GetClockStats();
            else {
                shared_lock lock(dbCommitMutex);
                if (type==29) {
                    auto height=ReadU64(data.data()); if (!height) height=DBReadBlockHeight();
                    string hash;
                    if (DBGet("height/"+to_string(height),hash)&&hash.size()==32) {
                        array<uint8_t,32> key; memcpy(key.data(),hash.data(),32);
                        reply={{"height",height},{"hash",U32ToHex(key)},{"transactions",DBReadNumber(DBHashKey("score/",key))}};
                    }
                } else if (type==35) {
                    crypto_generichash_state digest;
                    crypto_generichash_init(&digest,nullptr,0,32);
                    vector<array<uint8_t,32>> keys;
                    for (const auto& [key,user]:users) keys.push_back(key);
                    sort(keys.begin(),keys.end());
                    for (const auto& key:keys) {
                        const auto& user=users.at(key);
                        array<uint8_t,16> bytes; WriteU64(bytes.data(),user.balance); WriteU64(bytes.data()+8,user.nonce);
                        crypto_generichash_update(&digest,key.data(),key.size());
                        crypto_generichash_update(&digest,bytes.data(),bytes.size());
                    }
                    vector<evmc::address> addresses;
                    for (const auto& [address,account]:evmState.get_accounts()) addresses.push_back(address);
                    sort(addresses.begin(),addresses.end());
                    for (const auto& address:addresses) {
                        auto encoded=SerializeEvmAccount(evmState.get_accounts().at(address));
                        crypto_generichash_update(&digest,address.bytes,20);
                        crypto_generichash_update(&digest,reinterpret_cast<const uint8_t*>(encoded.data()),encoded.size());
                    }
                    array<uint8_t,32> hash; crypto_generichash_final(&digest,hash.data(),hash.size());
                    reply={{"state_hash",U32ToHex(hash)},{"head",U32ToHex(DBReadCurrentBlock())},
                        {"transactions",DBReadNumber("ChainTx")},{"supply",genesisSupply-userBurned},{"burned",userBurned}};
                } else if (type==37) {
                    reply=nlohmann::json::array();
                    for (size_t offset=0;offset<data.size();offset+=32) {
                        array<uint8_t,32> key; memcpy(key.data(),data.data()+offset,32);
                        auto found=users.find(key);
                        if (found==users.end()) reply.push_back(nullptr);
                        else reply.push_back({{"balance",found->second.balance},{"nonce",found->second.nonce}});
                    }
                } else {
                    reply=nlohmann::json::array();
                    for (size_t offset=0;offset<data.size();offset+=32) {
                        array<uint8_t,32> key; memcpy(key.data(),data.data()+offset,32);
                        auto height=DBReadNumber(DBHashKey("txheight/",key));
                        string hash;
                        if (height&&DBGet("height/"+to_string(height),hash)&&hash.size()==32) {
                            array<uint8_t,32> block; memcpy(block.data(),hash.data(),32);
                            reply.push_back({{"height",height},{"block",U32ToHex(block)}});
                        } else reply.push_back(nullptr);
                    }
                }
            }
            auto text=reply.dump();
            SendData(peer,GenerateMessage(type+1,span<const uint8_t>(reinterpret_cast<const uint8_t*>(text.data()),text.size())));
        } else if (type==4) {
            SendData(peer,GenerateHeightMessage(5));
        } else if (type==5||type==12) {
            peer->remoteHeight=ReadU64(data.data());
            if (peer->port&&!peer->syncing&&peer->remoteHeight>DBReadBlockHeight()) {
                peer->syncing=true;
                co_spawn(peer->strand,SyncBlock(peer),[peer](exception_ptr error){if (error) PeerError(peer,error);});
            }
        } else if (type==6) {
            SendData(peer,GenerateBlockMessage(ReadU64(data.data())));
        } else if (type==8) {
            SendData(peer,GenerateDiscoverMessage());
        } else if (type==9) {
            co_await ProcessDiscoverMessage(std::move(data));
        } else if (type==11) {
            ProcessTxTimeACKMessage(data,system_clock::now(),peer);
        } else if (type==13) {
            auto stats=GetNodeStats().dump();
            SendData(peer,GenerateMessage(14,span<const uint8_t>(reinterpret_cast<const uint8_t*>(stats.data()),stats.size())));
        } else if (type==15) {
            array<uint8_t,32> key;
            memcpy(key.data(),data.data(),32);
            array<uint8_t,17> reply{};
            {
                shared_lock lock(dbCommitMutex);
                auto found=users.find(key);
                if (found!=users.end()) {
                    reply[0]=1;
                    WriteU64(reply.data()+1,found->second.balance);
                    WriteU64(reply.data()+9,found->second.nonce);
                }
            }
            SendData(peer,GenerateMessage(16,reply));
        } else if (type==19) {
            array<uint8_t,80> reply;
            { shared_lock lock(dbCommitMutex);
              auto head=DBReadCurrentBlock(); memcpy(reply.data(),genesisRoot.data(),32); memcpy(reply.data()+32,head.data(),32);
              WriteU64(reply.data()+64,DBReadBlockHeight()); WriteU64(reply.data()+72,DBReadNumber("ChainTx")); }
            SendData(peer,GenerateMessage(20,reply));
        } else if (type==20&&peer->port) {
            if (memcmp(data.data(),genesisRoot.data(),32)) { ClosePeer(peer); co_return; }
            array<uint8_t,32> remote; memcpy(remote.data(),data.data()+32,32);
            peer->remoteHeight=ReadU64(data.data()+64);
            uint64_t height; array<uint8_t,32> local;
            { shared_lock lock(dbCommitMutex); height=DBReadBlockHeight(); local=DBReadCurrentBlock(); }
            if (local!=remote&&peer->remoteHeight<=height+256) SendData(peer,GenerateMessage(21,remote));
            if (!peer->syncing&&peer->remoteHeight>height) {
                peer->syncing=true;
                co_spawn(peer->strand,SyncBlock(peer),[peer](exception_ptr error){if (error) PeerError(peer,error);});
            }
        } else if (type==21&&peer->port) {
            array<uint8_t,32> hash; memcpy(hash.data(),data.data(),32);
            auto block=FindForkBlock(hash);
            if (!block.empty()) SendData(peer,GenerateMessage(22,block));
        } else if (type==23) {
            array<uint8_t,32> hash; memcpy(hash.data(),data.data(),32);
            string receipt;
            { shared_lock lock(dbCommitMutex); if (!DBGet(DBHashKey("receipt/",hash),receipt)) receipt="null"; }
            SendData(peer,GenerateMessage(24,span<const uint8_t>(reinterpret_cast<const uint8_t*>(receipt.data()),receipt.size())));
        } else if (type==25) {
            evmc::address address; memcpy(address.bytes,data.data(),20);
            nlohmann::json reply=nullptr;
            { shared_lock lock(dbCommitMutex);
              auto found=evmState.get_accounts().find(address);
              if (found!=evmState.get_accounts().end()) {
                if (data.size()==20) reply=nlohmann::json::parse(SerializeEvmAccount(found->second));
                else { evmc::bytes32 key; memcpy(key.bytes,data.data()+20,32);
                       auto slot=found->second.storage.find(key);
                       auto word=slot==found->second.storage.end()?evmc::bytes32{}:slot->second.current;
                       reply=EvmHex(word.bytes); }
              } }
            auto text=reply.dump();
            SendData(peer,GenerateMessage(26,span<const uint8_t>(reinterpret_cast<const uint8_t*>(text.data()),text.size())));
        }
    }
}
awaitable<void> AcceptPeer(shared_ptr<Peer> peer) {
    array<uint8_t,1> type;
    if (!co_await ReadData(peer,buffer(type),3)) co_return;
    if (type[0]!=1&&type[0]!=2) { ClosePeer(peer); co_return; }
    if (type[0]==1) {
        array<uint8_t,2> port;
        if (!co_await ReadData(peer,buffer(port),3)) co_return;
        peer->port=uint16_t(port[0])|(uint16_t(port[1])<<8);
        if (!peer->port) { ClosePeer(peer); co_return; }
        auto endpoint=peer->socket.remote_endpoint();
        peer->key=GetPeerKey(endpoint.address().to_v4().to_bytes(),endpoint.port());
        peer->address=endpoint.address().to_v4().to_bytes();
        {
            lock_guard lock(peerMutex);
            if (peerPool.size()>=maxPeers||peerPool.count(peer->key)) { peer->key=0; co_return; }
            peerPool.emplace(peer->key,peer);
        }
        QueryHeight(peer);
    }
    co_await session(peer->key,peer);
}
awaitable<void> Listen(unsigned short port) {
    auto executor=co_await this_coro::executor;
    tcp::acceptor acceptor(executor,tcp::endpoint(ip::make_address_v4(listenAddress),port));
    spdlog::info("Server started on {}",port);
    while (nodeRunning) {
        boost::system::error_code ec;
        auto socket=co_await acceptor.async_accept(redirect_error(use_awaitable,ec));
        if (ec) {
            if (!nodeRunning) co_return;
            throw boost::system::system_error(ec);
        }
        if (connectionCount>=maxConnections) { socket.close(ec); continue; }
        socket.set_option(tcp::no_delay(true),ec);
        auto peer=make_shared<Peer>(std::move(socket));
        co_spawn(peer->strand,AcceptPeer(peer),[peer](exception_ptr error){PeerError(peer,error);});
    }
}
#endif
