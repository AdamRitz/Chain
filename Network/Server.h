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
mutex blockWorkMutex;
struct BlockWork;
unordered_map<array<uint8_t,32>,weak_ptr<BlockWork>,GetMapHash> blockWork;
size_t blockWorkBytes=0;
struct BlockWork {
    array<uint8_t,32> fingerprint;
    vector<uint8_t> data;
    bool registered=false;
    ~BlockWork() {
        if (!registered) return;
        lock_guard lock(blockWorkMutex);
        auto found=blockWork.find(fingerprint);
        if (found!=blockWork.end()&&found->second.expired()) blockWork.erase(found);
        blockWorkBytes-=data.size();
    }
};
array<uint8_t,32> GetBlockFingerprint(const vector<uint8_t>& data,bool batch) {
    array<uint8_t,32> fingerprint; crypto_generichash_state hash;
    uint8_t format=batch;
    crypto_generichash_init(&hash,nullptr,0,32); crypto_generichash_update(&hash,&format,1);
    crypto_generichash_update(&hash,data.data(),data.size()); crypto_generichash_final(&hash,fingerprint.data(),32);
    return fingerprint;
}
shared_ptr<BlockWork> ReserveBlockWork(const vector<uint8_t>& data,const array<uint8_t,32>& fingerprint,bool& duplicate) {
    shared_ptr<BlockWork> known;
    lock_guard lock(blockWorkMutex);
    auto found=blockWork.find(fingerprint);
    if (found!=blockWork.end()) known=found->second.lock();
    if (known&&known->data==data) { duplicate=true; duplicateBlockWork++; return {}; }
    if (blockWork.size()>=64||data.size()>32*1024*1024-blockWorkBytes) return {};
    auto work=make_shared<BlockWork>(); work->fingerprint=fingerprint; work->data=data;
    blockWork[fingerprint]=work; blockWorkBytes+=data.size(); work->registered=true; return work;
}
shared_ptr<BlockWork> ReserveBlockWork(const vector<uint8_t>& data,bool batch,bool& duplicate) {
    return ReserveBlockWork(data,GetBlockFingerprint(data,batch),duplicate);
}
bool ReserveVerify(size_t num,bool block=false,bool metadata=false) {
    auto& pending=metadata?pendingMetadata:(block?pendingBlockVerify:pendingTxVerify);
    auto limit=metadata?size_t(64):(block?maxProtocolTx*4:maxPendingVerify);
    auto current=pending.load();
    do {
        if (num>limit||current>limit-num) return false;
    } while (!pending.compare_exchange_weak(current,current+num));
    auto total=pendingVerify.fetch_add(num)+num;
    auto peak=peakPendingVerify.load();
    while (peak<total&&!peakPendingVerify.compare_exchange_weak(peak,total)) {}
    return true;
}
void ReleaseVerify(size_t num,bool block=false,bool metadata=false) {
    (metadata?pendingMetadata:(block?pendingBlockVerify:pendingTxVerify))-=num; pendingVerify-=num;
}
struct VerifyReservation {
    size_t count=0,bytes=0; bool block=false,metadata=false,active=false;
    ~VerifyReservation() {
        if (!active) return;
        ReleaseVerify(count,block,metadata);
        (block?pendingBlockBytes:pendingTxBytes)-=bytes; pendingVerifyBytes-=bytes;
    }
};
awaitable<void> ProcessMessage(shared_ptr<Peer> peer,uint8_t type,vector<uint8_t> data) {
    vector<vector<uint8_t>> blocks;
    if (type==43&&!ReadBlockBatch(data,blocks)) { ClosePeer(peer); co_return; }
    auto num=(type==1||type==10||type==44)?data.size()/176:(type==17||type==18||type==43)?1:max(size_t(1),min(maxProtocolTx,(data.size()-112)/176));
    if (type==43) {
        num=0;
        for (const auto& block:blocks) num+=max(uint64_t(1),min(uint64_t(maxProtocolTx),ReadU64(block.data()+72)));
    }
    if (type==18) {
        num=0; size_t offset=0;
        while (offset<data.size()) {
            if (data.size()-offset<4) { ClosePeer(peer); co_return; }
            auto length=ReadU32(data.data()+offset); offset+=4;
            if (length<221||length>221+maxEvmInput||length>data.size()-offset||++num>maxProtocolTx) { ClosePeer(peer); co_return; }
            offset+=length;
        }
    }
    bool metadata=type==39||type==41;
    if (metadata) num=1;
    bool blockMessage=type!=1&&type!=10&&type!=17&&type!=18&&type!=44;
    auto& byteCounter=blockMessage?pendingBlockBytes:pendingTxBytes;
    auto byteLimit=blockMessage?size_t(16*1024*1024):size_t(48*1024*1024);
    steady_timer timer(co_await this_coro::executor);
    auto queuedAt=GetSteadyTime();
    uint64_t expected=0;
    if (type==1||type==10) firstTxTime.compare_exchange_strong(expected,queuedAt);
    // 队列满时暂停该连接读取，利用 TCP 背压，不无限堆积异步任务。
    if (num>(metadata?size_t(64):(blockMessage?maxProtocolTx*4:maxPendingVerify))) { ClosePeer(peer); co_return; }
    shared_ptr<BlockWork> work;
    if (type==2||type==7||type==22||type==43) {
        auto fingerprint=GetBlockFingerprint(data,type==43);
        while (!work) {
            bool duplicate=false; work=ReserveBlockWork(data,fingerprint,duplicate);
            if (duplicate) co_return;
            if (work) break;
            if (peer->closed||!nodeRunning) co_return;
            timer.expires_after(milliseconds(1)); co_await timer.async_wait(use_awaitable);
        }
    }
    if ((type==1||type==10)&&produceBlocks&&!peer->port) {
        auto waitStart=GetSteadyTime();
        while (true) {
            bool busy;
            { lock_guard lock(txpoolMutex); busy=readyPoolTx>0&&txpool.size()>=maxPoolTx-max(size_t(1),maxPoolTx/10); }
            if (!busy||peer->closed||!nodeRunning) break;
            timer.expires_after(milliseconds(1)); co_await timer.async_wait(use_awaitable);
        }
        ingressWaitNs+=GetSteadyTime()-waitStart;
        if (peer->closed||!nodeRunning) co_return;
    }
    bool reserved=false;
    auto reservation=make_shared<VerifyReservation>();
    reservation->count=num; reservation->bytes=data.size(); reservation->block=blockMessage; reservation->metadata=metadata;
    while (!reserved) {
        auto bytes=byteCounter.load();
        if (data.size()<=byteLimit&&bytes<=byteLimit-data.size()&&byteCounter.compare_exchange_weak(bytes,bytes+data.size())) {
            reserved=ReserveVerify(num,blockMessage,metadata);
            if (!reserved) byteCounter-=data.size();
            else pendingVerifyBytes+=data.size();
        }
        if (reserved) { reservation->active=true; break; }
        if (peer->closed||!nodeRunning) co_return;
        timer.expires_after(milliseconds(1));
        co_await timer.async_wait(use_awaitable);
    }
    auto admissionOrder=type==44?peer->admissionRead++:0;
    auto& workers=blockMessage&&candidateThreads?*candidateThreads:*verifyThreads;
    post(workers,[peer,type,queuedAt,blockMessage,metadata,admissionOrder,reservation=std::move(reservation),work=std::move(work),data=std::move(data),blocks=std::move(blocks)]() mutable {
        (void)work;
        auto wait=GetSteadyTime()-queuedAt; queueWaitNs+=wait;
        (metadata?metadataQueueWaitNs:(blockMessage?blockQueueWaitNs:txQueueWaitNs))+=wait;
        if (metadata) metadataTasks++; else if (blockMessage) blockTasks++;
        try {
            if (type==1||type==10||type==44) {
                vector<uint8_t> results;
                auto added=ProcessTxPackage(data,type==44?&results:nullptr);
                if (type==44) SendAdmission(peer,admissionOrder,std::move(results));
                if (added&&(!produceBlocks||relayTransactions)) BroadcastData(GenerateMessage(10,data),PeerId(peer));
            } else if (type==17) {
                if (ProcessEvmTx(data)&&(!produceBlocks||relayTransactions)) BroadcastData(GenerateMessage(17,data),PeerId(peer));
            } else if (type==18) {
                size_t offset=0;
                while (data.size()-offset>=4) {
                    auto size=ReadU32(data.data()+offset); offset+=4;
                    if (size<221||size>176+45+maxEvmInput||size>data.size()-offset) break;
                    auto tx=span<const uint8_t>(data).subspan(offset,size);
                    if (ProcessEvmTx(tx)&&(!produceBlocks||relayTransactions)) BroadcastData(GenerateMessage(17,tx),PeerId(peer));
                    offset+=size;
                }
            } else if (type==39) ReceiveCompactBlock(peer,data);
            else if (type==41) ReceiveMissingTransactions(peer,data);
            else {
                if (type!=43) blocks.push_back(std::move(data));
                bool queuedBatch=false;
                for (auto& block:blocks) {
                    array<uint8_t,32> hash; memcpy(hash.data(),block.data()+80,32);
                    bool accepted=false;
                    if (type==43) {
                        Block parsed; auto height=ReadU64(block.data()+64);
                        uint64_t current; bool known;
                        { shared_lock lock(dbCommitMutex);current=DBReadBlockHeight();known=DBReadBlockByHeight(to_string(height))==block; }
                        if (!known&&!HasForkBlock(hash,block)&&height>=2&&height<=current+256&&current<=height+256&&VerifyBlock(block,&parsed)) {
                            accepted=QueueForkBlock(std::move(parsed),std::move(block),false); queuedBatch|=accepted;
                        }
                    } else accepted=ProcessBlock(std::move(block));
                    CompleteBlockReceive(hash);
                    if (peer->port&&(accepted||KnowsForkBlock(hash))) {
                        RememberBlockSource(hash,PeerId(peer));
                        if (type==43) syncBlocks++;
                        auto missing=FindMissingParent(hash);
                        if (missing!=array<uint8_t,32>{}) SendData(peer,GenerateMessage(21,missing));
                    }
                }
                if (queuedBatch) { forkReady=true; WakeMainLoop(); }
            }
        } catch (const exception& e) {
            spdlog::error("Verification worker: {}",e.what());
            nodeFailed=true;
            nodeRunning=false;
            if (nodeIO) nodeIO->stop();
        }
        reservation.reset(); work.reset();
        WakeMainLoop();
    });
}
vector<uint8_t> ReadQueryResponse(uint8_t type,const vector<uint8_t>& data) {
    if (type==13) {
        auto stats=GetNodeStats().dump();
        return GenerateMessage(14,span<const uint8_t>(reinterpret_cast<const uint8_t*>(stats.data()),stats.size()));
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
        return GenerateMessage(16,reply);
    } else if (type==19) {
        array<uint8_t,80> reply;
        auto head=ReadChainHead(); memcpy(reply.data(),genesisRoot.data(),32); memcpy(reply.data()+32,head.hash.data(),32);
        WriteU64(reply.data()+64,head.height); WriteU64(reply.data()+72,head.transactions);
        return GenerateMessage(20,reply);
    } else if (type==23) {
        array<uint8_t,32> hash; memcpy(hash.data(),data.data(),32);
        string receipt;
        { shared_lock lock(dbCommitMutex); if (!DBGet(DBHashKey("receipt/",hash),receipt)) receipt="null"; }
        return GenerateMessage(24,span<const uint8_t>(reinterpret_cast<const uint8_t*>(receipt.data()),receipt.size()));
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
        return GenerateMessage(26,span<const uint8_t>(reinterpret_cast<const uint8_t*>(text.data()),text.size()));
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
                    uint64_t height=0; uint32_t index=0; array<uint8_t,32> block;
                    if (experimentIndex&&DBReadTxPosition(key,height,block,index)) {
                        reply.push_back({{"height",height},{"block",U32ToHex(block)}});
                    } else reply.push_back(nullptr);
                }
            }
        }
        auto text=reply.dump();
        return GenerateMessage(type+1,span<const uint8_t>(reinterpret_cast<const uint8_t*>(text.data()),text.size()));

    }
    throw invalid_argument("Unknown query type");
}
awaitable<void> ProcessQuery(shared_ptr<Peer> peer,uint8_t type,vector<uint8_t> data) {
    steady_timer timer(co_await this_coro::executor); auto queued=GetSteadyTime();
    auto pending=pendingQueries.load();
    while (true) {
        if (pending<128&&pendingQueries.compare_exchange_weak(pending,pending+1)) break;
        if (peer->closed||!nodeRunning) co_return;
        timer.expires_after(milliseconds(1)); co_await timer.async_wait(use_awaitable);
        pending=pendingQueries.load();
    }
    post(*queryThreads,[peer,type,queued,data=std::move(data)] {
        queryWaitNs+=GetSteadyTime()-queued;
        try { SendData(peer,ReadQueryResponse(type,data)); }
        catch (const exception& e) {
            spdlog::warn("Query worker: {}",e.what()); auto error=current_exception();
            post(peer->strand,[peer,error] { PeerError(peer,error); });
        }
        pendingQueries--;
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
        if (type==1||type==10||type==44||type==2||type==7||type==17||type==18||type==22) {
            co_await ProcessMessage(peer,type,std::move(data));
        } else if (type==39&&peer->port) {
            co_await ProcessMessage(peer,type,std::move(data));
        } else if (type==40&&peer->port) {
            co_await SendBlockReply(peer,type,std::move(data));
        } else if (type==41&&peer->port) {
            co_await ProcessMessage(peer,type,std::move(data));
        } else if (type==42&&peer->port) {
            if (peer->compact) co_await SendBlockReply(peer,type,std::move(data));
        } else if (type==43&&peer->compact) {
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
        } else if (type==13||type==15||type==19||type==23||type==25||type==29||type==31||type==33||type==35||type==37) {
            co_await ProcessQuery(peer,type,std::move(data));
        } else if (type==4) {
            SendData(peer,GenerateHeightMessage(5));
        } else if (type==5||type==12) {
            peer->remoteHeight=ReadU64(data.data());
            if (peer->port&&!peer->syncing&&peer->remoteHeight>DBReadBlockHeight()) {
                StartSync(peer);
            }
        } else if (type==6) {
            co_await SendBlockReply(peer,type,std::move(data));
        } else if (type==8) {
            SendData(peer,GenerateDiscoverMessage());
        } else if (type==9) {
            co_await ProcessDiscoverMessage(std::move(data));
        } else if (type==11) {
            ProcessTxTimeACKMessage(data,system_clock::now(),peer);
        } else if (type==20&&peer->port) {
            if (memcmp(data.data(),genesisRoot.data(),32)) { ClosePeer(peer); co_return; }
            array<uint8_t,32> remote; memcpy(remote.data(),data.data()+32,32);
            peer->remoteHeight=ReadU64(data.data()+64);
            auto localHead=ReadChainHead(); auto height=localHead.height; auto local=localHead.hash;
            if (local!=remote&&(peer->remoteHeight<=height||peer->remoteHeight-height<=256)&&peer->requestedHead!=remote) {
                peer->requestedHead=remote; SendData(peer,GenerateMessage(21,remote));
            }
            if (!peer->syncing&&peer->remoteHeight>height) {
                StartSync(peer);
            }
        } else if (type==21&&peer->port) {
            co_await SendBlockReply(peer,type,std::move(data));
        }
    }
}
awaitable<void> AcceptPeer(shared_ptr<Peer> peer) {
    array<uint8_t,1> type;
    if (!co_await ReadData(peer,buffer(type),3)) co_return;
    if (type[0]!=1&&type[0]!=2&&type[0]!=3) { ClosePeer(peer); co_return; }
    if (type[0]==1||type[0]==3) {
        peer->compact=type[0]==3;
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
