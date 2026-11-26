#include <filesystem>
#include <iostream>
#include <future>
#include "../Network/Server.h"
using namespace std;
void Require(bool value,const char* message) { if (!value) throw runtime_error(message); }
struct Connection {
    shared_ptr<Peer> peer;
    tcp::socket reader;
    Connection(boost::asio::io_context& io):reader(io) {
        tcp::acceptor acceptor(io,tcp::endpoint(ip::address_v4::loopback(),0));
        reader.connect(acceptor.local_endpoint());tcp::socket socket(io);acceptor.accept(socket);
        peer=make_shared<Peer>(std::move(socket));peer->key=NewPeerKey();peer->port=acceptor.local_endpoint().port();
        peer->address=ip::address_v4::loopback();peer->txInventory=true;
    }
};
vector<pair<uint8_t,vector<uint8_t>>> ReadMessages(boost::asio::io_context& io,Connection& connection,size_t expected=1) {
    vector<uint8_t> bytes;auto deadline=steady_clock::now()+seconds(2);
    do {
        io.restart();io.poll();
        auto available=connection.reader.available();
        if (available) {auto offset=bytes.size();bytes.resize(offset+available);auto read=connection.reader.read_some(buffer(bytes.data()+offset,available));bytes.resize(offset+read);}
        size_t complete=0,offset=0;
        while (bytes.size()-offset>=5) {
            auto length=ReadU32(bytes.data()+offset+1);if (length>bytes.size()-offset-5) break;
            offset+=5+length;complete++;
        }
        if (complete>=expected&&!connection.peer->writing&&connection.peer->messageQueue.Empty()) break;
        this_thread::yield();
    } while (steady_clock::now()<deadline);
    // Loopback reads may be delivered immediately after the write completion.
    for (int i=0;i<4;++i) {
        auto available=connection.reader.available();if (!available) break;
        auto offset=bytes.size();bytes.resize(offset+available);auto read=connection.reader.read_some(buffer(bytes.data()+offset,available));bytes.resize(offset+read);
    }
    vector<pair<uint8_t,vector<uint8_t>>> result;size_t offset=0;
    while (offset<bytes.size()) {
        Require(bytes.size()-offset>=5,"complete inventory frame header");auto length=ReadU32(bytes.data()+offset+1);
        Require(length<=bytes.size()-offset-5,"complete inventory frame payload");
        result.emplace_back(bytes[offset],vector<uint8_t>(bytes.begin()+offset+5,bytes.begin()+offset+5+length));offset+=5+length;
    }
    return result;
}
vector<uint8_t> MakeEvmBody(const Wallet& wallet,uint64_t nonce=1) {
    vector<uint8_t> payload(46,0);memcpy(payload.data(),"EVM1",4);WriteU64(payload.data()+5,100000);
    memcpy(payload.data()+13,genesisRoot.data(),32);payload.back()=0;
    array<uint8_t,176> tx{};memcpy(tx.data(),wallet.public_key.data(),32);WriteU64(tx.data()+72,nonce);
    auto hash=EvmTransactionHash(tx,payload);memcpy(tx.data()+80,hash.data(),32);
    crypto_sign_detached(tx.data()+112,nullptr,hash.data(),32,wallet.private_key.data());
    vector<uint8_t> body(tx.begin(),tx.end());body.insert(body.end(),payload.begin(),payload.end());return body;
}
// Hold the only verification worker so every duplicate is received while its
// original transaction is still queued. No timing assumption is needed.
struct PausedVerification {
    promise<void> released;
    bool finished=false;
    PausedVerification() {
        Require(!verifyThreads,"verification pool must be idle before test");
        verifyThreads=make_unique<thread_pool>(1);
        auto ready=released.get_future().share();post(*verifyThreads,[ready]{ready.wait();});
    }
    void Finish() {
        if (finished) return;
        released.set_value();verifyThreads->join();verifyThreads.reset();finished=true;
    }
    ~PausedVerification() { Finish(); }
};
vector<uint8_t> NativeBatch(initializer_list<array<uint8_t,176>> bodies) {
    vector<uint8_t> result;for (const auto& body:bodies) result.insert(result.end(),body.begin(),body.end());return result;
}
vector<uint8_t> EvmBatch(initializer_list<vector<uint8_t>> bodies) {
    vector<uint8_t> result;
    for (const auto& body:bodies) {auto at=result.size();result.resize(at+4);WriteU32(result.data()+at,uint32_t(body.size()));result.insert(result.end(),body.begin(),body.end());}
    return result;
}
void SubmitWork(boost::asio::io_context& io,Connection& connection,uint8_t kind,vector<uint8_t> body) {
    co_spawn(connection.peer->strand,ProcessMessage(connection.peer,kind,std::move(body)),detached);
    io.restart();io.poll();
}
void RequireAck(const pair<uint8_t,vector<uint8_t>>& frame,initializer_list<uint8_t> expected) {
    Require(frame.first==45&&frame.second==vector<uint8_t>(expected),"type44 response preserves every position and request order");
}
void TestMergedWork(boost::asio::io_context& io,const Wallet& wallet,const Wallet& receiver,const Wallet& evmWallet) {
    // Keep relay output away from these admission-response assertions.
    {lock_guard lock(peerMutex);peerPool.clear();}
    auto a=GenerateTx(wallet.public_key,receiver.public_key,1,10,wallet);
    auto b=GenerateTx(wallet.public_key,receiver.public_key,1,11,wallet);
    auto c=GenerateTx(wallet.public_key,receiver.public_key,1,12,wallet);
    auto bad=a;bad[112]^=1;
    Connection admission(io);
    auto validBefore=validTx.load(),invalidBefore=invalidTx.load(),signaturesBefore=signatureChecks.load();
    {
        PausedVerification workers;
        SubmitWork(io,admission,44,NativeBatch({bad,a,a,b,b}));
        Require(pendingTxVerify==3&&pendingTxBytes==3*176,"same batch reserves only its three distinct complete bodies");
        SubmitWork(io,admission,44,NativeBatch({a,b,a}));
        Require(pendingTxVerify==3,"all-duplicate batch adds no verification work");
        SubmitWork(io,admission,44,NativeBatch({b,c,bad}));
        Require(pendingTxVerify==4&&pendingTxBytes==4*176,"mixed batch reserves only its one new complete body");
        workers.Finish();
    }
    auto replies=ReadMessages(io,admission,3);
    Require(replies.size()==3,"each original admission batch receives one response");
    RequireAck(replies[0],{TxInvalid,TxAccepted,TxKnown,TxAccepted,TxKnown});
    RequireAck(replies[1],{TxKnown,TxKnown,TxKnown});
    RequireAck(replies[2],{TxKnown,TxAccepted,TxInvalid});
    Require(validTx-validBefore==3&&invalidTx-invalidBefore==1,"merged work counts unique accepted and invalid bodies once");
    Require(signatureChecks-signaturesBefore==4,"bad signature does not hide the valid body sharing its transaction hash");
    Require(pendingTxVerify==0&&pendingTxBytes==0&&txReceivingBytes==0,"completed batches release verification and receipt quotas");
    {
        PausedVerification workers;
        auto checks=signatureChecks.load();
        SubmitWork(io,admission,44,NativeBatch({a,b,a}));
        Require(pendingTxVerify==0,"verified pool duplicates skip verification admission");
        workers.Finish();
        auto known=ReadMessages(io,admission);
        Require(known.size()==1,"all-known batch still receives acknowledgement");
        RequireAck(known[0],{TxKnown,TxKnown,TxKnown});
        Require(signatureChecks==checks,"known-body response does not repeat signature checks");
    }
    {
        auto body=GenerateTx(wallet.public_key,receiver.public_key,1,13,wallet);
        Connection cancelled(io),follower(io);
        auto limit=maxPendingVerify;maxPendingVerify=1;pendingTxVerify=1;
        PausedVerification workers;
        SubmitWork(io,cancelled,44,NativeBatch({body}));
        SubmitWork(io,follower,44,NativeBatch({body,body}));
        Require(pendingTxVerify==1,"duplicate waiters do not reserve an occupied verification slot");
        cancelled.peer->closed=true;
        io.restart();io.run_for(milliseconds(20));
        Require(txReceivingBytes==176,"duplicate source keeps complete body available after original connection closes");
        pendingTxVerify=0;maxPendingVerify=limit;
        auto until=steady_clock::now()+seconds(2);
        while (pendingTxVerify==0&&steady_clock::now()<until) {io.restart();io.run_for(milliseconds(10));}
        Require(pendingTxVerify==1,"duplicate source allows queued body to continue after original connection closes");
        workers.Finish();
        auto completed=ReadMessages(io,follower);
        Require(completed.size()==1,"surviving duplicate source receives its complete admission response");RequireAck(completed[0],{TxKnown,TxKnown});
        Require(txReceivingBytes==0,"completed work releases the original receipt after connection cancellation");
        cancelled.peer->socket.close();follower.peer->socket.close();
    }
    {
        auto body=GenerateTx(wallet.public_key,receiver.public_key,1,14,wallet);
        auto poolLimit=maxPoolTx;
        {lock_guard lock(txpoolMutex);maxPoolTx=txpool.size();}
        {
            PausedVerification workers;SubmitWork(io,admission,44,NativeBatch({body,body}));workers.Finish();
        }
        maxPoolTx=poolLimit;
        auto busy=ReadMessages(io,admission);
        Require(busy.size()==1,"full transaction pool returns a complete batch response");RequireAck(busy[0],{TxBusy,TxBusy});
        {
            PausedVerification workers;SubmitWork(io,admission,44,NativeBatch({body}));
            Require(pendingTxVerify==1,"busy response is not retained as a completed duplicate");workers.Finish();
        }
        auto retry=ReadMessages(io,admission);Require(retry.size()==1,"pool retry has an acknowledgement");RequireAck(retry[0],{TxAccepted});
    }
    {
        auto body=GenerateTx(wallet.public_key,receiver.public_key,1,15,wallet);
        auto accepted=validTx.load(),checks=signatureChecks.load();
        PausedVerification workers;
        SubmitWork(io,admission,10,NativeBatch({body,body}));
        SubmitWork(io,admission,44,NativeBatch({body}));
        Require(pendingTxVerify==1,"ordinary relay and admission share one verification owner");
        workers.Finish();
        auto reply=ReadMessages(io,admission);
        Require(reply.size()==1,"type44 duplicate of ordinary relay receives a response");RequireAck(reply[0],{TxKnown});
        Require(validTx-accepted==1&&signatureChecks-checks==1,"ordinary and admission duplicates count one accepted transaction");
    }
    {
        auto body=MakeEvmBody(evmWallet,2),badBody=body;badBody[112]^=1;
        auto accepted=validTx.load(),invalid=invalidTx.load(),checks=signatureChecks.load();
        PausedVerification workers;
        SubmitWork(io,admission,18,EvmBatch({badBody,body,body}));
        SubmitWork(io,admission,17,body);
        Require(pendingTxVerify==2,"EVM batch and single-body duplicates share complete-body verification");
        workers.Finish();io.restart();io.poll();
        Require(validTx-accepted==1&&invalidTx-invalid==1&&signatureChecks-checks==2,"EVM signature variants are verified independently and exact duplicates once");
        auto key=TransactionInventoryKey(body,true);
        Require(ReadTxRelayBody(key)&&*ReadTxRelayBody(key)==body,"EVM accepted cache retains the complete input");
    }
    {
        auto first=GenerateTx(wallet.public_key,receiver.public_key,1,16,wallet);
        auto second=GenerateTx(wallet.public_key,receiver.public_key,1,17,wallet);
        auto bytes=NativeBatch({first,second});vector<TxWorkInput> inputs;
        Require(ParseTxWork(10,bytes,inputs),"capacity test batch parses completely");
        {lock_guard lock(txWorkMutex);Require(txWork.empty()&&txWorkBytes==0,"capacity test starts without active owners");txWorkBytes=maxTxWorkBytes-176;}
        auto limited=TryReserveTxWork(admission.peer,false,inputs,bytes);
        Require(!limited,"whole batch waits when only one of two bodies fits");
        {lock_guard lock(txWorkMutex);Require(txWork.empty()&&txWorkBytes==maxTxWorkBytes-176,"failed batch registration leaves no partial owner or byte charge");txWorkBytes=0;}
        auto retry=TryReserveTxWork(admission.peer,false,inputs,bytes);
        Require(retry&&retry->owners.size()==2,"batch can retry both bodies after capacity returns");retry.reset();
    }
    {
        User previous;
        {unique_lock lock(poolUsersMutex);previous=poolUsers.at(wallet.public_key);poolUsers.at(wallet.public_key).balance=0;}
        {lock_guard lock(txpoolMutex);DropUserPoolFrom(wallet.public_key,10);}
        // DropUserPoolFrom counts the removed pending transactions as invalid;
        // measure only the subsequent replay against the changed account.
        auto checks=signatureChecks.load(),invalid=invalidUserTx.load();
        {
            PausedVerification workers;SubmitWork(io,admission,44,NativeBatch({a}));
            Require(pendingTxVerify==1,"verified body absent from pool still enters account validation");workers.Finish();
        }
        {unique_lock lock(poolUsersMutex);poolUsers.at(wallet.public_key)=previous;}
        auto reply=ReadMessages(io,admission);
        Require(reply.size()==1,"state-invalid replay receives an admission response");RequireAck(reply[0],{TxInvalid});
        Require(signatureChecks==checks&&invalidUserTx-invalid==1,"cached signature does not bypass the changed account balance");
        {lock_guard lock(txpoolMutex);Require(!txpool.count(GetTransactionHash(a)),"state-invalid transaction is not restored to the pool");}
    }
    Require(pendingTxVerify==0&&pendingTxBytes==0&&txReceivingBytes==0,"all merged-work tests release bounded quotas");
    auto active=TxWorkStats();
    Require(active["tx_work_active"]==0&&active["tx_work_bytes"]==0&&active["tx_work_waiters"]==0,
        "completed and cancelled work releases registry records, bytes and response slots");
    admission.peer->socket.close();
}
int main(int argc,char** argv) {
    try {
        if (sodium_init()<0) return 1;
        {
            vector<uint8_t> block(112+176,0);block[112+112]=1;
            Require(blockWork.empty()&&blockWorkBytes==0,"block-work test starts without live tasks");
            bool duplicate=false;auto owner=ReserveBlockWork(block,false,duplicate);
            Require(owner&&!duplicate&&owner->bytes==block.size()&&blockWorkBytes==block.size(),"full block registers one hash and its byte charge");
            auto same=ReserveBlockWork(block,false,duplicate);
            Require(!same&&duplicate&&blockWorkBytes==block.size(),"same complete block hash merges without a second byte charge");
            auto changed=block;changed[112+112]^=1;duplicate=false;
            auto altered=ReserveBlockWork(changed,false,duplicate);
            Require(altered&&!duplicate&&altered->fingerprint!=owner->fingerprint,"changed transaction signature creates distinct block work");
            duplicate=false;auto batch=ReserveBlockWork(block,true,duplicate);
            Require(batch&&!duplicate&&batch->fingerprint!=owner->fingerprint,"batch format is part of the work hash");
            Require(blockWorkBytes==block.size()*3&&blockWork.size()==3,"all distinct block tasks charge their full size");
            owner.reset();Require(blockWorkBytes==block.size()*2,"owner destruction releases its byte charge once");
            altered.reset();batch.reset();Require(blockWorkBytes==0&&blockWork.empty(),"all block work is released after processing");
        }
        TxInventoryKey nativeKey{},evmKey{};nativeKey.hash[0]=7;evmKey=nativeKey;evmKey.kind=1;
        vector<TxInventoryKey> keys{nativeKey,evmKey,nativeKey},parsed;
        auto encoded=EncodeTxInventory(keys);
        Require(DecodeTxInventory(encoded,parsed)&&parsed.size()==2&&parsed[0]==nativeKey&&parsed[1]==evmKey,
            "inventory codec deduplicates by hash and input kind");
        Require(!DecodeTxInventory({},parsed)&&!DecodeTxInventory(span<const uint8_t>(encoded).first(32),parsed),"truncated inventory rejected");
        auto malformed=encoded;malformed[66]=2;
        Require(!DecodeTxInventory(malformed,parsed)&&parsed.empty(),"bad trailing kind is rejected before any state mutation");
        Require(!DecodeTxInventory(vector<uint8_t>((maxTxInventoryItems+1)*33),parsed),"inventory item count bounded");
        Require(VerifyMessageSize(53,33)&&VerifyMessageSize(54,1024*33)&&!VerifyMessageSize(53,0)&&!VerifyMessageSize(54,1025*33),"wire length limits");

        auto root=filesystem::path(argc>1?argv[1]:"transaction-relay-data")/to_string(GetSteadyTime());filesystem::create_directories(root);
        auto wallet=GenerateWallet(),receiver=GenerateWallet(),evmWallet=GenerateWallet();
        SetGenesisUsers({{"base_fee",1},{"accounts",nlohmann::json::array({
            {{"public_key",U32ToHex(wallet.public_key)},{"balance",1000000}},
            {{"public_key",U32ToHex(evmWallet.public_key)},{"balance",1000000}}})}});
        InitDB(root.string());GenerateGenesisBlock();
        boost::asio::io_context io;Connection first(io),second(io),third(io),legacy(io);
        legacy.peer->txInventory=false;
        auto tx=GenerateTx(wallet.public_key,receiver.public_key,10,1,wallet);nativeKey=TransactionInventoryKey(tx);
        auto announcement=EncodeTxInventory(vector<TxInventoryKey>{nativeKey});
        Require(ReceiveTxInventory(first.peer,announcement),"native inventory accepted");
        Require(txPending.count(nativeKey)&&txPending.at(nativeKey).attempts==1,"missing body requested immediately");
        auto frames=ReadMessages(io,first);
        Require(frames.size()==1&&frames[0].first==54&&frames[0].second==announcement,"request contains original kind and full hash");
        ReceiveTxInventory(second.peer,announcement);ReceiveTxInventory(third.peer,announcement);
        Require(txPending.at(nativeKey).sources.size()==3&&txPending.at(nativeKey).attempts==1,"announcers retained without concurrent duplicate requests");
        txPending.at(nativeKey).lastRequest=0;TxInventoryMaintenance();
        Require(txPending.at(nativeKey).lastSource==second.peer->key,"retry uses second source");
        frames=ReadMessages(io,second);Require(frames.size()==1&&frames[0].first==54,"alternate receives missing-body request");
        txPending.at(nativeKey).lastRequest=0;TxInventoryMaintenance();
        Require(txPending.at(nativeKey).lastSource==third.peer->key,"retry reaches third source instead of repeating first two");
        ReadMessages(io,third);
        auto invalid=tx;invalid[112]^=1;vector<uint8_t> statuses;
        {
            auto wrongHash=tx;wrongHash[0]^=1;
            auto wrong=ReserveTxBodies(first.peer,10,wrongHash);
            Require(wrong->receipts.empty()&&!txReceiving.count(nativeKey),"claimed hash with different content cannot pause requests");
            Require(!ReserveTxBodies(first.peer,10,span<const uint8_t>(tx).first(175)),"incomplete native body has no receipt");
            auto bad=ReserveTxBodies(first.peer,10,invalid);
            Require(bad->receipts.size()==1&&txReceivingBytes==176&&!ReadTxRelayBody(nativeKey),"received bad signature remains unverified");
            auto created=txReceiving.at(nativeKey).created;
            auto duplicate=ReserveTxBodies(second.peer,10,invalid);
            Require(duplicate->receipts.empty()&&txReceiving.at(nativeKey).created==created,"duplicate receipt cannot renew deadline or own another lease");
            TxRequestBatches requests;
            {lock_guard lock(txInventoryMutex);txPending.at(nativeKey).lastRequest=0;SelectTxRequestLocked(nativeKey,txPending.at(nativeKey),GetSteadyTime(),requests);}
            Require(requests.empty(),"complete body waiting for verification pauses retries");
            auto good=ReserveTxBodies(second.peer,44,tx);
            Require(good->receipts.size()==1&&txReceiving.at(nativeKey).bodies.size()==2,"different signature bytes are tracked separately and still verified");
            vector<shared_ptr<TxBodyReservation>> variants;
            for (size_t i=0;i<3;++i) {auto changed=invalid;changed[113+i]^=1;variants.push_back(ReserveTxBodies(first.peer,10,changed));}
            Require(txReceiving.at(nativeKey).bodies.size()==maxTxBodyVariants&&variants.back()->receipts.empty(),"signature variants cannot exceed the per-hash record limit");
            variants.clear();
            bad.reset();
            {lock_guard lock(txInventoryMutex);SelectTxRequestLocked(nativeKey,txPending.at(nativeKey),GetSteadyTime(),requests);}
            Require(requests.count(second.peer->key)&&!TxBodyWaitingLocked(nativeKey,GetSteadyTime()),"failed owner immediately permits another source");
            Require(txPending.at(nativeKey).attempts==3,"selection does not consume a request attempt before queue admission");
            // These direct state checks discard the selected messages.
            txPending.at(nativeKey).requestToken=0;requests.clear();
            good.reset();
            {lock_guard lock(txInventoryMutex);EraseTxReceivingLocked(nativeKey);}
            auto old=ReserveTxBodies(first.peer,10,tx);
            {lock_guard lock(txInventoryMutex);txReceiving.at(nativeKey).created=GetSteadyTime()-txBodyWaitNs;}
            requests.clear();
            {lock_guard lock(txInventoryMutex);txPending.at(nativeKey).lastRequest=0;SelectTxRequestLocked(nativeKey,txPending.at(nativeKey),GetSteadyTime(),requests);}
            Require(requests.empty(),"a real queued owner still suppresses requests after two seconds");
            {lock_guard lock(txInventoryMutex);txReceiving.at(nativeKey).created=GetSteadyTime()-txRequestLifetimeNs;}
            TxInventoryMaintenance();
            Require(txReceiving.count(nativeKey)&&txPending.count(nativeKey),"queued owner and its sources survive the ordinary request lifetime");
            {lock_guard lock(txInventoryMutex);txReceiving.at(nativeKey).created=GetSteadyTime()-txBodyOwnerWaitNs;
                SelectTxRequestLocked(nativeKey,txPending.at(nativeKey),GetSteadyTime(),requests);}
            Require(!requests.empty(),"absolute owner deadline still permits recovery from a stuck task");
            txPending.at(nativeKey).requestToken=0;txPending.at(nativeKey).attempts=6;requests.clear();
            TxInventoryMaintenance();
            Require(txReceivingBytes==176&&txReceiving.at(nativeKey).retryReleased,"expired owner remains charged without pausing new requests");
            auto afterDeadline=ReserveTxBodies(second.peer,10,tx);
            Require(afterDeadline->receipts.empty()&&txReceivingBytes==176,"same live owner cannot establish a second waiting period");
            // Completion by another path can remove this generation while the
            // old task still holds its reservation; test that release boundary.
            {lock_guard lock(txInventoryMutex);EraseTxReceivingLocked(nativeKey);}
            auto fresh=ReserveTxBodies(second.peer,10,tx);auto freshToken=txReceiving.at(nativeKey).token;
            Require(txReceivingBytes==352,"new generation and old queued task share the same byte quota");
            old.reset();
            Require(txReceiving.at(nativeKey).token==freshToken&&txReceivingBytes==176,"old lease cannot release a new generation");
            fresh.reset();
            {lock_guard lock(txInventoryMutex);EraseTxReceivingLocked(nativeKey);}
            vector<shared_ptr<TxBodyReservation>> leases(8);vector<thread> concurrent;
            for (size_t i=0;i<leases.size();++i) concurrent.emplace_back([&,i]{leases[i]=ReserveTxBodies(first.peer,10,tx);});
            for (auto& worker:concurrent) worker.join();
            size_t owners=0;for (const auto& lease:leases) owners+=lease->receipts.size();
            Require(owners==1&&txReceivingBytes==176,"concurrent duplicate receipts have one bounded owner");
            leases.clear();Require(txReceivingBytes==0,"concurrent lease destruction releases bytes once");
            {lock_guard lock(txInventoryMutex);EraseTxReceivingLocked(nativeKey);}
            // Existing retry tests already used three attempts. Restore the
            // normal request budget after these direct state-machine checks.
            txPending.at(nativeKey).attempts=3;
        }
        Require(ProcessTxPackage(invalid,&statuses)==0&&statuses[0]==TxInvalid,"invalid signature rejected by normal transaction processing");
        RecordVerifiedNativeTransactions(first.peer,invalid,statuses);
        Require(txPending.count(nativeKey)&&!ReadTxRelayBody(nativeKey),"invalid bytes with claimed hash do not satisfy pending request or poison cache");
        Require(ProcessTxPackage(tx,&statuses)==1&&statuses[0]==TxAccepted,"valid requested transaction accepted");
        RecordVerifiedNativeTransactions(third.peer,tx,statuses);
        Require(!txPending.count(nativeKey)&&ReadTxRelayBody(nativeKey)&&TxPeerKnowsTransaction(third.peer,nativeKey.hash),"validated receipt clears pending and records exact body");
        Require(ProcessTxPackage(tx,&statuses)==0&&statuses[0]==TxKnown,"duplicate still recognized");
        RecordVerifiedNativeTransactions(second.peer,tx,statuses);
        Require(TxPeerKnowsTransaction(second.peer,nativeKey.hash),"verified duplicate updates possession without rebroadcast");

        auto evm=MakeEvmBody(evmWallet);evmKey=TransactionInventoryKey(evm,true);
        auto evmAnnouncement=EncodeTxInventory(vector<TxInventoryKey>{evmKey});
        ReceiveTxInventory(first.peer,evmAnnouncement);ReadMessages(io,first);
        {
            vector<uint8_t> batch(4);WriteU32(batch.data(),uint32_t(evm.size()));batch.insert(batch.end(),evm.begin(),evm.end());batch.push_back(1);
            Require(!ReserveTxBodies(first.peer,18,batch)&&!txReceiving.count(evmKey),"malformed EVM batch is rejected before any body registration");
            auto altered=evm;altered.back()^=1;
            auto mismatch=ReserveTxBodies(first.peer,17,altered);
            Require(mismatch->receipts.empty(),"complete EVM input is bound to the claimed hash before waiting");
            auto lease=ReserveTxBodies(first.peer,17,evm);
            Require(lease->receipts.size()==1,"complete EVM body participates in retry suppression");
            lease.reset();
        }
        auto invalidEvm=evm;invalidEvm.back()^=1;
        Require(!ProcessEvmTx(invalidEvm)&&!ObserveKnownEvmTransaction(first.peer,invalidEvm),"EVM input tampering fails signature/hash check");
        Require(txPending.count(evmKey)&&!ReadTxRelayBody(evmKey),"invalid EVM response retains request");
        Require(ProcessEvmTx(evm),"valid EVM response accepted");RecordVerifiedEvmTransaction(first.peer,evm);
        Require(!txPending.count(evmKey)&&ObserveKnownEvmTransaction(second.peer,evm),"exact EVM duplicate satisfies receipt hook");
        invalidEvm=evm;invalidEvm[112]^=1;
        Require(!ObserveKnownEvmTransaction(second.peer,invalidEvm),"same EVM hash with a different signature does not count as verified receipt");
        auto wrongKind=evmKey;wrongKind.kind=0;
        Require(!ReadTxRelayBody(wrongKind),"native request cannot strip input from EVM transaction");
        {
            lock_guard lock(txpoolMutex);txpool.clear();evmPool.clear();evmPoolBytes=0;txUserPool.clear();readyPoolTx=0;
        }
        Require(ServeTxRequests(third.peer,EncodeTxInventory(vector<TxInventoryKey>{nativeKey,evmKey,wrongKind})),"serve native and EVM from recent cache after pool removal");
        frames=ReadMessages(io,third,2);
        Require(frames.size()==2&&frames[0].first==10&&frames[0].second==vector<uint8_t>(tx.begin(),tx.end()),"native response reuses type10 exact body");
        Require(frames[1].first==18&&ReadU32(frames[1].second.data())==evm.size()&&equal(evm.begin(),evm.end(),frames[1].second.begin()+4),"EVM response retains length and complete input");

        {
            Connection congested(io);
            auto waiting=GenerateTx(wallet.public_key,receiver.public_key,1,50,wallet);
            auto waitingKey=TransactionInventoryKey(waiting);
            auto notice=EncodeTxInventory(vector<TxInventoryKey>{waitingKey});
            auto occupied=sendQueueBudget.TryReserve(congested.peer->sendBudget,maxSendBytes,maxSendBytes,false);
            Require(bool(occupied),"test fills only the target peer's send allowance");
            auto attemptsBefore=txInventoryRetries.load(),rejectedBefore=txInventoryRequestQueueRejected.load();
            for (unsigned i=0;i<8;++i) Require(ReceiveTxInventory(congested.peer,notice),"inventory accepted while send queue is full");
            const auto& pending=txPending.at(waitingKey);
            Require(pending.attempts==0&&pending.lastRequest==0&&pending.requestToken==0,
                "rejected queue admission consumes neither attempts nor retry time and releases selection");
            Require(txInventoryRetries==attemptsBefore&&txInventoryRequestQueueRejected-rejectedBefore==8,
                "unsent requests are counted separately from actual retries");
            occupied.reset();TxInventoryMaintenance();
            Require(txPending.at(waitingKey).attempts==1,"request can be sent immediately after capacity returns");
            auto request=ReadMessages(io,congested);
            Require(request.size()==1&&request[0].first==54&&request[0].second==notice,"recovered capacity sends the missing request");
            {lock_guard lock(txInventoryMutex);txPending.erase(waitingKey);}

            occupied=sendQueueBudget.TryReserve(congested.peer->sendBudget,maxSendBytes,maxSendBytes,false);
            Require(bool(occupied),"reply failure test fills peer send allowance");
            const uint64_t tokens=1000000;
            {lock_guard lock(txInventoryMutex);auto& state=TxPeerStateLocked(congested.peer,GetSteadyTime());
                state.replyTokens=tokens;state.replyAt=GetSteadyTime()+1000000000ULL;}
            auto refunded=txInventoryReplyRefundBytes.load();
            auto requested=EncodeTxInventory(vector<TxInventoryKey>{nativeKey,evmKey});
            Require(ServeTxRequests(congested.peer,requested),"rejected reply keeps connection available");
            Require(txPeerKnown.at(congested.peer->key).replyTokens==tokens&&
                txInventoryReplyRefundBytes-refunded==tx.size()+4+evm.size(),"both unsent native and EVM replies refund their byte quota");
            occupied.reset();Require(ServeTxRequests(congested.peer,requested),"same reply can retry after queue capacity returns");
            Require(txPeerKnown.at(congested.peer->key).replyTokens==tokens-tx.size()-4-evm.size(),"successful replies consume byte quota once");
            Require(ReadMessages(io,congested,2).size()==2,"recovered replies contain both requested transaction kinds");
            ClosePeer(congested.peer);
        }

        {
            Connection slow(io),alternate(io);
            auto waiting=GenerateTx(wallet.public_key,receiver.public_key,1,80,wallet);
            auto waitingKey=TransactionInventoryKey(waiting);
            auto notice=EncodeTxInventory(vector<TxInventoryKey>{waitingKey});
            Require(ReceiveTxInventory(slow.peer,notice),"owner timeout test requests its first source");
            Require(ReadMessages(io,slow).size()==1,"first source receives initial body request");
            Require(ReceiveTxInventory(alternate.peer,notice),"owner timeout test retains alternate source");
            auto owner=ReserveTxBodies(slow.peer,10,waiting);
            Require(owner->receipts.size()==1,"timeout test holds an actual body reservation");
            uint64_t started;
            {lock_guard lock(txInventoryMutex);started=GetSteadyTime()-txBodyOwnerWaitNs;
                txReceiving.at(waitingKey).created=started;
                auto& pending=txPending.at(waitingKey);pending.created=pending.started=started;}
            TxInventoryMaintenance();
            Require(txPending.count(waitingKey)&&txPending.at(waitingKey).attempts==2&&
                txPending.at(waitingKey).lastSource==alternate.peer->key,"maintenance resumes at another source without erasing or resetting attempts");
            auto retried=ReadMessages(io,alternate);
            Require(retried.size()==1&&retried[0].first==54&&retried[0].second==notice,"owner expiry sends a real missing-body request");
            const auto resumed=txPending.at(waitingKey).created;
            auto duplicate=ReserveTxBodies(alternate.peer,10,waiting);
            auto invalid=waiting;invalid[112]^=1;
            auto variant=ReserveTxBodies(slow.peer,10,invalid);variant.reset();
            Require(duplicate->receipts.empty()&&txReceiving.at(waitingKey).created==started&&
                txReceiving.at(waitingKey).retryReleased&&txPending.at(waitingKey).created==resumed&&
                txPending.at(waitingKey).started==started&&txPending.at(waitingKey).attempts==2,
                "duplicate and failed signature variant cannot renew deadlines or reset attempts");
            {lock_guard lock(txInventoryMutex);auto& pending=txPending.at(waitingKey);
                pending.started=GetSteadyTime()-txBodyOwnerWaitNs-txRequestLifetimeNs;
                pending.created=GetSteadyTime();}
            TxInventoryMaintenance();
            Require(!txPending.count(waitingKey),"fixed total request lifetime expires even when a reply recently resumed requests");
            owner.reset();TxInventoryMaintenance();
            Require(!txReceiving.count(waitingKey),"completed expired owner releases its final receipt record");
            ClosePeer(slow.peer);ClosePeer(alternate.peer);
        }

        auto next=GenerateTx(wallet.public_key,receiver.public_key,10,2,wallet);auto nextKey=TransactionInventoryKey(next);
        Require(ProcessTxPackage(next,&statuses)==1,"new native transaction validated before broadcast");
        // Add two broadcast targets. A zero push count still serves old peers
        // in full, while an upgraded peer receives an announcement only.
        {lock_guard lock(peerMutex);peerPool.emplace(second.peer->key,second.peer);peerPool.emplace(legacy.peer->key,legacy.peer);}
        txPushPeers=0;
        RelayAcceptedTransactions(first.peer,next);
        frames=ReadMessages(io,second);auto legacyFrames=ReadMessages(io,legacy);
        Require(frames.size()==1&&frames[0].first==53&&DecodeTxInventory(frames[0].second,parsed)&&parsed==vector<TxInventoryKey>{nextKey},"upgraded peer receives hash when full-body push disabled");
        Require(!TxPeerKnowsTransaction(second.peer,nextKey.hash),"outgoing hash notice does not imply possession for compact prefill");
        Require(legacyFrames.size()==1&&legacyFrames[0].first==10&&legacyFrames[0].second==vector<uint8_t>(next.begin(),next.end()),"legacy peer still receives complete body");
        RelayAcceptedTransactions(first.peer,next);
        Require(ReadMessages(io,second,0).empty()&&ReadMessages(io,legacy,0).empty(),"known-body and notification caches suppress redundant broadcasts");
        Require(ServeTxRequests(second.peer,EncodeTxInventory(vector<TxInventoryKey>{nextKey})),"notified peer can request body");
        frames=ReadMessages(io,second);Require(frames.size()==1&&frames[0].first==10&&TxPeerKnowsTransaction(second.peer,nextKey.hash),"body response establishes possession separately from notification");

        {
            Connection unknown(io);
            vector<uint8_t> batch;vector<TxInventoryKey> batchKeys;
            for (uint64_t nonce=60;nonce<76;++nonce) {
                auto body=GenerateTx(wallet.public_key,receiver.public_key,1,nonce,wallet);
                batch.insert(batch.end(),body.begin(),body.end());batchKeys.push_back(TransactionInventoryKey(body));
            }
            Require(ProcessTxPackage(batch,&statuses)==batchKeys.size(),"push selection test uses verified accepted transactions");
            RecordVerifiedNativeTransactions(first.peer,batch,statuses);
            legacy.peer->txInventory=true;txPushPeers=1;
            {lock_guard lock(peerMutex);peerPool.emplace(third.peer->key,third.peer);peerPool.emplace(unknown.peer->key,unknown.peer);}
            {lock_guard lock(txInventoryMutex);for (auto target:{second.peer,third.peer,legacy.peer})
                for (const auto& key:batchKeys) TxRememberPeerLocked(target,key,GetSteadyTime());}
            RelayAcceptedTransactions(first.peer,batch,false,false);
            auto pushed=ReadMessages(io,unknown);
            Require(pushed.size()==1&&pushed[0].first==10&&pushed[0].second==batch,
                "known peers never consume the only full-body destination for any transaction");
            Require(ReadMessages(io,second,0).empty()&&ReadMessages(io,third,0).empty()&&ReadMessages(io,legacy,0).empty(),
                "already-known destinations receive neither duplicate body nor inventory");
            {lock_guard lock(peerMutex);peerPool.erase(third.peer->key);peerPool.erase(unknown.peer->key);}
            legacy.peer->txInventory=false;txPushPeers=0;ClosePeer(unknown.peer);
        }

        // Bound hostile announcements and sources without sending thousands of
        // queued requests: exercise the same protected state helpers directly.
        {
            lock_guard lock(txInventoryMutex);
            txPending.clear();
            for (size_t i=0;i<maxTxKnownPerPeer+5;++i) {TxInventoryKey key;WriteU64(key.hash.data(),i+10000);TxRememberPeerLocked(first.peer,key,GetSteadyTime());}
            Require(txPeerKnown.at(first.peer->key).hashes.size()==maxTxKnownPerPeer&&txKnownItems<=maxTxKnownItems,"per-peer known cache remains bounded");
            TxInventoryKey oldest;WriteU64(oldest.hash.data(),10000);Require(!TxPeerKnowsLocked(first.peer,oldest),"known cache evicts oldest item");
            for (size_t i=0;i<maxTxPending;++i) {TxInventoryKey key;WriteU64(key.hash.data(),i+100000);txPending[key].created=GetSteadyTime();}
        }
        TxInventoryKey overflow;overflow.hash[31]=99;
        ReceiveTxInventory(first.peer,EncodeTxInventory(vector<TxInventoryKey>{overflow}));
        Require(txPending.size()==maxTxPending&&!txPending.count(overflow),"unknown transaction flood respects global pending limit");
        for (auto& [key,pending]:txPending) pending.created=GetSteadyTime()-txRequestLifetimeNs;
        TxInventoryMaintenance();Require(txPending.empty(),"expired missing requests are released");
        PendingTransaction pending;for (auto peer:{first.peer,second.peer,third.peer,legacy.peer}) TxRememberSourceLocked(pending,peer);
        auto extra=make_shared<Peer>(tcp::socket(io));extra->key=NewPeerKey();TxRememberSourceLocked(pending,extra);
        Require(pending.sources.size()==maxTxSources,"source list bounded");first.peer->closePosted=true;TxRememberSourceLocked(pending,extra);
        Require(pending.sources.front().lock()==extra,"dead source slot reused");
        auto now=GetSteadyTime();
        {lock_guard lock(txInventoryMutex);auto& state=TxPeerStateLocked(third.peer,now);state.replyTokens=0;state.replyAt=now;}
        Require(!ReserveTxReplyBytes(third.peer,176,now)&&ReserveTxReplyBytes(third.peer,176,now+1000000000ULL),"response traffic quota refills with monotonic time");
        {
            auto body=GenerateTx(wallet.public_key,receiver.public_key,1,3,wallet);auto key=TransactionInventoryKey(body);
            {lock_guard lock(txInventoryMutex);for (size_t i=0;i<maxTxBodyRecords;++i) {TxInventoryKey item;WriteU64(item.hash.data(),i+200000);txReceiving[item].created=GetSteadyTime();}}
            auto capped=ReserveTxBodies(second.peer,10,body);
            Require(capped->receipts.empty()&&txReceiving.size()==maxTxBodyRecords,"receipt count limit does not grow under a flood");
            {lock_guard lock(txInventoryMutex);txReceiving.clear();TxInventoryKey item;txReceiving[item].bodies[{}]=maxTxBodyBytes;txReceivingBytes=maxTxBodyBytes;}
            capped=ReserveTxBodies(second.peer,10,body);
            Require(capped->receipts.empty()&&txReceivingBytes==maxTxBodyBytes,"receipt byte quota does not grow beyond its limit");
            {lock_guard lock(txInventoryMutex);txReceiving.clear();txReceivingBytes=0;}
            Connection cancelled(io);
            auto previousLimit=maxPendingVerify;maxPendingVerify=1;pendingTxVerify=1;
            co_spawn(cancelled.peer->strand,ProcessMessage(cancelled.peer,10,vector<uint8_t>(body.begin(),body.end())),detached);
            io.restart();io.poll();
            Require(txReceiving.count(key)&&TxBodyWaitingLocked(key,GetSteadyTime()),"body registered while verification admission is full");
            cancelled.peer->closed=true;
            auto cancelledAt=steady_clock::now();
            while (txReceivingBytes&&steady_clock::now()-cancelledAt<seconds(2)) {io.restart();io.run_for(milliseconds(20));}
            Require(txReceivingBytes==0&&!TxBodyWaitingLocked(key,GetSteadyTime()),"cancelled admission releases the receipt without a worker");
            pendingTxVerify=0;maxPendingVerify=previousLimit;
            cancelled.peer->socket.close();
            Connection admission(io);auto broken=body;broken[112]^=1;
            verifyThreads=make_unique<thread_pool>(2);
            for (const auto& bytes:{vector<uint8_t>(broken.begin(),broken.end()),vector<uint8_t>(body.begin(),body.end()),vector<uint8_t>(body.begin(),body.end())})
                co_spawn(admission.peer->strand,ProcessMessage(admission.peer,44,bytes),detached);
            io.restart();io.poll();verifyThreads->join();verifyThreads.reset();
            auto replies=ReadMessages(io,admission,3);size_t accepted=0,known=0,rejected=0;
            for (const auto& reply:replies) {
                Require(reply.first==45&&reply.second.size()==1,"each type44 batch retains its admission response");
                accepted+=reply.second[0]==TxAccepted;known+=reply.second[0]==TxKnown;rejected+=reply.second[0]==TxInvalid;
            }
            Require(accepted==1&&known==1&&rejected==1,"bad signature and concurrent good duplicates follow normal validation and ACK rules");
            Require(txReceivingBytes==0,"successful validation and failed worker release all body bytes");
            admission.peer->socket.close();
        }
        TestMergedWork(io,wallet,receiver,evmWallet);
        for (auto peer:{first.peer,second.peer,third.peer,legacy.peer}) {peer->closePosted=true;peer->socket.close();}
        peerPool.clear();db.reset();
        cout<<"Transaction inventory codec, retries, malformed signatures, EVM bodies, legacy relay, cache and request limits passed\n";
        return 0;
    } catch (const exception& error) {db.reset();cerr<<error.what()<<'\n';return 1;}
}
