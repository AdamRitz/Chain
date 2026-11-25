#include <filesystem>
#include <iostream>
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
vector<uint8_t> MakeEvmBody(const Wallet& wallet) {
    vector<uint8_t> payload(46,0);memcpy(payload.data(),"EVM1",4);WriteU64(payload.data()+5,100000);
    memcpy(payload.data()+13,genesisRoot.data(),32);payload.back()=0;
    array<uint8_t,176> tx{};memcpy(tx.data(),wallet.public_key.data(),32);WriteU64(tx.data()+72,1);
    auto hash=EvmTransactionHash(tx,payload);memcpy(tx.data()+80,hash.data(),32);
    crypto_sign_detached(tx.data()+112,nullptr,hash.data(),32,wallet.private_key.data());
    vector<uint8_t> body(tx.begin(),tx.end());body.insert(body.end(),payload.begin(),payload.end());return body;
}
int main(int argc,char** argv) {
    try {
        if (sodium_init()<0) return 1;
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
        for (auto peer:{first.peer,second.peer,third.peer,legacy.peer}) {peer->closePosted=true;peer->socket.close();}
        peerPool.clear();db.reset();
        cout<<"Transaction inventory codec, retries, malformed signatures, EVM bodies, legacy relay, cache and request limits passed\n";
        return 0;
    } catch (const exception& error) {db.reset();cerr<<error.what()<<'\n';return 1;}
}
