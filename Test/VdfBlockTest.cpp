#include <filesystem>
#include <iostream>
#include "../Network/Server.h"
#include "../EVM/BuildTransaction.h"

using namespace std;
namespace consensus=chain::vdf_consensus;
namespace election=chain::vdf_election;

size_t checks=0;
void Require(bool condition,const string& message) {
    if (!condition) throw runtime_error("FAILED: "+message);
    ++checks;
}

Wallet SeededWallet(uint8_t value) {
    array<uint8_t,crypto_sign_SEEDBYTES> seed{};
    seed.fill(value);
    Wallet wallet;
    if (crypto_sign_seed_keypair(wallet.public_key.data(),wallet.private_key.data(),seed.data()))
        throw runtime_error("Cannot create test wallet");
    return wallet;
}

election::Candidate SignCandidateWithNonce(election::Candidate candidate,const election::SecretKey& key,uint8_t nonce) {
    // A signer can use another nonce while producing a valid Ed25519 signature
    // for the same key and message. Exercise those records, not corrupt bytes.
    array<uint8_t,64> expanded{},scalarBytes{},challengeBytes{};
    array<uint8_t,32> secretScalar{},r{},challenge{},product{};
    crypto_hash_sha512(expanded.data(),key.data(),crypto_sign_SEEDBYTES);
    expanded[0]&=248;
    expanded[31]&=63;
    expanded[31]|=64;
    copy_n(expanded.begin(),32,scalarBytes.begin());
    crypto_core_ed25519_scalar_reduce(secretScalar.data(),scalarBytes.data());
    r[0]=nonce;
    Require(crypto_scalarmult_ed25519_base_noclamp(candidate.signature.data(),r.data())==0,
            "alternate Ed25519 nonce creates a group point");
    array<uint8_t,96> challengeInput{};
    copy_n(candidate.signature.begin(),32,challengeInput.begin());
    copy(candidate.publicKey.begin(),candidate.publicKey.end(),challengeInput.begin()+32);
    const auto digest=election::SigningDigest(candidate);
    copy(digest.begin(),digest.end(),challengeInput.begin()+64);
    crypto_hash_sha512(challengeBytes.data(),challengeInput.data(),challengeInput.size());
    crypto_core_ed25519_scalar_reduce(challenge.data(),challengeBytes.data());
    crypto_core_ed25519_scalar_mul(product.data(),challenge.data(),secretScalar.data());
    crypto_core_ed25519_scalar_add(candidate.signature.data()+32,r.data(),product.data());
    return candidate;
}

void TestCandidateMerge() {
    election::Context context;
    context.chainId.fill(0x31);
    context.targetEpoch=5;
    context.anchor.fill(0x44);
    context.iterations=election::kMinIterations;
    const auto wallet=SeededWallet(1);
    const auto original=election::CreateCandidate(context,wallet.private_key);
    const auto first=SignCandidateWithNonce(original,wallet.private_key,7);
    const auto second=SignCandidateWithNonce(original,wallet.private_key,11);
    Require(original.signature!=first.signature&&first.signature!=second.signature&&original.signature!=second.signature,
            "same key and proof have distinct signed encodings");
    vector<election::Candidate> variants{original,first,second};
    for (const auto& record:variants) {
        Require(election::VerifyCandidate(record,context),"alternate candidate signature and VDF both verify");
        Require(consensus::Score(record)==consensus::Score(original),"signature does not change candidate score");
    }
    const auto canonical=*min_element(variants.begin(),variants.end(),[](const auto& left,const auto& right) {
        return election::EncodeCandidate(left)<election::EncodeCandidate(right);
    });
    Require(MergeVdfCandidates(variants)==vector<election::Candidate>{canonical},
            "same-key merge retains smallest complete encoding");
    reverse(variants.begin(),variants.end());
    Require(MergeVdfCandidates(variants)==vector<election::Candidate>{canonical},
            "same-key merge ignores reception order");

    auto records=variants;
    for (uint8_t i=2;i<=6;++i) records.push_back(election::CreateCandidate(context,SeededWallet(i).private_key));
    vector<election::Candidate> expected;
    for (const auto& ranked:election::SelectTopCandidates(records,context)) expected.push_back(ranked.candidate);
    Require(expected.size()==5,"six different candidate keys select five records");
    for (size_t i=0;i<records.size();++i) {
        Require(MergeVdfCandidates(records)==expected,"runtime merge matches verified selection after rotation");
        auto reversed=records;
        reverse(reversed.begin(),reversed.end());
        Require(MergeVdfCandidates(reversed)==expected,"runtime merge matches verified selection after reversal");
        rotate(records.begin(),records.begin()+1,records.end());
    }
}

