#include <filesystem>
#include <iostream>
#include "../Network/Server.h"
#include "../EVM/BuildTransaction.h"
using namespace std;
void Require(bool condition,const char* message) { if (!condition) throw runtime_error(message); }
unordered_set<array<uint8_t,32>,GetMapHash> prefillTestKnown;
bool PrefillTestPeerKnows(const shared_ptr<Peer>&,const array<uint8_t,32>& hash) { return prefillTestKnown.count(hash)!=0; }
vector<chain::compact::PrefilledTransaction> PrefillEntries(const vector<uint8_t>& block,initializer_list<uint32_t> positions) {
    EvmPayloads payloads; Require(ReadEvmPayloads(block,payloads),"source prefill payload parsed");
    vector<chain::compact::PrefilledTransaction> entries;
    for (const auto index:positions) {
        vector<uint8_t> tx(block.begin()+112+size_t(index)*176,block.begin()+112+size_t(index+1)*176);
        if (auto input=payloads.find(index);input!=payloads.end()) tx.insert(tx.end(),input->second.begin(),input->second.end());
        entries.push_back({index,std::move(tx)});
    }
    return entries;
}

vector<uint8_t> RepairResponse(const array<uint8_t,32>& hash,const vector<uint8_t>& block,initializer_list<uint32_t> positions) {
    vector<uint8_t> result(hash.begin(),hash.end());
    for (auto position:positions) {
        size_t offset=result.size(); result.resize(offset+8+176);
        WriteU32(result.data()+offset,position); WriteU32(result.data()+offset+4,176);
        memcpy(result.data()+offset+8,block.data()+112+size_t(position)*176,176);
    }
    return result;
}
// Run the real network verification worker independently of the peer IO loop.
// The peer loop retains reconstructed type2 work, so the batch can finish first.
void RunVerificationMessage(const shared_ptr<Peer>& peer,uint8_t type,vector<uint8_t> data) {
    boost::asio::io_context verificationIO;
    candidateThreads=make_unique<boost::asio::thread_pool>(1);
    exception_ptr failure;
    co_spawn(verificationIO,ProcessMessage(peer,type,std::move(data)),[&](exception_ptr error) { failure=error; });
    verificationIO.run();
    candidateThreads->join(); candidateThreads.reset();
    if (failure) rethrow_exception(failure);
    Require(!nodeFailed,"network verification worker did not fail");
}
int main(int argc,char** argv) {
    try {
        if (sodium_init()<0) return 1;
        chain::compact::Key key{}; chain::compact::Hash hashVector{};
        for (size_t i=0;i<key.size();++i) key[i]=uint8_t(i);
        for (size_t i=0;i<hashVector.size();++i) hashVector[i]=uint8_t(i);
        Require(chain::compact::ShortId(key,hashVector)==0x512f72f27cceULL,"SipHash-2-4 reference vector and little-endian truncation");

        vector<uint8_t> header(112,0); WriteU64(header.data()+72,9);
        vector<array<uint8_t,32>> hashes(9); vector<uint8_t> contracts(9,0);
        for (size_t i=0;i<hashes.size();++i) hashes[i][0]=uint8_t(i);
        contracts[0]=contracts[8]=1;
        auto encoded=chain::compact::Encode(header,hashes,contracts,12345);
        chain::compact::Decoded decoded;
        Require(encoded.size()==120+2+9*6&&chain::compact::Decode(encoded,10000,2408,decoded),"short compact size and parse");
        Require(decoded.contracts==contracts&&decoded.nonce==12345&&decoded.transactionEnd==encoded.size(),"bitmap crosses byte boundary");
        for (size_t i=0;i<hashes.size();++i) Require(decoded.ids[i]==chain::compact::ShortId(decoded.key,hashes[i]),"IDs preserve transaction order");
        for (size_t length=0;length<encoded.size();++length)
            Require(!chain::compact::Decode(span<const uint8_t>(encoded).first(length),10000,2408,decoded),"truncated compact rejected");
        auto malformed=encoded; malformed[121]|=0x80;
        Require(!chain::compact::Decode(malformed,10000,2408,decoded),"unused bitmap bits rejected");
        malformed=encoded; WriteU64(malformed.data()+72,10001);
        Require(!chain::compact::Decode(malformed,10000,2408,decoded),"transaction count bounded before allocation");
        malformed=encoded; malformed.resize(malformed.size()+2409);
        Require(!chain::compact::Decode(malformed,10000,2408,decoded),"extension length bounded");
        Require(chain::compact::DeriveKey(header,12345)!=chain::compact::DeriveKey(header,12346),"nonce changes ID key");
        header[0]^=1;
        Require(chain::compact::DeriveKey(header,12345)!=chain::compact::DeriveKey(span<const uint8_t>(encoded).first(112),12345),"block header changes ID key");
        vector<uint64_t> repeated{7,7,9}; chain::compact::ShortIdIndex collisions(repeated);
        Require(collisions.IsAmbiguous(0)&&collisions.IsAmbiguous(1),"repeated advertised IDs both require repair");
        collisions.Observe(2,hashes[0]); collisions.Observe(2,hashes[0]);
        Require(!collisions.IsAmbiguous(2)&&collisions.MatchedHash(2).has_value(),"same transaction in pool and recent cache is unique");
        collisions.Observe(2,hashes[1]);
        Require(collisions.IsAmbiguous(2)&&!collisions.MatchedHash(2),"different local hashes with one ID require repair");
        Require(VerifyMessageSize(51,120)&&!VerifyMessageSize(51,119)&&
            !VerifyMessageSize(51,120+(maxProtocolTx+7)/8+maxProtocolTx*6+2409),"new frame bounds");
        Require(VerifyMessageSize(39,112+6000*33),"legacy frame remains supported");

        auto root=filesystem::path(argc>1?argv[1]:"compact-relay-data")/to_string(GetSteadyTime());
        filesystem::create_directories(root);
        auto wallet=GenerateWallet(),receiver=GenerateWallet();
        SetGenesisUsers({{"base_fee",1},{"accounts",nlohmann::json::array({{{"public_key",U32ToHex(wallet.public_key)},{"balance",10000}}})}});
        InitDB(root.string()); GenerateGenesisBlock();
        Block block; block.height=2; block.previousHash=DBReadCurrentBlock(); hashes.clear(); contracts.assign(3,0);
        for (uint64_t nonce=1;nonce<=3;++nonce) {
            block.txs.push_back(GenerateTx(wallet.public_key,receiver.public_key,1,nonce,wallet));
            hashes.push_back(GetTransactionHash(block.txs.back()));
        }
        block.merkleRoot=MerkleCompute(hashes); auto full=SerializeBlockALL(block);
        Require(VerifyBlock(full),"full source block valid");
        auto compact=chain::compact::Encode(span<const uint8_t>(full).first(112),hashes,contracts,42);
        auto blockHash=block.hash;
        boost::asio::io_context io;
        auto first=make_shared<Peer>(tcp::socket(io)),second=make_shared<Peer>(tcp::socket(io)),unknown=make_shared<Peer>(tcp::socket(io));
        for (auto peer:{first,second,unknown}) { peer->key=NewPeerKey(); peer->port=uint16_t(5000+peer->key); peer->compact=true; }
        {
            lock_guard lock(peerMutex);
            Require(peerPool.empty()&&addressAttempts.empty(),"peer count fixture starts empty");
            struct RestorePeerCount {
                shared_ptr<Peer> first,second,unknown; uint16_t secondPort;
                ~RestorePeerCount() {
                    peerPool.clear(); addressAttempts.clear(); second->port=secondPort;
                    first->closePosted=false; second->closePosted=false; unknown->closePosted=false;
                }
            } restore{first,second,unknown,second->port};
            second->port=first->port;
            for (const auto& peer:{first,second,unknown}) peerPool.emplace(peer->key,peer);
            Require(ConnectedPeerCountLocked()==2,"two connections to one endpoint count as one neighbor");
            unknown->closePosted=true;
            Require(ConnectedPeerCountLocked()==1,"closed endpoint is excluded from neighbor count");
            first->closePosted=true;
            Require(ConnectedPeerCountLocked()==1,"remaining open duplicate still counts as one neighbor");
            second->closePosted=true;
            Require(ConnectedPeerCountLocked()==0,"all closed connections count as no neighbors");
            first->closePosted=false; second->closePosted=false; unknown->closePosted=false;
            addressAttempts.emplace(first->address.to_string(),first->port);
            addressAttempts.emplace(unknown->address.to_string(),unknown->port);
            Require(ConnectedPeerCountLocked(true)==2,"connection attempts overlapping connected endpoints are deduplicated");
            addressAttempts.emplace("127.0.0.2",first->port);
            Require(ConnectedPeerCountLocked(true)==3&&ConnectedPeerCountLocked()==2,
                "independent endpoint attempt adds one only when attempts are requested");
        }
        Require(peerPool.empty()&&addressAttempts.empty()&&first->port!=second->port,
            "peer count fixture restores connection and attempt state");
        auto allEntries=PrefillEntries(full,{0,1,2});
        auto prefilled=chain::compact::EncodePrefilled(compact,allEntries);
        chain::compact::PrefilledDecoded prefilledDecoded;
        Require(chain::compact::DecodePrefilled(prefilled,maxProtocolTx,2408,221+maxEvmInput,prefilledDecoded)&&
            prefilledDecoded.transactions.size()==3,"prefilled native frame decodes in order");
        for (size_t length=0;length<prefilled.size();++length)
            Require(!chain::compact::DecodePrefilled(span<const uint8_t>(prefilled).first(length),maxProtocolTx,2408,
                221+maxEvmInput,prefilledDecoded),"truncated prefilled frame rejected");
        compactPrefill=false;
        ReceivePrefilledCompactBlock(first,prefilled);
        Require(!compactBlocks.count(blockHash),"locally disabled prefill is not accepted");
        compactPrefill=true; shortIds=false;
        ReceivePrefilledCompactBlock(first,prefilled);
        Require(!compactBlocks.count(blockHash),"locally disabled short IDs also disable prefill");
        shortIds=true;
        const auto firstPort=first->port; first->port=0;
        ReceivePrefilledCompactBlock(first,prefilled);
        Require(!compactBlocks.count(blockHash),"query-only connection cannot provide a prefilled compact block");
        first->port=firstPort; first->closePosted=true;
        ReceivePrefilledCompactBlock(first,prefilled);
        Require(!compactBlocks.count(blockHash),"closed connection cannot provide a prefilled compact block");
        first->closePosted=false;
        Require(!first->shortCompact&&!first->prefillCompact,"remote capability reply has not arrived");
        ReceivePrefilledCompactBlock(first,prefilled);
        Require(compactBlocks.count(blockHash)&&compactBlocks.at(blockHash)->missing==0&&
            compactBlocks.at(blockHash)->awaitingVerification&&CompactRootMatches(*compactBlocks.at(blockHash)),
            "first prefilled block is accepted before the remote capability reply arrives");
        Require(!first->shortCompact&&!first->prefillCompact,"receiving prefill does not enable sending before remote negotiation");
        CompleteBlockReceive(blockHash);
        first->prefillCompact=true; second->prefillCompact=true;
        auto malformedPrefill=prefilled;
        auto recordOffset=8+compact.size();
        WriteU32(malformedPrefill.data()+recordOffset+184,0);
        Require(!chain::compact::DecodePrefilled(malformedPrefill,maxProtocolTx,2408,221+maxEvmInput,prefilledDecoded),
            "duplicate prefilled index rejected");
        malformedPrefill=prefilled;
        swap_ranges(malformedPrefill.begin()+recordOffset,malformedPrefill.begin()+recordOffset+184,
            malformedPrefill.begin()+recordOffset+184);
        Require(!chain::compact::DecodePrefilled(malformedPrefill,maxProtocolTx,2408,221+maxEvmInput,prefilledDecoded),
            "out-of-order prefilled records rejected");
        malformedPrefill=prefilled; WriteU32(malformedPrefill.data()+4+compact.size(),65);
        Require(!chain::compact::DecodePrefilled(malformedPrefill,maxProtocolTx,2408,221+maxEvmInput,prefilledDecoded),
            "prefilled count bounded before allocating entries");
        malformedPrefill=prefilled; WriteU32(malformedPrefill.data()+recordOffset,3);
        ReceivePrefilledCompactBlock(first,malformedPrefill);
        Require(compactBlocks.empty()&&compactBuilding.empty(),"out-of-range prefill does not reserve pending state");
        malformedPrefill=prefilled; malformedPrefill[recordOffset+8+80]^=1;
        ReceivePrefilledCompactBlock(first,malformedPrefill);
        Require(compactBlocks.empty(),"prefilled hash must match advertised short ID");
        malformedPrefill=prefilled; malformedPrefill[recordOffset+8]^=1;
        ReceivePrefilledCompactBlock(first,malformedPrefill);
        Require(compactBlocks.empty(),"prefilled native content hash is verified");
        malformedPrefill=prefilled; malformedPrefill[recordOffset+8+112]^=1;
        ReceivePrefilledCompactBlock(first,malformedPrefill);
        Require(compactBlocks.empty(),"prefilled native signature is verified");
        malformedPrefill=prefilled; malformedPrefill.push_back(0);
        ReceivePrefilledCompactBlock(first,malformedPrefill);
        Require(compactBlocks.empty(),"malformed prefill tail does not cache valid prefix");
        auto repeatedCompact=compact;
        copy_n(repeatedCompact.begin()+121,6,repeatedCompact.begin()+127);
        auto repeatedPrefill=chain::compact::EncodePrefilled(repeatedCompact,allEntries);
        Require(!chain::compact::DecodePrefilled(repeatedPrefill,maxProtocolTx,2408,221+maxEvmInput,prefilledDecoded),
            "prefilled index with repeated short ID rejected");
        auto partialPrefill=chain::compact::EncodePrefilled(compact,PrefillEntries(full,{0,2}));
        ReceivePrefilledCompactBlock(first,partialPrefill);
        Require(compactBlocks.at(blockHash)->missing==1&&compactBlocks.at(blockHash)->transactions[1].empty()&&
            compactBlocks.at(blockHash)->transactions[0]==allEntries[0].transaction,"prefill requests only missing positions");
        auto prefillPending=compactBlocks.at(blockHash); auto prefillBytes=compactBytes;
        ReceivePrefilledCompactBlock(second,malformedPrefill);
        Require(compactBlocks.at(blockHash)==prefillPending&&prefillPending->sources.size()==1&&compactBytes==prefillBytes,
            "malformed prefill leaves existing pending block and sources unchanged");
        CompleteBlockReceive(blockHash);
        ReceivePrefilledCompactBlock(first,prefilled);
        Require(compactBlocks.at(blockHash)->missing==0&&compactBlocks.at(blockHash)->awaitingVerification&&
            CompactRootMatches(*compactBlocks.at(blockHash)),"complete prefill reconstructs committed Merkle root");
        CompleteBlockReceive(blockHash);
        auto wrongRoot=full; wrongRoot[32]^=1; crypto_generichash(wrongRoot.data()+80,32,wrongRoot.data(),80,nullptr,0);
        auto wrongRootCompact=chain::compact::Encode(span<const uint8_t>(wrongRoot).first(112),hashes,contracts,42);
        ReceivePrefilledCompactBlock(first,chain::compact::EncodePrefilled(wrongRootCompact,allEntries));
        Require(compactBlocks.empty()&&relayBlocks.empty(),"complete prefill with wrong Merkle root never caches block");
        compactPeerKnowsTransaction=PrefillTestPeerKnows; prefillTestKnown.insert(hashes[2]);
        auto oldPrefillMax=compactPrefillMax,oldPrefillBytes=compactPrefillBytes;
        compactPrefillMax=1; compactPrefillBytes=176;
        Require(chain::compact::Decode(compact,maxProtocolTx,2408,decoded),"selection compact parsed");
        auto selected=SelectCompactPrefilled(first,full,EvmPayloads{},hashes,decoded);
        Require(selected.size()==1&&selected[0].index==1,"prefill prioritizes later unknown transaction and skips known hash");
        compactPrefillBytes=175;
        Require(SelectCompactPrefilled(first,full,EvmPayloads{},hashes,decoded).empty(),"prefill byte limit includes transaction bytes");
        compactPrefillMax=oldPrefillMax; compactPrefillBytes=oldPrefillBytes;
        compactPeerKnowsTransaction=nullptr; prefillTestKnown.clear();

        auto evmTx=MakeEvmTx(wallet,{},vector<uint8_t>{0},1,0,100000,true);
        Block evmBlock; evmBlock.height=2; evmBlock.previousHash=block.previousHash;
        array<uint8_t,176> evmEnvelope; memcpy(evmEnvelope.data(),evmTx.data(),176);
        evmBlock.txs.push_back(evmEnvelope); evmBlock.evm[0]={evmTx.begin()+176,evmTx.end()};
        vector<array<uint8_t,32>> evmHashes{GetTransactionHash(evmEnvelope)};
        evmBlock.merkleRoot=MerkleCompute(evmHashes); auto evmFull=SerializeBlockALL(evmBlock);
        auto evmCompact=chain::compact::Encode(span<const uint8_t>(evmFull).first(112),evmHashes,vector<uint8_t>{1},43);
        auto evmPrefill=chain::compact::EncodePrefilled(evmCompact,PrefillEntries(evmFull,{0}));
        auto badEvmPrefill=evmPrefill; badEvmPrefill.back()^=1;
        ReceivePrefilledCompactBlock(first,badEvmPrefill);
        Require(compactBlocks.empty(),"prefilled EVM payload hash is verified");
        ReceivePrefilledCompactBlock(first,evmPrefill);
        Require(compactBlocks.count(evmBlock.hash)&&compactBlocks.at(evmBlock.hash)->missing==0&&
            compactBlocks.at(evmBlock.hash)->encodedBytes==evmFull.size(),"prefilled EVM payload preserved and encoded size exact");
        CompleteBlockReceive(evmBlock.hash);

        { lock_guard lock(txpoolMutex); txpool.emplace(hashes[0],block.txs[0]); }
        ResetCompactHashSnapshot();
        auto snapshot=ReadCompactHashSnapshot(); auto reusedSnapshot=ReadCompactHashSnapshot();
        Require(snapshot==reusedSnapshot&&snapshot->hashes.size()==1&&snapshot->hashes[0]==hashes[0],
            "concurrent compact candidates reuse one transaction hash snapshot");
        { lock_guard lock(txpoolMutex); txpool.clear(); }
        ReceiveShortCompactBlock(first,compact);
        Require(compactBlocks.at(blockHash)->missing==3,"removed snapshot transaction is fetched by index");
        CompleteBlockReceive(blockHash); snapshot.reset(); reusedSnapshot.reset(); ResetCompactHashSnapshot();
        ReceiveShortCompactBlock(first,compact);
        Require(compactBlocks.count(blockHash)&&compactBlocks.at(blockHash)->missing==3,"unknown transactions requested by index");
        auto pending=compactBlocks.at(blockHash);
        ReceiveShortCompactBlock(second,compact);
        Require(pending->sources.size()==2,"duplicate announcement preserves second source");
        auto response=RepairResponse(blockHash,full,{0});
        ReceiveMissingTransactions(unknown,response);
        Require(pending->missing==3,"unsolicited source cannot fill pending block");
        auto badResponse=response; badResponse[32+8+80]^=1;
        ReceiveMissingTransactions(first,badResponse);
        Require(pending->missing==3,"wrong short ID response rejected");
        auto badTail=RepairResponse(blockHash,full,{0,1,2}); badTail.push_back(0);
        ReceiveMissingTransactions(first,badTail);
        Require(pending->missing==3,"bad trailing record leaves entire pending block unchanged");
        ReceiveMissingTransactions(first,RepairResponse(blockHash,full,{0,0}));
        Require(pending->missing==3,"duplicate index reply rejected before mutation");
        ReceiveMissingTransactions(first,response);
        Require(pending->missing==2&&pending->hashes[0]==hashes[0],"partial response retains correct full transaction hash");
        pending->lastRequest=0; PruneCompactBlocks();
        Require(pending->sourceId==second->key&&pending->requestedSources.count(second->key)&&pending->attempts==2,"timeout rotates to alternate source");
        CompleteBlockReceive(blockHash,false,true);
        Require(compactBlocks.count(blockHash),"invalid full reply preserves outstanding compact repair");
        ReceiveMissingTransactions(first,RepairResponse(blockHash,full,{1,2}));
        Require(pending->missing==0&&CompactRootMatches(*pending)&&pending->awaitingVerification&&compactBlocks.count(blockHash)&&inflightBlocks.count(blockHash),"completed repair retained until full verification result");
        Require(pending->sourceId==first->key,"completed repair retains actual responder even after alternate request");
        auto attempts=pending->attempts;
        pending->lastRequest=GetSteadyTime()-300000000ULL; PruneCompactBlocks();
        Require(pending->awaitingVerification&&pending->attempts==attempts,"normal verification is not retried at repair interval");
        auto created=pending->created;
        pending->created=pending->lastRequest=GetSteadyTime()+1000000000ULL; PruneCompactBlocks();
        Require(compactBlocks.count(blockHash)&&pending->awaitingVerification,"future timestamp never underflows into immediate expiry");
        pending->created=created; pending->lastRequest=GetSteadyTime();

        // Simulate a local 48-bit collision: root mismatch must replace every
        // locally selected transaction, then fall back if the second root fails.
        pending->hashes[0]=hashes[1]; pending->shortEncoding=true; pending->repairedAll=false;
        RepairCompactRoot(blockHash,pending);
        Require(pending->repairedAll&&!pending->fullFallback&&pending->missing==3&&compactBlocks.count(blockHash),"first root mismatch requests all positions");
        Require(all_of(pending->transactions.begin(),pending->transactions.end(),[](const auto& tx){return tx.empty();}),"collision recovery discards local transactions");
        pending->awaitingVerification=true;
        RepairCompactRoot(blockHash,pending);
        Require(pending->fullFallback&&CompactRepairRequest(blockHash,*pending)[0]==21,"second root mismatch requests full block");
        CompleteBlockReceive(blockHash);

        ReceiveShortCompactBlock(first,compact); ReceiveShortCompactBlock(second,compact);
        pending=compactBlocks.at(blockHash);
        auto badSignature=RepairResponse(blockHash,full,{0,1,2}); badSignature[32+8+112]^=1;
        ReceiveMissingTransactions(first,badSignature);
        Require(pending->awaitingVerification&&CompactRootMatches(*pending),"signature alteration does not change advertised transaction hash");
        auto malformedFull=full; malformedFull[112+112]^=1;
        Require(!VerifyBlock(malformedFull),"full validation detects altered signature");
        CompleteBlockReceive(blockHash,false,true);
        Require(pending->fullFallback&&!pending->awaitingVerification&&pending->missing==3,"failed full encoding validation retains alternate repair state");
        PruneCompactBlocks();
        Require(pending->sourceId==second->key&&pending->attempts==4,"invalid signature causes full block request to untried announcer");
        CompleteBlockReceive(blockHash);

        ReceiveShortCompactBlock(first,compact);
        ReceiveMissingTransactions(first,RepairResponse(blockHash,full,{0,1,2}));
        auto fallbacks=compactFullFallbacks.load();
        CompleteBlockReceive(blockHash,false,false);
        Require(!compactBlocks.count(blockHash)&&compactFullFallbacks==fallbacks,"valid unselected candidate does not trigger full block retries");

        ReceiveShortCompactBlock(first,compact); ReceiveShortCompactBlock(second,compact);
        ReceiveMissingTransactions(first,RepairResponse(blockHash,full,{0,1,2}));
        pending=compactBlocks.at(blockHash); pending->lastRequest=GetSteadyTime()-6000000000ULL;
        PruneCompactBlocks();
        Require(pending->fullFallback&&!pending->awaitingVerification&&pending->sourceId==second->key,"stalled verification retries after five seconds");
        CompleteBlockReceive(blockHash);

        auto third=make_shared<Peer>(tcp::socket(io)),fourth=make_shared<Peer>(tcp::socket(io));
        for (auto peer:{third,fourth}) { peer->key=NewPeerKey(); peer->port=uint16_t(5000+peer->key); peer->compact=true; }
        for (auto peer:{first,second,third,fourth}) ReceiveShortCompactBlock(peer,compact);
        pending=compactBlocks.at(blockHash);
        pending->requestedSources.insert(second->key); pending->requestedSources.insert(third->key);
        pending->attempts=3; pending->lastRequest=0; peerPool.emplace(unknown->key,unknown);
        PruneCompactBlocks();
        Require(pending->sourceId==fourth->key,"untried announcer is preferred over unrelated connected peer");
        CompleteBlockReceive(blockHash);
        CompactBlock remembered; remembered.sources={first,second,third,fourth};
        for (auto peer:{first,second,third,fourth}) peer->closePosted=true;
        RememberCompactPeer(remembered,unknown);
        Require(remembered.sources.size()==4&&remembered.sources[0].lock()==unknown,"closed source slot reused for fresh announcer");
        for (auto peer:{first,second,third,fourth}) peer->closePosted=false;

        vector<uint8_t> legacy(full.begin(),full.begin()+112);
        for (const auto& txHash:hashes) { legacy.insert(legacy.end(),txHash.begin(),txHash.end()); legacy.push_back(0); }
        ReceiveCompactBlock(first,legacy);
        Require(compactBlocks.count(blockHash)&&!compactBlocks.at(blockHash)->shortEncoding,"legacy compact reconstruction retained");
        pending=compactBlocks.at(blockHash); pending->attempts=3; pending->lastRequest=0;
        peerPool.emplace(unknown->key,unknown);
        PruneCompactBlocks();
        Require(pending->fullFallback&&pending->attempts==4,"three unanswered indexed requests fall back to full block");
        Require(pending->sourceId==unknown->key,"unresponsive connected source does not prevent trying another peer");
        pending->attempts=6; pending->lastRequest=0; PruneCompactBlocks();
        Require(!compactBlocks.count(blockHash)&&compactBytes==0,"retry expiry releases all compact accounting");

        Block large; large.height=2; large.previousHash=block.previousHash;
        vector<array<uint8_t,32>> largeHashes;
        for (uint64_t nonce=1;nonce<=100;++nonce) {
            large.txs.push_back(GenerateTx(wallet.public_key,receiver.public_key,1,nonce,wallet));
            largeHashes.push_back(GetTransactionHash(large.txs.back()));
        }
        large.merkleRoot=MerkleCompute(largeHashes); auto largeFull=SerializeBlockALL(large);
        vector<uint8_t> largeCompact(largeFull.begin(),largeFull.begin()+112);
        for (const auto& txHash:largeHashes) { largeCompact.insert(largeCompact.end(),txHash.begin(),txHash.end()); largeCompact.push_back(1); }
        ReceiveCompactBlock(first,largeCompact); auto oversized=compactBlocks.at(large.hash);
        size_t supplied=0;
        while (!oversized->fullFallback&&supplied<large.txs.size()) {
            vector<uint8_t> reply(large.hash.begin(),large.hash.end());
            auto length=uint32_t(221+maxEvmInput); reply.resize(32+8+length,0);
            WriteU32(reply.data()+32,uint32_t(supplied)); WriteU32(reply.data()+36,length);
            memcpy(reply.data()+40,large.txs[supplied].data(),176);
            ReceiveMissingTransactions(first,reply); supplied++;
            Require(oversized->encodedBytes<=maxBlockBytes,"encoded size bounded while EVM replies arrive");
        }
        Require(oversized->fullFallback&&supplied<large.txs.size()&&
            all_of(oversized->transactions.begin(),oversized->transactions.end(),[](const auto& tx){return tx.empty();}),"oversized EVM reconstruction falls back before building full bytes");
        CompleteBlockReceive(large.hash);
        Require(compactBytes==0,"oversized EVM rejection releases cached payload accounting");
        for (size_t i=0;i<large.txs.size();++i) {
            vector<uint8_t> cached(large.txs[i].begin(),large.txs[i].end()); cached.resize(221+maxEvmInput,0);
            recentBlockTransactions.emplace(largeHashes[i],std::move(cached));
        }
        ReceiveCompactBlock(first,largeCompact); oversized=compactBlocks.at(large.hash);
        Require(oversized->encodedBytes<=maxBlockBytes&&oversized->missing>0,
            "local EVM cache cannot make a reconstruction exceed encoded block size");
        CompleteBlockReceive(large.hash); recentBlockTransactions.clear();
        Require(compactBytes==0,"local cache reconstruction accounting released");
        relayBlocks[blockHash]=make_shared<const vector<uint8_t>>(full);
        auto replies=ReadBlockReplies(21,vector<uint8_t>(blockHash.begin(),blockHash.end()));
        Require(replies.size()==1&&replies[0][0]==22&&equal(full.begin(),full.end(),replies[0].begin()+5),"full fallback serves uncommitted relay block");
        relayBlocks.clear();
        peerPool.clear();
        auto historical=ReadBlockReplies(21,vector<uint8_t>(block.previousHash.begin(),block.previousHash.end()));
        Require(historical.size()==1&&historical[0][0]==22&&!memcmp(historical[0].data()+5+80,block.previousHash.data(),32),"historical hash request reads database after relay cache cleared");
        ResetCompactHashSnapshot();
        Require(compactScanBytes==0,"pool snapshot reservations released");
        Require(compactBuilding.empty(),"compact construction reservations released");

        // A batch may finish before the reconstructed type2 message. Its bad
        // signature must not discard the valid header's alternate sources.
        ReceiveShortCompactBlock(first,compact); ReceiveShortCompactBlock(second,compact);
        ReceiveMissingTransactions(first,badSignature);
        pending=compactBlocks.at(blockHash);
        Require(pending->awaitingVerification&&CompactRootMatches(*pending),"compact awaits full signature verification before batch race");
        vector<uint8_t> batch(6,0); batch[0]=1;
        WriteU32(batch.data()+2,uint32_t(malformedFull.size()));
        batch.insert(batch.end(),malformedFull.begin(),malformedFull.end());
        RunVerificationMessage(first,43,std::move(batch));
        Require(compactBlocks.count(blockHash)&&pending->fullFallback&&!pending->awaitingVerification,
            "invalid type43 signature retains compact repair and alternate sources");
        PruneCompactBlocks();
        Require(pending->sourceId==second->key&&CompactRepairRequest(blockHash,*pending)[0]==21,
            "invalid type43 triggers full block request to alternate peer");
        RunVerificationMessage(second,22,full);
        Require(!compactBlocks.count(blockHash)&&compactBytes==0&&BlockBuffer[0].second==full,
            "alternate full reply passes actual network validation and releases pending compact");
        Require(CommitBlock(block,full)&&DBReadCurrentBlock()==blockHash&&DBReadBlockHeight()==2,
            "alternate full reply commits the correct block");
        db.reset();
        cout<<"Short IDs, collisions, malformed frames, legacy repair, alternate sources and full fallback passed\n";
        return 0;
    } catch (const exception& error) { cerr<<error.what()<<'\n'; return 1; }
}