void ClearPool() {
    lock_guard lock(txpoolMutex);
    txpool.clear(); txUserPool.clear(); evmPool.clear();
    readyPoolTx=0; evmPoolBytes=0;
}

vector<uint8_t> LegacyBytes(Block block) {
    block.txNum=block.txs.size();
    const auto header=SerializeBlockHeader(block);
    crypto_generichash(block.hash.data(),block.hash.size(),header.data(),header.size(),nullptr,0);
    vector<uint8_t> data(112+block.txs.size()*176);
    memcpy(data.data(),header.data(),header.size());
    memcpy(data.data()+80,block.hash.data(),block.hash.size());
    if (!block.txs.empty()) memcpy(data.data()+112,block.txs.data(),block.txs.size()*176);
    AppendEvmPayloads(data,block.evm);
    return data;
}

void TestTailParsing(const vector<uint8_t>& bytes) {
    const auto original=UnSerializeBlock(bytes);
    Require(original.vdf.has_value(),"VDF block has extension");
    const auto tail=consensus::Encode(*original.vdf);
    const auto offset=bytes.size()-tail.size();
    Require(offset>=112+original.txNum*176,"extension starts after transactions and EVM inputs");
    optional<consensus::Extension> extension;
    EvmPayloads payloads;
    Require(ReadVdfExtension(bytes,extension)&&extension==original.vdf,"extension parser preserves fields");
    Require(ReadEvmPayloads(bytes,payloads)&&payloads==original.evm,"EVM parser preserves inputs before extension");

    for (const size_t length:{size_t(0),size_t(1),size_t(7),size_t(8),tail.size()-1}) {
        auto truncated=bytes;
        truncated.resize(offset+length);
        if (length) {
            Require(!ReadEvmPayloads(truncated,payloads)&&!ReadVdfExtension(truncated,extension),"truncated extension rejected");
        }
        Require(!VerifyBlock(truncated),"missing or truncated extension cannot verify on VDF chain");
    }
    for (const size_t relative:{size_t(0),size_t(4),size_t(8),size_t(51)}) {
        auto changed=bytes;
        changed[offset+relative]^=0x80;
        Require(!ReadEvmPayloads(changed,payloads)&&!ReadVdfExtension(changed,extension),"invalid extension frame rejected");
    }
    auto changed=bytes;
    changed[offset+50]=6;
    Require(!ReadVdfExtension(changed,extension),"more than five candidates rejected");
    changed=bytes;
    changed.push_back(0);
    Require(!ReadEvmPayloads(changed,payloads)&&!VerifyBlock(changed),"bytes after extension rejected");
    changed=bytes;
    changed.insert(changed.end(),tail.begin(),tail.end());
    Require(!ReadVdfExtension(changed,extension),"duplicate extension rejected");

    // Marker bytes inside a transaction signature are ordinary transaction data.
    vector<uint8_t> native(112+176);
    WriteU64(native.data()+72,1);
    memcpy(native.data()+native.size()-8,"VDB1",4);
    WriteU32(native.data()+native.size()-4,120);
    Require(ReadEvmPayloads(native,payloads)&&payloads.empty(),"signature marker does not become a trailer");
    Require(ReadVdfExtension(native,extension)&&!extension,"signature bytes are not stripped as an extension");
    native.push_back(0);
    Require(!ReadVdfExtension(native,extension),"extra byte after native transactions rejected");

    if (!original.evm.empty()) {
        const size_t transactionEnd=112+original.txNum*176;
        changed.assign(bytes.begin(),bytes.begin()+transactionEnd);
        changed.insert(changed.end(),tail.begin(),tail.end());
        changed.insert(changed.end(),bytes.begin()+transactionEnd,bytes.begin()+offset);
        Require(!ReadEvmPayloads(changed,payloads),"EVM payloads after VDF extension rejected");
        changed=bytes;
        WriteU32(changed.data()+transactionEnd+12,UINT32_MAX);
        Require(!ReadVdfExtension(changed,extension),"invalid EVM input length cannot hide extension boundary");
    }
}

void TestCommitment(const vector<uint8_t>& bytes) {
    auto original=UnSerializeBlock(bytes);
    Require(VerifyBlock(bytes),"untampered block verifies");
    Require(SerializeBlockALL(original)==bytes,"block serialization preserves complete VDF bytes");
    vector<array<uint8_t,32>> hashes;
    for (const auto& tx:original.txs) hashes.push_back(GetTransactionHash(tx));
    const auto txRoot=MerkleCompute(hashes);
    Require(consensus::MerkleRoot(txRoot,*original.vdf)==original.merkleRoot,"header commits to transaction root and unsigned extension");

    for (int field=0;field<5;++field) {
        auto changed=original;
        if (field==0) ++changed.vdf->slot;
        if (field==1) changed.vdf->producer[0]^=1;
        if (field==2) ++changed.vdf->candidates.front().context.targetEpoch;
        if (field==3) changed.vdf->candidates.front().proof[0]^=1;
        if (field==4) changed.vdf->producerProof->signature[0]^=1;
        Require(consensus::MerkleRoot(txRoot,*changed.vdf)!=original.merkleRoot,"unsigned extension field changes commitment");
        Require(!VerifyBlock(SerializeBlockALL(changed)),"changed unsigned extension cannot keep old header");
    }
    auto changed=original;
    changed.vdf->signature[0]^=1;
    Require(consensus::MerkleRoot(txRoot,*changed.vdf)==original.merkleRoot,"producer signature excluded from its own commitment");
    Require(!VerifyBlock(SerializeBlockALL(changed)),"changed producer signature rejected");
    changed=original;
    ++changed.height;
    Require(!VerifyBlock(SerializeBlockALL(changed)),"producer signature binds header height");
    changed=original;
    changed.previousHash[0]^=1;
    Require(!VerifyBlock(SerializeBlockALL(changed)),"producer signature binds parent hash");
}

void TestVdfAttemptRetry(const Wallet& producer) {
    const auto clock=consensus::currentTimeMs;
    uint64_t slot=100;
    consensus::currentTimeMs=[&] { return slot*consensus::config.slotMs; };
    vdfPublicKey=producer.public_key; vdfIdentityReady=true;
    const auto parent=DBReadCurrentBlock();
    consensus::Extension extension;
    const auto attempts=vdfPrepareAttempts.load();
    Require(!PrepareVdfBlock(2,parent,extension),"missing local proof declines production");
    const auto skipped=vdfDeclinedAttemptHits.load();
    for (size_t i=0;i<8;++i)
        Require(!PrepareVdfBlock(2,parent,extension),"same parent and slot reuse failed production check");
    Require(vdfPrepareAttempts==attempts+1&&vdfDeclinedAttemptHits==skipped+8,
            "repeated attempts do not reread production state");
    ++slot;
    Require(!PrepareVdfBlock(2,parent,extension)&&vdfPrepareAttempts==attempts+2,
            "new slot performs its own production check");
    --slot;
    Require(!PrepareVdfBlock(2,parent,extension)&&vdfPrepareAttempts==attempts+3,
            "clock rollback does not reuse another slot's result");
    auto changedParent=parent; changedParent[0]^=1;
    Require(!WasVdfAttemptDeclined(MakeVdfAttempt(2,changedParent,slot)),
            "different branch parent never reuses a declined attempt");

    const auto proof=election::CreateCandidate(DBVdfContext(0),producer.private_key);
    Require(consensus::VerifyProof(proof,DBVdfContext(0)),"new local proof is valid");
    const auto oldProofVersion=vdfOwnProofVersion.load();
    StoreVdfCandidate(proof,true);
    Require(vdfOwnProofVersion==oldProofVersion+1&&
            !WasVdfAttemptDeclined(MakeVdfAttempt(2,parent,slot)),
            "proof completion invalidates the declined attempt in the same slot");
    Require(PrepareVdfBlock(2,parent,extension)&&extension.producerProof==proof,
            "proof completion allows immediate production retry");
    StoreVdfCandidate(proof,true);
    Require(vdfOwnProofVersion==oldProofVersion+1,"duplicate proof does not create repeated retries");
    vdfBootstrapProduce=false;
    Require(!PrepareVdfBlock(2,parent,extension),"disabled bootstrap production is declined");
    vdfBootstrapProduce=true;
    Require(PrepareVdfBlock(2,parent,extension),"restored bootstrap setting performs a fresh check");

    { lock_guard lock(vdfRuntimeMutex); vdfOwnProofs.clear(); vdfCandidateSets.clear(); }
    { lock_guard lock(vdfAttemptMutex); vdfDeclinedAttempt.reset(); }
    vdfIdentityReady=false;
    consensus::currentTimeMs=clock;
}

int main(int argc,char** argv) {
    try {
        Require(sodium_init()>=0,"libsodium initializes");
        TestCandidateMerge();
        spdlog::set_level(spdlog::level::off);
        const auto root=filesystem::path(argc>1?argv[1]:"vdf-block-data")/to_string(GetSteadyTime());
        filesystem::create_directories(root);
        const auto producer=SeededWallet(1),sender=SeededWallet(2),receiver=SeededWallet(3);
        auto accounts=nlohmann::json::array();
        for (const auto& wallet:{producer,sender})
            accounts.push_back({{"public_key",U32ToHex(wallet.public_key)},{"balance",1000000000000ULL}});
        SetGenesisUsers({{"base_fee",1},{"accounts",accounts},
            {"vdf",{{"enabled",true},{"epoch_blocks",32},{"iterations",election::kMinIterations},
                    {"slot_ms",50},{"start_time_ms",50},{"fallback_slots",20},{"future_slots",2},{"producers",5}}}});
        // Only format, signatures and byte budgets are exercised here. Branch
        // committee/slot rules have their own consensus and database tests.
        Require(consensus::config.enabled&&consensus::config.iterations==election::kMinIterations,
                "genesis configuration enables VDF block format");
        InitDB(root.string(),true); GenerateGenesisBlock();
        const auto genesis=DBReadBlockByHeight("1");
        Require(genesis.size()==112&&VerifyBlock(genesis),"genesis keeps the original header-only encoding");
        TestVdfAttemptRetry(producer);

        consensus::Extension proposal;
        proposal.slot=100;
        proposal.producer=producer.public_key;
        election::Context context{genesisRoot,1,DBReadCurrentBlock(),election::kMinIterations};
        for (uint8_t i=4;i<9;++i) proposal.candidates.push_back(election::CreateCandidate(context,SeededWallet(i).private_key));
        proposal.producerProof=election::CreateCandidate(context,producer.private_key);
        prepareVdfBlock=[&](uint64_t height,const array<uint8_t,32>& parent,consensus::Extension& extension) {
            Require(height==2&&parent==DBReadCurrentBlock(),"preparation receives its database parent");
            extension=proposal;
            return true;
        };
        signVdfBlock=[&](const array<uint8_t,32>& hash,consensus::Signature& signature) {
            signature=consensus::SignHash(hash,producer.private_key);
            return true;
        };
        const auto empty=GenerateBlock();
        Require(!empty.empty()&&UnSerializeBlock(empty).txNum==0,"VDF chain generates a signed empty block");
        TestCommitment(empty); TestTailParsing(empty);
        auto genesisWithTail=genesis;
        const auto tail=consensus::Encode(*UnSerializeBlock(empty).vdf);
        genesisWithTail.insert(genesisWithTail.end(),tail.begin(),tail.end());
        Require(!VerifyBlock(genesisWithTail),"genesis cannot carry a VDF extension");

        const auto tx=GenerateTx(producer.public_key,receiver.public_key,5,1,producer);
        Require(ProcessTxPackage(vector<uint8_t>(tx.begin(),tx.end()))==1,"native transaction admitted");
        vector<uint8_t> input(24,0);
        memcpy(input.data()+input.size()-8,"VDB1",4);
        WriteU32(input.data()+input.size()-4,120);
        Require(ProcessEvmTx(MakeEvmTx(sender,{},input,1,0,100000,true)),"EVM transaction containing trailer marker admitted");
        const auto mixed=GenerateBlock();
        const auto mixedBlock=UnSerializeBlock(mixed);
        Require(mixedBlock.txNum==2&&mixedBlock.evm.size()==1,"native and EVM transactions coexist with VDF");
        TestCommitment(mixed); TestTailParsing(mixed);

        consensus::config.enabled=false;
        Require(!VerifyBlock(mixed)&&!VerifyBlock(empty),"legacy chain rejects VDF extensions");
        const auto legacy=GenerateBlock();
        auto legacyBlock=UnSerializeBlock(legacy);
        Require(!legacyBlock.vdf&&VerifyBlock(legacy),"legacy generation has no VDF extension");
        Require(legacy==LegacyBytes(legacyBlock),"legacy serialization remains byte-for-byte unchanged");
        ClearPool();
        Require(GenerateBlock().empty(),"legacy empty pool still produces no block");
        consensus::config.enabled=true;
        Require(!VerifyBlock(legacy),"VDF chain rejects a non-genesis block without its extension");

        // Forty-three inputs fit the legacy budget with eight bytes to spare.
        // The VDF extension must be reserved before selecting transactions.
        constexpr size_t count=43;
        constexpr size_t perTransaction=(maxBlockBytes-120)/count;
        vector<uint8_t> largeInput(perTransaction-176-8-45,0);
        for (uint64_t nonce=1;nonce<=count;++nonce)
            Require(ProcessEvmTx(MakeEvmTx(sender,{},largeInput,nonce,0,400000,true)),"large EVM transaction admitted");
        maxBlockTx=maxProtocolTx;
        const auto bounded=GenerateBlock();
        const auto boundedBlock=UnSerializeBlock(bounded);
        Require(boundedBlock.txNum==count-1&&boundedBlock.evm.size()==count-1,"selection reserves VDF extension bytes");
        Require(bounded.size()<=maxBlockBytes&&VerifyBlock(bounded),"full VDF block stays within protocol byte budget");
        Require(txpool.size()==count,"generation preserves uncommitted transactions");
        consensus::config.enabled=false;
        const auto legacyFull=GenerateBlock();
        Require(UnSerializeBlock(legacyFull).txNum==count&&legacyFull.size()<=maxBlockBytes,"same transactions fill legacy budget");
        consensus::config.enabled=true;

        const auto prepare=prepareVdfBlock;
        const auto sign=signVdfBlock;
        prepareVdfBlock=[](uint64_t,const array<uint8_t,32>&,consensus::Extension&) { return false; };
        Require(GenerateBlock().empty(),"runtime can decline block generation");
        prepareVdfBlock=prepare;
        signVdfBlock=[](const array<uint8_t,32>&,consensus::Signature&) { return false; };
        Require(GenerateBlock().empty(),"signing failure produces no block");
        signVdfBlock=[&](const array<uint8_t,32>& hash,consensus::Signature& signature) {
            signature=consensus::SignHash(hash,receiver.private_key);
            return true;
        };
        Require(GenerateBlock().empty(),"wrong producer signing key produces no block");
        signVdfBlock=sign;
        db.reset();
        cout<<"PASS "<<checks<<" VDF block checks. Database: "<<root.string()<<'\n';
        return 0;
    } catch (const exception& error) {
        cerr<<error.what()<<'\n';
        return 1;
    }
}
