#ifndef CHAIN_DB_H
#define CHAIN_DB_H
#include <atomic>
#include <algorithm>
#include <charconv>
#include <memory>
#include <map>
#include <mutex>
#include <shared_mutex>
#include <vector>
#include <deque>
#include <unordered_set>
#include <thread>
#include <tuple>
#include <rocksdb/db.h>
#include <rocksdb/version.h>
#include <rocksdb/options.h>
#include <rocksdb/write_batch.h>
#include <rocksdb/statistics.h>
#include <rocksdb/filter_policy.h>
#include <rocksdb/table.h>
#include <rocksdb/perf_context.h>
#include <rocksdb/perf_level.h>
#include <rocksdb/utilities/write_batch_with_index.h>
#include <spdlog/spdlog.h>
#include "../Tool/Tool.h"
#include "../Time/Time.h"
#include "../User/User.h"
#include "../Consensus/WorkRules.h"
#include "../EVM/Execute.h"
using namespace std;

unique_ptr<rocksdb::DB> db;
rocksdb::Options options;
shared_mutex dbCommitMutex;
bool dbSync=true;
bool dbLegacyKeys=false;
bool experimentIndex=false;
atomic<uint64_t> dbWriteNs{0},dbWriteCount{0};
atomic<uint64_t> dbPrepareNs{0},dbBatchBytes{0};
bool dbProfile=false;
atomic<uint64_t> dbProfileWrites{0},dbWalNs{0},dbMemtableNs{0},dbDelayNs{0},dbOtherNs{0};
struct ChainHead {
    uint64_t height=0,transactions=0,version=0,burned=0;
    size_t users=0,contracts=0;
    array<uint8_t,32> hash{};
};
ChainHead publishedHead;
mutex chainHeadMutex;
ChainHead ReadChainHead() { lock_guard lock(chainHeadMutex); return publishedHead; }
void PublishChainHead();
UserMap users;
uint64_t userBurned=0;
UserMap poolUsers;
shared_mutex poolUsersMutex;
void PublishPoolUsers(const UserMap* changed=nullptr,const unordered_set<string>* keys=nullptr) {
    unique_lock lock(poolUsersMutex);
    if (changed) {
        for (const auto& [key,user]:*changed) poolUsers[key]=user;
    } else if (keys) {
        for (const auto& encoded:*keys) if (encoded.starts_with("user/")&&encoded.size()==37) {
            array<uint8_t,32> key; memcpy(key.data(),encoded.data()+5,32);
            if (auto found=users.find(key);found!=users.end()) poolUsers[key]=found->second;
            else poolUsers.erase(key);
        }
    } else poolUsers=users;
}
atomic<uint64_t> userCheckNs{0},userWriteCount{0},invalidUserBlocks{0};
// 分支切换使用临时批次；所有读取看到同一批次中的回滚和重放结果。
thread_local rocksdb::WriteBatchWithIndex* branchBatch=nullptr;
atomic<uint64_t> reorgCount{0};
atomic<uint64_t> maxReorgDepth{0};
uint64_t dbStateVersion=0;
// Set before node threads start. Native execution has no block timestamp or
// contract environment; all account changes still belong to the locked parent.
bool executionPrefixReuse=true;
unsigned nativeExecutionThreads=1;
struct NativeExecutionUndo {
    User sender,receiver;
    bool hadSender=false,hadReceiver=false;
};
struct CachedExecution {
    vector<uint8_t> data;
    array<uint8_t,32> parent{};
    uint64_t version=0,height=0;
    UserMap users;
    EvmExecution evm;
    vector<NativeExecutionUndo> nativeUndo;
    array<uint8_t,32> genesis{};
    uint64_t fee=0,supply=0,burned=0;
    size_t bytes=0;
};
mutex executionMutex;
deque<unique_ptr<CachedExecution>> executionCache;
size_t executionBytes=0;
constexpr size_t maxExecutionBytes=32*1024*1024;
atomic<uint64_t> executionCacheHits{0},executionCacheMisses{0},candidateExecuteNs{0},commitExecuteNs{0};
atomic<uint64_t> candidateRankSkips{0},candidateExecutionCacheHits{0},candidateExecutions{0};
atomic<uint64_t> executionPrefixHits{0},executionPrefixTransactions{0};
atomic<uint64_t> nativeParallelBlocks{0},nativeParallelTransactions{0};
atomic<uint64_t> evmAccountWrites{0},evmSlotWrites{0},evmPersistNs{0};
atomic<uint64_t> commitLockWaitNs{0},candidateLockWaitNs{0};
mutex vdfExtensionMutex;
unordered_map<array<uint8_t,32>,shared_ptr<const chain::vdf_consensus::Extension>,GetMapHash> vdfExtensions;
deque<array<uint8_t,32>> vdfExtensionOrder;
constexpr size_t maxVdfExtensions=512;
atomic<uint64_t> vdfExtensionCacheHits{0},vdfExtensionLoads{0},vdfExtensionReadBytes{0};
mutex workStateMutex;
unordered_map<array<uint8_t,32>,shared_ptr<const chain::work_consensus::State>,GetMapHash> workStates;
deque<array<uint8_t,32>> workStateOrder;
mutex workAnchorMutex;
unordered_map<array<uint8_t,32>,array<uint8_t,32>,GetMapHash> workAnchors;
deque<array<uint8_t,32>> workAnchorOrder;
atomic<uint64_t> workStateCacheHits{0},workStateReplays{0},workAnchorCacheHits{0};
void ClearWorkStateCache() {
    { lock_guard lock(workStateMutex); workStates.clear(); workStateOrder.clear(); }
    { lock_guard lock(workAnchorMutex); workAnchors.clear(); workAnchorOrder.clear(); }
    chain::work_consensus::proofCache.Clear();
}
void ClearVdfExtensionCache() {
    lock_guard lock(vdfExtensionMutex);
    vdfExtensions.clear(); vdfExtensionOrder.clear();
}
void InvalidateExecutionCache() {
    dbStateVersion++;
    lock_guard lock(executionMutex); executionCache.clear(); executionBytes=0;
}
string DBEvmKey(const char* prefix,const evmc::address& address) {
    string key(prefix); key.append(reinterpret_cast<const char*>(address.bytes),20); return key;
}
string DBEvmSlotKey(const evmc::address& address,const evmc::bytes32& slot) {
    auto key=DBEvmKey("evm-slot/",address); key.append(reinterpret_cast<const char*>(slot.bytes),32); return key;
}

void CheckDBStatus(const rocksdb::Status& status) {
    if (!status.ok()) throw runtime_error("RocksDB: "+status.ToString());
}
string DBHashKey(const char* prefix,const array<uint8_t,32>& hash) {
    string key(prefix);
    key.append(reinterpret_cast<const char*>(hash.data()),hash.size());
    return key;
}
bool DBGet(const string& key,string& value) {
    auto status=branchBatch?branchBatch->GetFromBatchAndDB(db.get(),rocksdb::ReadOptions(),key,&value):db->Get(rocksdb::ReadOptions(),key,&value);
    if (status.IsNotFound()) return false;
    CheckDBStatus(status);
    return true;
}
void InitDB(const string& path="test",bool sync=true) {
    ClearVdfExtensionCache();
    ClearWorkStateCache();
    dbSync=sync;
    options=rocksdb::Options();
    options.create_if_missing=true;
    options.statistics=rocksdb::CreateDBStatistics();
    // 布隆过滤器加速历史查询中不存在的键，减少读取磁盘数据块。
    rocksdb::BlockBasedTableOptions tableOptions;
    tableOptions.filter_policy.reset(rocksdb::NewBloomFilterPolicy(10,false));
    options.table_factory.reset(rocksdb::NewBlockBasedTableFactory(tableOptions));
#if ROCKSDB_MAJOR >= 11
    unique_ptr<rocksdb::DB> openedDB;
    CheckDBStatus(rocksdb::DB::Open(options,path,&openedDB));
    db=std::move(openedDB);
#else
    rocksdb::DB* openedDB=nullptr;
    CheckDBStatus(rocksdb::DB::Open(options,path,&openedDB));
    db.reset(openedDB);
#endif
    string version;
    if (DBGet("SchemaVersion",version)) {
        if (version!="7") throw runtime_error("Height transaction indexes require schema 7. Use a NEW --data directory with --genesis.");
        string saved;
        if (!DBGet("GenesisConfig",saved)) throw runtime_error("Missing genesis account configuration");
        auto config=nlohmann::json::parse(saved);
        if (genesisConfigured&&GetGenesisConfig()!=config) throw runtime_error("Genesis configuration does not match this database");
        if (!genesisConfigured) SetGenesisUsers(config);
    } else {
        unique_ptr<rocksdb::Iterator> it(db->NewIterator(rocksdb::ReadOptions()));
        it->SeekToFirst();
        if (it->Valid()) throw runtime_error("Unversioned database: use a NEW account database directory");
        CheckDBStatus(it->status());
        if (!genesisConfigured) SetGenesisUsers({{"base_fee",0},{"accounts",nlohmann::json::array()}});
    }
    users.clear();
    userBurned=0;
    uint64_t balanceTotal=0;
    unique_ptr<rocksdb::Iterator> it(db->NewIterator(rocksdb::ReadOptions()));
    for (it->Seek("user/");it->Valid()&&it->key().starts_with("user/");it->Next()) {
        if (it->key().size()!=37||it->value().size()!=16) throw runtime_error("Invalid persisted account");
        array<uint8_t,32> key;
        memcpy(key.data(),it->key().data()+5,32);
        auto value=reinterpret_cast<const uint8_t*>(it->value().data());
        User user{ReadU64(value),ReadU64(value+8)};
        if (UINT64_MAX-balanceTotal<user.balance) throw runtime_error("Account supply overflow");
        balanceTotal+=user.balance;
        users.emplace(key,user);
    }
    CheckDBStatus(it->status());
    evmState=EvmState{};
    for (it->Seek("evm/");it->Valid()&&it->key().starts_with("evm/");it->Next()) {
        if (it->key().size()!=24) throw runtime_error("Invalid EVM account key");
        evmc::address address; memcpy(address.bytes,it->key().data()+4,20);
        auto account=UnserializeEvmAccount(it->value().ToString());
        if (account.balance>UINT64_MAX-balanceTotal) throw runtime_error("EVM supply overflow");
        balanceTotal+=static_cast<uint64_t>(account.balance);
        evmState.insert(address,std::move(account));
    }
    CheckDBStatus(it->status());
    for (it->Seek("evm-code/");it->Valid()&&it->key().starts_with("evm-code/");it->Next()) {
        if (it->key().size()!=29) throw runtime_error("Invalid EVM code key");
        evmc::address address; memcpy(address.bytes,it->key().data()+9,20);
        auto found=evmState.get_accounts().find(address);
        if (found==evmState.get_accounts().end()) throw runtime_error("Orphan EVM code");
        auto code=it->value(); found->second.code.assign(reinterpret_cast<const uint8_t*>(code.data()),code.size());
    }
    CheckDBStatus(it->status());
    for (it->Seek("evm-slot/");it->Valid()&&it->key().starts_with("evm-slot/");it->Next()) {
        if (it->key().size()!=61||it->value().size()!=32) throw runtime_error("Invalid EVM slot");
        evmc::address address; evmc::bytes32 key,value;
        memcpy(address.bytes,it->key().data()+9,20); memcpy(key.bytes,it->key().data()+29,32); memcpy(value.bytes,it->value().data(),32);
        auto found=evmState.get_accounts().find(address);
        if (found==evmState.get_accounts().end()||value==evmc::bytes32{}) throw runtime_error("Orphan or zero EVM slot");
        found->second.storage[key]={value,value,EVMC_ACCESS_COLD};
    }
    CheckDBStatus(it->status());
    if (!version.empty()) {
        string burned;
        if (!DBGet("UserBurned",burned)) throw runtime_error("Missing fee total");
        auto result=from_chars(burned.data(),burned.data()+burned.size(),userBurned);
        if (result.ec!=errc()||result.ptr!=burned.data()+burned.size()||userBurned>genesisSupply||balanceTotal!=genesisSupply-userBurned) throw runtime_error("Account supply mismatch");
    }
    spdlog::info("RocksDB Started. Path:{}, Sync:{}",path,sync);
    InvalidateExecutionCache();
    PublishPoolUsers();
    PublishChainHead();
}
void DBWriteBatch(rocksdb::WriteBatch& batch) {
    if (branchBatch) {
        struct Copy:rocksdb::WriteBatch::Handler {
            void Put(const rocksdb::Slice& key,const rocksdb::Slice& value) override { branchBatch->Put(key,value); }
            void Delete(const rocksdb::Slice& key) override { branchBatch->Delete(key); }
        } copy;
        CheckDBStatus(batch.Iterate(&copy));
        return;
    }
    auto start=GetSteadyTime();
    bool sampled=dbProfile&&dbWriteCount.load()%64==0;
    auto context=sampled?rocksdb::get_perf_context():nullptr;
    uint64_t wal=0,memtable=0,delay=0,other=0;
    struct PerfLevel {
        bool active; rocksdb::PerfLevel previous;
        ~PerfLevel() { if (active) rocksdb::SetPerfLevel(previous); }
    } level{sampled,sampled?rocksdb::GetPerfLevel():rocksdb::kDisable};
    if (sampled) {
        wal=context->write_wal_time; memtable=context->write_memtable_time;
        delay=context->write_delay_time; other=context->write_pre_and_post_process_time;
        rocksdb::SetPerfLevel(rocksdb::kEnableTimeExceptForMutex);
    }
    rocksdb::WriteOptions writeOptions;
    writeOptions.sync=dbSync;
    CheckDBStatus(db->Write(writeOptions,&batch));
    if (sampled) {
        dbWalNs+=context->write_wal_time-wal; dbMemtableNs+=context->write_memtable_time-memtable;
        dbDelayNs+=context->write_delay_time-delay; dbOtherNs+=context->write_pre_and_post_process_time-other; dbProfileWrites++;
    }
    dbWriteNs+=GetSteadyTime()-start;
    dbBatchBytes+=batch.Data().size();
    dbWriteCount++;
}
void DBWriteTx(const array<uint8_t,32>& key,const array<uint8_t,176>& value) {
    rocksdb::WriteBatch batch;
    batch.Put(DBHashKey("tx/",key),rocksdb::Slice(reinterpret_cast<const char*>(value.data()),value.size()));
    DBWriteBatch(batch);
}
bool DBHasTx(const array<uint8_t,32>& key) {
    string value;
    return DBGet(DBHashKey("tx/",key),value);
}
void DBAddUser(rocksdb::WriteBatch& batch,const array<uint8_t,32>& key,const User& user) {
    array<uint8_t,16> value;
    WriteU64(value.data(),user.balance);
    WriteU64(value.data()+8,user.nonce);
    batch.Put(DBHashKey("user/",key),rocksdb::Slice(reinterpret_cast<const char*>(value.data()),value.size()));
}
void ApplyMemoryKey(const rocksdb::Slice& key,const rocksdb::Slice* value) {
    if (key.starts_with("user/")&&key.size()==37) {
        array<uint8_t,32> address; memcpy(address.data(),key.data()+5,32);
        if (!value) users.erase(address);
        else {
            if (value->size()!=16) throw runtime_error("Invalid account undo");
            auto data=reinterpret_cast<const uint8_t*>(value->data());
            users[address]={ReadU64(data),ReadU64(data+8)};
        }
    } else if (key.starts_with("evm/")&&key.size()==24) {
        evmc::address address; memcpy(address.bytes,key.data()+4,20);
        if (!value) evmState.get_accounts().erase(address);
        else {
            auto header=UnserializeEvmAccount(value->ToString()); auto& account=evmState.get_accounts()[address];
            account.nonce=header.nonce; account.balance=header.balance;
        }
    } else if (key.starts_with("evm-code/")&&key.size()==29) {
        evmc::address address; memcpy(address.bytes,key.data()+9,20);
        if (value) evmState.get_accounts()[address].code.assign(reinterpret_cast<const uint8_t*>(value->data()),value->size());
        else if (auto it=evmState.get_accounts().find(address);it!=evmState.get_accounts().end()) it->second.code.clear();
    } else if (key.starts_with("evm-slot/")&&key.size()==61) {
        evmc::address address; evmc::bytes32 slot,word;
        memcpy(address.bytes,key.data()+9,20); memcpy(slot.bytes,key.data()+29,32);
        if (value) {
            if (value->size()!=32) throw runtime_error("Invalid EVM slot undo");
            memcpy(word.bytes,value->data(),32); evmState.get_accounts()[address].storage[slot]={word,word,EVMC_ACCESS_COLD};
        } else if (auto it=evmState.get_accounts().find(address);it!=evmState.get_accounts().end()) it->second.storage.erase(slot);
    }
}

struct BranchMemory {
    rocksdb::WriteBatch saved;
    unordered_set<string> keys;
    void Save(const rocksdb::Slice& key) {
        if (!(key.starts_with("user/")||key.starts_with("evm/")||key.starts_with("evm-code/")||key.starts_with("evm-slot/"))) return;
        if (!keys.insert(key.ToString()).second) return;
        if (key.starts_with("user/")&&key.size()==37) {
            array<uint8_t,32> address; memcpy(address.data(),key.data()+5,32);
            if (auto found=users.find(address);found!=users.end()) DBAddUser(saved,address,found->second);
            else saved.Delete(key);
            return;
        }
        auto prefix=key.starts_with("evm/")?size_t(4):size_t(9);
        if (key.size()!=(key.starts_with("evm-slot/")?size_t(61):prefix+20)) throw runtime_error("Invalid branch state key");
        evmc::address address; memcpy(address.bytes,key.data()+prefix,20);
        auto found=evmState.get_accounts().find(address);
        if (found==evmState.get_accounts().end()) { saved.Delete(key); return; }
        if (key.starts_with("evm/")) saved.Put(key,SerializeEvmHeader(found->second));
        else if (key.starts_with("evm-code/")) {
            const auto& code=found->second.code;
            if (code.empty()) saved.Delete(key);
            else saved.Put(key,rocksdb::Slice(reinterpret_cast<const char*>(code.data()),code.size()));
        } else {
            evmc::bytes32 slot; memcpy(slot.bytes,key.data()+29,32); auto word=evmState.ReadStorage(address,slot);
            if (word==evmc::bytes32{}) saved.Delete(key);
            else saved.Put(key,rocksdb::Slice(reinterpret_cast<const char*>(word.bytes),32));
        }
    }
    void Restore() {
        struct Apply:rocksdb::WriteBatch::Handler {
            void Put(const rocksdb::Slice& key,const rocksdb::Slice& value) override { ApplyMemoryKey(key,&value); }
            void Delete(const rocksdb::Slice& key) override { ApplyMemoryKey(key,nullptr); }
        } apply;
        CheckDBStatus(saved.Iterate(&apply));
    }
};
thread_local BranchMemory* branchMemory=nullptr;
void SaveBranchMemory(rocksdb::WriteBatch& batch) {
    if (!branchMemory) return;
    struct Save:rocksdb::WriteBatch::Handler {
        void Put(const rocksdb::Slice& key,const rocksdb::Slice&) override { branchMemory->Save(key); }
        void Delete(const rocksdb::Slice& key) override { branchMemory->Save(key); }
    } save;
    CheckDBStatus(batch.Iterate(&save));
}

bool DBValidateVdfBlock(const vector<uint8_t>& data,bool committing=false);
NativeExecutionUndo DBNativeUndo(const UserMap& changed,span<const uint8_t,176> tx) {
    array<uint8_t,32> sender,receiver;
    memcpy(sender.data(),tx.data(),32); memcpy(receiver.data(),tx.data()+32,32);
    return {GetUser(users,changed,sender),GetUser(users,changed,receiver),
        changed.contains(sender),changed.contains(receiver)};
}
void DBRewindNativeExecution(const vector<uint8_t>& data,const vector<NativeExecutionUndo>& undo,
                             size_t keep,UserMap& changed) {
    for (size_t i=undo.size();i>keep;) {
        --i;
        array<uint8_t,32> sender,receiver;
        memcpy(sender.data(),data.data()+112+i*176,32);
        memcpy(receiver.data(),data.data()+144+i*176,32);
        if (undo[i].hadSender) changed[sender]=undo[i].sender; else changed.erase(sender);
        if (undo[i].hadReceiver) changed[receiver]=undo[i].receiver; else changed.erase(receiver);
    }
}
// Every connected component of sender/receiver accounts is executed in its
// original order. Components cannot read or write one another's native state.
// Workers never publish partial changes, including when a later transfer fails.
bool DBApplyNativeTransactions(const vector<uint8_t>& data,size_t begin,size_t count,UserMap& changed,
                               vector<NativeExecutionUndo>* undo=nullptr) {
    if (undo) undo->resize(count);
    const auto remaining=count-begin;
    const auto requested=min(unsigned(16),nativeExecutionThreads);
    vector<vector<size_t>> lanes;
    if (requested>1&&remaining>=512) {
        unordered_map<array<uint8_t,32>,size_t,GetMapHash> accounts;
        accounts.reserve(remaining*2);
        vector<size_t> parent,weight;
        auto index=[&](const uint8_t* key) {
            array<uint8_t,32> account; memcpy(account.data(),key,32);
            auto [entry,inserted]=accounts.try_emplace(account,accounts.size());
            if (inserted) { parent.push_back(entry->second); weight.push_back(1); }
            return entry->second;
        };
        auto root=[&](size_t n) {
            while (n!=parent[n]) { parent[n]=parent[parent[n]]; n=parent[n]; }
            return n;
        };
        vector<size_t> senders; senders.reserve(remaining);
        for (size_t i=begin;i<count;i++) {
            const auto* tx=data.data()+112+i*176;
            auto sender=index(tx),receiver=index(tx+32); senders.push_back(sender);
            sender=root(sender); receiver=root(receiver);
            if (sender!=receiver) {
                if (weight[sender]<weight[receiver]) swap(sender,receiver);
                parent[receiver]=sender; weight[sender]+=weight[receiver];
            }
        }
        vector<vector<size_t>> components(parent.size());
        for (size_t i=0;i<senders.size();i++) components[root(senders[i])].push_back(begin+i);
        vector<size_t> order;
        for (size_t i=0;i<components.size();i++) if (!components[i].empty()) order.push_back(i);
        if (order.size()>1) {
            stable_sort(order.begin(),order.end(),[&](size_t a,size_t b) { return components[a].size()>components[b].size(); });
            lanes.resize(min(size_t(requested),order.size()));
            for (const auto component:order) {
                auto target=min_element(lanes.begin(),lanes.end(),[](const auto& a,const auto& b) { return a.size()<b.size(); });
                target->insert(target->end(),components[component].begin(),components[component].end());
            }
        }
    }
    if (lanes.empty()) {
        for (size_t i=begin;i<count;i++) {
            auto tx=span<const uint8_t,176>(data.data()+112+i*176,176);
            if (undo) (*undo)[i]=DBNativeUndo(changed,tx);
            if (!ApplyUserTx(users,changed,tx)) return false;
        }
        return true;
    }
    vector<UserMap> results(lanes.size());
    atomic<bool> accepted{true}; exception_ptr failure; mutex failureMutex;
    auto execute=[&](size_t part) {
        try {
            auto& result=results[part]; result.reserve(lanes[part].size()*2);
            for (const auto i:lanes[part]) {
                if (!accepted.load(memory_order_relaxed)) break;
                auto tx=span<const uint8_t,176>(data.data()+112+i*176,176);
                array<uint8_t,32> sender,receiver;
                memcpy(sender.data(),tx.data(),32); memcpy(receiver.data(),tx.data()+32,32);
                const bool hadSender=result.contains(sender)||changed.contains(sender);
                const bool hadReceiver=result.contains(receiver)||changed.contains(receiver);
                if (!result.contains(sender)) result.emplace(sender,GetUser(users,changed,sender));
                if (!result.contains(receiver)) result.emplace(receiver,GetUser(users,changed,receiver));
                if (undo) (*undo)[i]={result.at(sender),result.at(receiver),hadSender,hadReceiver};
                if (!ApplyUserTx(users,result,tx)) { accepted=false; break; }
            }
        } catch (...) { lock_guard lock(failureMutex); failure=current_exception(); accepted=false; }
    };
    {
        vector<jthread> workers; workers.reserve(lanes.size()-1);
        for (size_t part=1;part<lanes.size();part++) {
            try { workers.emplace_back(execute,part); }
            catch (const system_error&) { execute(part); }
        }
        execute(0);
    }
    if (failure) rethrow_exception(failure);
    if (!accepted.load()) return false;
    for (auto& result:results) for (auto& [key,user]:result) changed[key]=user;
    nativeParallelBlocks++; nativeParallelTransactions+=remaining;
    return true;
}
bool DBApplyBlockUsers(const vector<uint8_t>& data,UserMap& changed,EvmExecution* output=nullptr,
                       size_t nativeBegin=0,vector<NativeExecutionUndo>* nativeUndo=nullptr) {
    if (data.size()<112) return false;
    if (!DBValidateVdfBlock(data)) return false;
    auto num=ReadU64(data.data()+72);
    EvmPayloads payloads;
    if (!ReadEvmPayloads(data,payloads)) return false;
    if (nativeBegin>num||(!payloads.empty()&&nativeBegin)) return false;
    if (nativeUndo) {
        if (payloads.empty()) nativeUndo->resize(size_t(num));
        else nativeUndo->clear();
    }
    if (payloads.empty()) {
        changed.reserve(min(size_t(num*2),users.size()+size_t(num)));
        auto start=GetSteadyTime();
        const auto valid=DBApplyNativeTransactions(data,nativeBegin,size_t(num),changed,nativeUndo);
        userCheckNs+=GetSteadyTime()-start;
        return valid;
    }
    EvmExecution local;
    thread_local uint64_t knownVersion=UINT64_MAX;
    thread_local decltype(evmone::state::BlockInfo{}.known_block_hashes) knownHashes;
    auto& execution=output?*output:local;
    evmone::state::BlockInfo context{};
    context.number=ReadU64(data.data()+64); context.timestamp=context.number;
    context.gas_limit=maxBlockGas; context.base_fee=1;
    if (!payloads.empty()) {
        // 区块时间采用确定的逻辑轮次，所有节点取得相同环境。
        if (branchBatch||knownVersion!=dbStateVersion) {
            knownHashes.clear();
            for (int64_t height=max(int64_t(1),context.number-256);height<context.number;height++) {
                string hash;
                if (DBGet("height/"+to_string(height),hash)&&hash.size()==32) {
                    evmc::bytes32 value; memcpy(value.bytes,hash.data(),32); knownHashes.emplace(height,value);
                }
            }
            knownVersion=branchBatch?UINT64_MAX:dbStateVersion;
        }
        context.known_block_hashes=knownHashes;
    }
    changed.reserve(min(size_t(num*2),users.size()+size_t(num)));
    auto start=GetSteadyTime();
    for (size_t i=0;i<num;i++) {
        auto tx=span<const uint8_t,176>(data.data()+112+i*176,176);
        auto payload=payloads.find(i);
        bool accepted=payload==payloads.end()?ApplyUserTx(users,changed,tx):ExecuteEvmTx(changed,tx,payload->second,users,execution,context);
        if (!accepted) {
            userCheckNs+=GetSteadyTime()-start;
            return false;
        }
    }
    userCheckNs+=GetSteadyTime()-start;
    return true;
}

bool DBReadTxPosition(const array<uint8_t,32>& key,uint64_t& height,array<uint8_t,32>& block,uint32_t& index) {
    string value;
    if (!DBGet(DBHashKey("tx/",key),value)) return false;
    if (value.size()!=12) throw runtime_error("Invalid transaction position");
    auto encoded=reinterpret_cast<const uint8_t*>(value.data());
    height=ReadU64(encoded); index=ReadU32(encoded+8);
    string hash;
    if (!height||!DBGet("height/"+to_string(height),hash)||hash.size()!=32) throw runtime_error("Missing transaction height");
    memcpy(block.data(),hash.data(),32); return true;
}
bool DBReadTx(const array<uint8_t,32>& key,array<uint8_t,176>& tx) {
    string value;
    if (!DBGet(DBHashKey("tx/",key),value)) return false;
    // 原始写入微基准仍可读取完整交易；业务格式 7 保存高度和位置。
    if (value.size()==176) { memcpy(tx.data(),value.data(),176); return true; }
    if (value.size()!=12) throw runtime_error("Invalid transaction position");
    auto encoded=reinterpret_cast<const uint8_t*>(value.data());
    auto height=ReadU64(encoded); auto index=ReadU32(encoded+8);
    string hash;
    if (!height||!DBGet("height/"+to_string(height),hash)||hash.size()!=32) throw runtime_error("Missing indexed height");
    array<uint8_t,32> block; memcpy(block.data(),hash.data(),32);
    string data;
    if (!DBGet(DBHashKey("block/",block),data)||data.size()<112) throw runtime_error("Missing indexed block");
    if (ReadU64(reinterpret_cast<const uint8_t*>(data.data()+64))!=height||memcmp(data.data()+80,block.data(),32)) throw runtime_error("Indexed block height mismatch");
    auto count=ReadU64(reinterpret_cast<const uint8_t*>(data.data()+72));
    if (index>=count||count>(data.size()-112)/176) throw runtime_error("Invalid transaction offset");
    memcpy(tx.data(),data.data()+112+size_t(index)*176,176);
    if (memcmp(tx.data()+80,key.data(),32)) throw runtime_error("Transaction position hash mismatch");
    return true;
}
void DBReadTx(const array<uint8_t,32>& key) {
    array<uint8_t,176> tx{};
    if (DBReadTx(key,tx)) PrintHexForString(string(reinterpret_cast<const char*>(tx.data()),tx.size()));
}
void DBAddBlock(rocksdb::WriteBatch& batch,const array<uint8_t,32>& key,const vector<uint8_t>& value) {
    if (value.size()<112) throw invalid_argument("Short block");
    auto height=ReadU64(value.data()+64);
    batch.Put(DBHashKey("block/",key),rocksdb::Slice(reinterpret_cast<const char*>(value.data()),value.size()));
    batch.Put(DBHashKey("header/",key),rocksdb::Slice(reinterpret_cast<const char*>(value.data()),112));
    batch.Put("height/"+to_string(height),rocksdb::Slice(reinterpret_cast<const char*>(key.data()),key.size()));
}
void DBWriteBlockALL(const array<uint8_t,32>& key,const vector<uint8_t>& value) {
    rocksdb::WriteBatch batch;
    DBAddBlock(batch,key,value);
    DBWriteBatch(batch);
}
array<uint8_t,32> DBReadBlockHashByHeight(uint64_t height) {
    string value; array<uint8_t,32> hash{};
    if (!DBGet("height/"+to_string(height),value)) return hash;
    if (value.size()!=32) throw runtime_error("Invalid height index");
    memcpy(hash.data(),value.data(),32); return hash;
}
vector<uint8_t> DBReadBlockHeader(const array<uint8_t,32>& hash) {
    string value;
    if (!DBGet(DBHashKey("header/",hash),value)) return {};
    if (value.size()!=112||memcmp(value.data()+80,hash.data(),32)) throw runtime_error("Invalid saved block header");
    return {value.begin(),value.end()};
}
vector<uint8_t> DBReadBlockByHash(const array<uint8_t,32>& key) {
    string value;
    if (!DBGet(DBHashKey("block/",key),value)) DBGet(DBHashKey("",key),value);
    return vector<uint8_t>(value.begin(),value.end());
}
vector<uint8_t> DBReadBlockByHeight(const string& height) {
    string value;
    if (!DBGet("height/"+height,value)&&!DBGet(height,value)) return {};
    if (value.size()!=32) throw runtime_error("Invalid height index");
    array<uint8_t,32> hash;
    memcpy(hash.data(),value.data(),32);
    return DBReadBlockByHash(hash);
}
uint64_t DBReadNumber(const string& key) {
    string value;
    if (!DBGet(key,value)) return 0;
    uint64_t number=0;
    auto result=from_chars(value.data(),value.data()+value.size(),number);
    if (result.ec!=errc()||result.ptr!=value.data()+value.size()) throw runtime_error("Invalid DB number: "+key);
    return number;
}
uint64_t DBReadBlockHeight() { return DBReadNumber("BlockHeight"); }
uint64_t DBReadBlockMaxHeight() { return DBReadNumber("BlockMaxHeight"); }
void DBWriteBlockHeight(uint64_t height) { CheckDBStatus(db->Put(rocksdb::WriteOptions(),"BlockHeight",to_string(height))); }
void DBWriteBlockMaxHeight(uint64_t height) { CheckDBStatus(db->Put(rocksdb::WriteOptions(),"BlockMaxHeight",to_string(height))); }
array<uint8_t,32> DBReadCurrentBlock() {
    string value;
    array<uint8_t,32> hash{};
    if (!DBGet("CurrentBlock",value)) return hash;
    if (value.size()!=32) throw runtime_error("Invalid current block hash");
    memcpy(hash.data(),value.data(),32);
    return hash;
}
// These helpers require the caller to hold dbCommitMutex. DBGet also reads the
// branchBatch overlay, so fork replay derives every context from its own parent.
// Contexts are immutable for a chain and a source-epoch tip.
mutex vdfContextMutex;
map<tuple<array<uint8_t,32>,uint64_t,array<uint8_t,32>>,chain::vdf_election::Context> vdfContexts;
deque<tuple<array<uint8_t,32>,uint64_t,array<uint8_t,32>>> vdfContextOrder;
chain::vdf_election::Context DBVdfContext(uint64_t targetEpoch) {
    using namespace chain::vdf_consensus;
    auto genesis=DBReadBlockByHeight("1");
    if (genesis.size()<112) throw runtime_error("Missing VDF genesis block");
    Digest genesisHash{}; memcpy(genesisHash.data(),genesis.data()+80,32);
    Digest sourceHash=genesisHash;
    if (targetEpoch>=2) {
        const auto end=EpochStart(targetEpoch-2)+chain::vdf_consensus::config.epochBlocks-1;
        string encoded;
        if (!DBGet("height/"+to_string(end),encoded)||encoded.size()!=32) throw runtime_error("Missing VDF anchor block");
        memcpy(sourceHash.data(),encoded.data(),32);
    }
    // The last hash commits to all preceding hashes in the source epoch. A
    // fork gets a different key even while replaying through branchBatch.
    const auto key=make_tuple(genesisRoot,targetEpoch,sourceHash);
    {lock_guard lock(vdfContextMutex);if (auto it=vdfContexts.find(key);it!=vdfContexts.end()) return it->second;}
    auto context=ContextForEpoch(chain::vdf_consensus::config,genesisRoot,genesisHash,targetEpoch,[](uint64_t height)->optional<Digest> {
        string encoded;
        if (!DBGet("height/"+to_string(height),encoded)) return nullopt;
        if (encoded.size()!=32) throw runtime_error("Invalid VDF anchor height index");
        Digest hash{}; memcpy(hash.data(),encoded.data(),32); return hash;
    });
    {
        lock_guard lock(vdfContextMutex);
        if (vdfContexts.emplace(key,context).second) {
            vdfContextOrder.push_back(key);
            while (vdfContextOrder.size()>32) {vdfContexts.erase(vdfContextOrder.front());vdfContextOrder.pop_front();}
        }
    }
    return context;
}
shared_ptr<const chain::vdf_consensus::Extension> DBVdfExtension(const array<uint8_t,32>& hash) {
    // Branch replay reads its temporary batch directly. It must neither use nor
    // publish entries from writes which could still be rolled back.
    if (!branchBatch) {
        lock_guard lock(vdfExtensionMutex);
        if (auto found=vdfExtensions.find(hash);found!=vdfExtensions.end()) {
            vdfExtensionCacheHits++; return found->second;
        }
    }
    const auto data=DBReadBlockByHash(hash);
    vdfExtensionLoads++; vdfExtensionReadBytes+=data.size();
    optional<chain::vdf_consensus::Extension> extension;
    if (data.size()<112||memcmp(data.data()+80,hash.data(),hash.size())||
        !ReadVdfExtension(data,extension)||!extension) throw runtime_error("Missing VDF block extension");
    auto result=make_shared<const chain::vdf_consensus::Extension>(std::move(*extension));
    if (!branchBatch) {
        lock_guard lock(vdfExtensionMutex);
        if (auto found=vdfExtensions.find(hash);found!=vdfExtensions.end()) return found->second;
        while (vdfExtensions.size()>=maxVdfExtensions) {
            vdfExtensions.erase(vdfExtensionOrder.front()); vdfExtensionOrder.pop_front();
        }
        vdfExtensions.emplace(hash,result); vdfExtensionOrder.push_back(hash);
    }
    return result;
}
optional<chain::vdf_consensus::Extension> DBVdfParentExtension() {
    if (!chain::vdf_consensus::config.enabled||DBReadBlockHeight()<=1) return nullopt;
    return *DBVdfExtension(DBReadCurrentBlock());
}
uint64_t DBVdfParentSlot() {
    if (!chain::vdf_consensus::config.enabled) return 0;
    const auto parent=DBVdfParentExtension();
    return parent?parent->slot:chain::vdf_consensus::StartSlot()-1;
}
vector<chain::vdf_election::Candidate> DBVdfActiveCandidates(uint64_t height) {
    using namespace chain::vdf_consensus;
    if (!chain::vdf_consensus::config.enabled||height<2) return {};
    const auto epoch=Epoch(height);
    if (epoch<2) return {};
    const auto end=EpochStart(epoch)-1;
    return DBVdfExtension(DBReadBlockHashByHeight(end))->candidates;
}
vector<chain::vdf_election::Candidate> DBVdfCandidatesForNextBlock() {
    using namespace chain::vdf_consensus;
    const auto height=DBReadBlockHeight();
    if (!chain::vdf_consensus::config.enabled||height<2) return {};
    if (height==UINT64_MAX) throw runtime_error("VDF next block height overflow");
    if (Epoch(height)!=Epoch(height+1)) return {};
    return DBVdfExtension(DBReadCurrentBlock())->candidates;
}
optional<chain::vdf_election::Context> DBVdfNextContext(uint64_t height) {
    using namespace chain::vdf_consensus;
    if (!chain::vdf_consensus::config.enabled||height<2) return nullopt;
    const auto epoch=Epoch(height);
    if (!epoch) return nullopt;
    if (epoch==UINT64_MAX) throw runtime_error("VDF target epoch overflow");
    return DBVdfContext(epoch+1);
}

// Callers hold dbCommitMutex. Every lookup uses the current branchBatch view.
// A round's last hash commits to its complete parent path and keys the cache.
array<uint8_t,32> DBWorkAnchor(uint64_t round) {
    using namespace chain::vdf_consensus;
    const auto& settings=chain::vdf_consensus::config;
    const auto start=EpochStart(round,settings);
    if (settings.epochBlocks-1>UINT64_MAX-start) throw runtime_error("Work round height overflow");
    const auto end=start+settings.epochBlocks-1;
    if (end>DBReadBlockHeight()) throw runtime_error("Work round is incomplete");
    auto last=DBReadBlockHashByHeight(end);
    if (last==array<uint8_t,32>{}) throw runtime_error("Missing Work round header");
    {
        lock_guard lock(workAnchorMutex);
        if (auto found=workAnchors.find(last);found!=workAnchors.end()) { workAnchorCacheHits++; return found->second; }
    }
    vector<uint8_t> bytes(16+settings.epochBlocks*32);
    detail::Write64(bytes.data(),round);
    detail::Write64(bytes.data()+8,settings.epochBlocks);
    for (uint64_t i=0;i<settings.epochBlocks;++i) {
        const auto hash=DBReadBlockHashByHeight(start+i);
        if (hash==array<uint8_t,32>{}) throw runtime_error("Missing Work round header");
        copy(hash.begin(),hash.end(),bytes.begin()+16+i*32);
    }
    const auto anchor=chain::vdf_election::detail::Hash("Chain/VdfConsensus/Anchor/v1",{genesisRoot,bytes});
    {
        lock_guard lock(workAnchorMutex);
        if (workAnchors.emplace(last,anchor).second) {
            workAnchorOrder.push_back(last);
            while (workAnchorOrder.size()>16384) {workAnchors.erase(workAnchorOrder.front());workAnchorOrder.pop_front();}
        }
    }
    return anchor;
}
void DBRememberWorkState(const array<uint8_t,32>& hash,shared_ptr<const chain::work_consensus::State> state) {
    // The complete block hash binds its parent history and election records.
    // This immutable consensus state contains no mutable account/database data.
    lock_guard lock(workStateMutex);
    if (workStates.emplace(hash,std::move(state)).second) {
        workStateOrder.push_back(hash);
        while (workStateOrder.size()>512) {workStates.erase(workStateOrder.front());workStateOrder.pop_front();}
    }
}
vector<array<uint8_t,32>> DBWorkCommittee(uint64_t epoch) {
    const auto& settings=chain::vdf_consensus::config;
    if (!epoch) return settings.bootstrapProducers;
    const auto start=chain::vdf_consensus::EpochStart(epoch-1,settings);
    const auto end=chain::vdf_consensus::EpochStart(epoch,settings)-1;
    for (auto height=end;;--height) {
        const auto extension=DBVdfExtension(DBReadBlockHashByHeight(height));
        chain::work_consensus::Delta delta;
        if (!extension->work||!chain::work_consensus::DecodeDelta(*extension->work,delta))
            throw runtime_error("Missing Work election history");
        if (delta.finalization) return delta.finalization->nextCommittee;
        if (height==start) break;
    }
    throw runtime_error("Missing previous Work election result");
}
shared_ptr<const chain::work_consensus::State> DBWorkState() {
    namespace rules=chain::work_consensus;
    const auto& settings=chain::vdf_consensus::config;
    const auto height=DBReadBlockHeight();
    if (!settings.workElection||height<2) return {};
    const auto hash=DBReadCurrentBlock();
    const auto cached=[&](const array<uint8_t,32>& key)->shared_ptr<const rules::State> {
        lock_guard lock(workStateMutex);
        if (auto found=workStates.find(key);found!=workStates.end()) {workStateCacheHits++;return found->second;}
        return {};
    };
    if (auto state=cached(hash)) return state;
    const auto epoch=chain::vdf_consensus::Epoch(height,settings);
    const auto start=chain::vdf_consensus::EpochStart(epoch,settings);
    vector<pair<array<uint8_t,32>,shared_ptr<const chain::vdf_consensus::Extension>>> replay;
    shared_ptr<const rules::State> state;
    for (auto h=height;;--h) {
        const auto blockHash=DBReadBlockHashByHeight(h);
        if ((state=cached(blockHash))) break;
        replay.emplace_back(blockHash,DBVdfExtension(blockHash));
        if (h==start) break;
    }
    if (!state) {
        const auto anchor=DBReadBlockHashByHeight(start-1);
        const auto parentSlot=start==2?chain::vdf_consensus::StartSlot(settings)-1:DBVdfExtension(anchor)->slot;
        state=make_shared<const rules::State>(rules::Initialize(settings,genesisRoot,epoch,
            replay.back().second->slot,anchor,DBWorkCommittee(epoch),parentSlot));
    }
    const chain::work_election::AnchorLookup lookup=[](uint64_t round,array<uint8_t,32>& anchor) {
        try {anchor=DBWorkAnchor(round);return true;} catch (...) {return false;}
    };
    for (auto it=replay.rbegin();it!=replay.rend();++it) {
        const auto& extension=*it->second;
        rules::Delta delta;
        if (!extension.work||!extension.turn||!extension.candidates.empty()||extension.producerProof||
            !rules::DecodeDelta(*extension.work,delta)) throw runtime_error("Invalid stored Work extension");
        state=make_shared<const rules::State>(rules::Advance(*state,delta,extension.slot,
            extension.producer,*extension.turn,lookup,settings));
        workStateReplays++;
        DBRememberWorkState(it->first,state);
    }
    return state;
}
chain::work_consensus::State DBWorkStateForNextBlock(uint64_t height,uint64_t slot) {
    namespace rules=chain::work_consensus;
    const auto& settings=chain::vdf_consensus::config;
    if (!settings.workElection||height<2||DBReadBlockHeight()==UINT64_MAX||height!=DBReadBlockHeight()+1)
        throw runtime_error("Invalid Work block height");
    if (height==2) return rules::Initialize(settings,genesisRoot,0,slot,DBReadCurrentBlock(),
        settings.bootstrapProducers,chain::vdf_consensus::StartSlot(settings)-1);
    const auto parent=DBWorkState();
    if (!parent) throw runtime_error("Missing Work parent state");
    if (chain::vdf_consensus::Epoch(height,settings)!=parent->epoch)
        return rules::ForNextEpoch(*parent,slot,DBReadCurrentBlock(),settings);
    return *parent;
}
// Generic block encoding, transaction signatures and the bound Merkle root are
// checked before this function. Eligibility is checked again for this parent
// before either a cached execution or fork replay can commit the block.
bool DBValidateVdfBlock(const vector<uint8_t>& data,bool committing) {
    using namespace chain::vdf_consensus;
    if (data.size()<112) return false;
    optional<Extension> extension;
    if (!ReadVdfExtension(data,extension)) return false;
    const auto height=ReadU64(data.data()+64);
    if (!chain::vdf_consensus::config.enabled||height==1) return !extension;
    if (!extension) return false;
    if (committing&&chain::vdf_consensus::config.producerRotation&&extension->slot>CurrentSlot()) return false;
    const auto parentHeight=DBReadBlockHeight();
    const auto parentHash=DBReadCurrentBlock();
    if (parentHeight==UINT64_MAX||height!=parentHeight+1||memcmp(data.data(),parentHash.data(),32)) return false;
    try {
        Digest hash{}; memcpy(hash.data(),data.data()+80,32);
        if (!VerifySignature(*extension,hash)) return false;
        if (chain::vdf_consensus::config.workElection) {
            if (!extension->work||!extension->turn||extension->producerProof||!extension->candidates.empty()||
                extension->slot<MinimumSlot(height)) return false;
            const auto now=CurrentSlot();
            if (extension->slot>now&&extension->slot-now>chain::vdf_consensus::config.futureSlots) return false;
            chain::work_consensus::Delta delta;
            if (!chain::work_consensus::DecodeDelta(*extension->work,delta)) return false;
            const auto parent=DBWorkStateForNextBlock(height,extension->slot);
            const chain::work_election::AnchorLookup lookup=[](uint64_t round,Digest& anchor) {
                try {anchor=DBWorkAnchor(round);return true;} catch (...) {return false;}
            };
            const auto state=chain::work_consensus::Advance(parent,delta,extension->slot,
                extension->producer,*extension->turn,lookup,chain::vdf_consensus::config);
            DBRememberWorkState(hash,make_shared<const chain::work_consensus::State>(state));
            return true;
        }
        if (extension->work) return false;
        const auto current=DBVdfContext(Epoch(height));
        const auto next=DBVdfNextContext(height);
        const auto previousCandidates=DBVdfCandidatesForNextBlock();
        const auto activeCandidates=DBVdfActiveCandidates(height);
        const auto parent=DBVdfParentExtension();
        const auto parentSlot=parent?parent->slot:StartSlot()-1;
        const bool sameEpoch=parentHeight>=2&&Epoch(parentHeight)==Epoch(height);
        return VerifyRules(*extension,parentSlot,previousCandidates,activeCandidates,current,next,MinimumSlot(height),
            chain::vdf_consensus::config,parent?parent->turn:nullopt,sameEpoch);
    } catch (...) {
        return false;
    }
}
// 调用方持有提交锁，在原子写入和账户发布完成后更新。
void PublishChainHead() {
    ChainHead head{DBReadBlockHeight(),DBReadNumber("ChainTx"),dbStateVersion,userBurned,users.size(),evmState.get_accounts().size(),DBReadCurrentBlock()};
    lock_guard lock(chainHeadMutex); publishedHead=head;
}
void DBWriteCurrentBlock(const array<uint8_t,32>& hash) {
    CheckDBStatus(db->Put(rocksdb::WriteOptions(),"CurrentBlock",rocksdb::Slice(reinterpret_cast<const char*>(hash.data()),32)));
}
enum class CandidateCheckResult { Invalid,Valid,Superseded };
bool DBCachedExecutionMatches(const CachedExecution& cached,const array<uint8_t,32>& parent,uint64_t height) {
    return cached.version==dbStateVersion&&cached.parent==parent&&cached.height==height&&
        cached.genesis==genesisRoot&&cached.fee==baseFee&&cached.supply==genesisSupply&&cached.burned==userBurned;
}
// The caller has already checked the complete block's encoding, signatures and Merkle root.
// A ranked winner can avoid execution only while its complete successful execution
// is still cached for this exact parent state. Cache eviction only loses the shortcut.
CandidateCheckResult DBPrepareBlockUsers(const vector<uint8_t>& data,const vector<uint8_t>* rankedWinner=nullptr,bool* fullyChecked=nullptr) {
    if (fullyChecked) *fullyChecked=false;
    if (data.size()<112) return CandidateCheckResult::Invalid;
    auto waitStart=GetSteadyTime();
    shared_lock lock(dbCommitMutex);
    candidateLockWaitNs+=GetSteadyTime()-waitStart;
    auto current=DBReadBlockHeight();
    auto height=ReadU64(data.data()+64);
    // 未来高度先缓存内容；轮到提交时，使用它实际父块的状态完整执行。
    if (current<UINT64_MAX&&height>current+1) return CandidateCheckResult::Valid;
    auto previous=DBReadCurrentBlock();
    if (current==UINT64_MAX||height!=current+1||memcmp(data.data(),previous.data(),32)!=0) return CandidateCheckResult::Invalid;
    if (!DBValidateVdfBlock(data)) return CandidateCheckResult::Invalid;
    EvmPayloads payloads;
    if (!ReadEvmPayloads(data,payloads)) return CandidateCheckResult::Invalid;
    auto result=make_unique<CachedExecution>();
    size_t prefix=0;
    auto start=GetSteadyTime();
    {
        lock_guard cacheLock(executionMutex);
        for (const auto& cached:executionCache) {
            if (!DBCachedExecutionMatches(*cached,previous,height)) continue;
            if (cached->data==data) {
                candidateExecutionCacheHits++;
                if (fullyChecked) *fullyChecked=true;
                return CandidateCheckResult::Valid;
            }
        }
        if (rankedWinner&&rankedWinner->size()>=112&&ReadU64(rankedWinner->data()+64)==height&&
            memcmp(rankedWinner->data(),previous.data(),32)==0) {
            auto winnerCount=ReadU64(rankedWinner->data()+72),count=ReadU64(data.data()+72);
            bool loses=count<winnerCount||(count==winnerCount&&memcmp(data.data()+80,rankedWinner->data()+80,32)>=0);
            if (loses) for (const auto& cached:executionCache) {
                if (DBCachedExecutionMatches(*cached,previous,height)&&cached->data==*rankedWinner) {
                    candidateRankSkips++;
                    return CandidateCheckResult::Superseded;
                }
            }
        }
        if (executionPrefixReuse&&payloads.empty()) {
            const CachedExecution* source=nullptr;
            for (const auto& cached:executionCache) {
                if (!DBCachedExecutionMatches(*cached,previous,height)||cached->data.size()<112||cached->evm.active) continue;
                const auto oldCount=size_t(ReadU64(cached->data.data()+72));
                if (!oldCount||cached->nativeUndo.size()!=oldCount) continue;
                const auto limit=min(oldCount,size_t(ReadU64(data.data()+72)));
                size_t common=0;
                while (common<limit&&!memcmp(data.data()+112+common*176,cached->data.data()+112+common*176,176)) common++;
                // Copying and rewinding a long divergent suffix costs more than
                // executing a short prefix. Reuse only the larger shared part.
                if (common>prefix&&common>=oldCount-common) { prefix=common; source=cached.get(); }
            }
            if (source) {
                result->users=source->users;
                DBRewindNativeExecution(source->data,source->nativeUndo,prefix,result->users);
                result->nativeUndo.assign(source->nativeUndo.begin(),source->nativeUndo.begin()+prefix);
            }
        }
    }
    result->data=data; result->parent=previous;
    result->version=dbStateVersion; result->height=height;
    result->genesis=genesisRoot; result->fee=baseFee; result->supply=genesisSupply; result->burned=userBurned;
    candidateExecutions++;
    if (!DBApplyBlockUsers(data,result->users,&result->evm,prefix,executionPrefixReuse?&result->nativeUndo:nullptr)) return CandidateCheckResult::Invalid;
    if (prefix) { executionPrefixHits++; executionPrefixTransactions+=prefix; }
    candidateExecuteNs+=GetSteadyTime()-start;
    result->bytes=data.size()+result->users.size()*96+result->nativeUndo.capacity()*sizeof(NativeExecutionUndo);
    for (const auto& [address,account]:result->evm.state.get_accounts()) result->bytes+=256+account.code.size()+account.storage.size()*128;
    for (const auto& [hash,receipt]:result->evm.receipts) result->bytes+=96+receipt.size();
    if (result->bytes<=maxExecutionBytes) {
        lock_guard cacheLock(executionMutex);
        for (auto it=executionCache.begin();it!=executionCache.end();) {
            if ((*it)->data==data) { executionBytes-=(*it)->bytes; it=executionCache.erase(it); } else ++it;
        }
        while (!executionCache.empty()&&(executionCache.size()>=8||executionBytes+result->bytes>maxExecutionBytes)) {
            executionBytes-=executionCache.front()->bytes; executionCache.pop_front();
        }
        executionBytes+=result->bytes; executionCache.push_back(std::move(result));
    }
    if (fullyChecked) *fullyChecked=true;
    return CandidateCheckResult::Valid;
}
bool DBCheckBlockUsers(const vector<uint8_t>& data) {
    return DBPrepareBlockUsers(data)==CandidateCheckResult::Valid;
}
// 一块的块体、交易、索引、当前高度和链头在同一个 batch 中提交。
bool DBCommitBlockLocked(const array<uint8_t,32>& hash,const vector<uint8_t>& data) {
    if (data.size()<112) return false;
    auto height=ReadU64(data.data()+64);
    auto num=ReadU64(data.data()+72);
    EvmPayloads payloads;
    if (!ReadEvmPayloads(data,payloads)) return false;
    auto currentHeight=DBReadBlockHeight();
    auto previousHash=DBReadCurrentBlock();
    if (currentHeight==UINT64_MAX||height!=currentHeight+1||memcmp(data.data(),previousHash.data(),32)!=0) return false;
    if (!DBValidateVdfBlock(data,true)) return false;
    UserMap changed;
    EvmExecution execution;
    unique_ptr<CachedExecution> cached;
    {
        lock_guard cacheLock(executionMutex);
        for (auto it=executionCache.begin();it!=executionCache.end();++it) {
            if (DBCachedExecutionMatches(**it,previousHash,height)&&(*it)->data==data) {
                executionBytes-=(*it)->bytes; cached=std::move(*it); executionCache.erase(it); break;
            }
        }
    }
    if (cached) { changed=std::move(cached->users); execution=std::move(cached->evm); executionCacheHits++; }
    else {
        executionCacheMisses++; auto start=GetSteadyTime();
        auto valid=DBApplyBlockUsers(data,changed,&execution); commitExecuteNs+=GetSteadyTime()-start;
        if (!valid) { invalidUserBlocks++; return false; }
    }
    if (baseFee&&num>(genesisSupply-userBurned)/baseFee) return false;
    auto burned=userBurned+num*baseFee;
    if (execution.gasUsed>genesisSupply-burned) return false;
    burned+=execution.gasUsed;
    auto prepareStart=GetSteadyTime();
    // 写盘前准备哈希桶；提交后直接转移新增账户节点，减少状态发布时的分配。
    if (users.size()+changed.size()>users.bucket_count()*users.max_load_factor()) users.reserve(max(users.size()*2,users.size()+changed.size()));
    rocksdb::WriteBatch batch;
    for (size_t i=0;i<num;i++) {
        auto tx=data.data()+112+i*176;
        array<uint8_t,32> txHash;
        memcpy(txHash.data(),tx+80,32);
        // 连续账户序号已排除历史重放和块内重复，直接写交易索引。
        array<uint8_t,12> position; WriteU64(position.data(),height); WriteU32(position.data()+8,uint32_t(i));
        batch.Put(DBHashKey("tx/",txHash),rocksdb::Slice(reinterpret_cast<const char*>(position.data()),position.size()));
        if (auto found=payloads.find(i);found!=payloads.end()) batch.Put(DBHashKey("input/",txHash),rocksdb::Slice(reinterpret_cast<const char*>(found->second.data()),found->second.size()));
    }
    for (const auto& [key,user]:changed) DBAddUser(batch,key,user);
    uint64_t accountWrites=0,slotWrites=0; auto persistStart=GetSteadyTime();
    if (execution.active) {
        for (const auto& address:execution.state.GetCleared()) {
            auto old=evmState.get_accounts().find(address);
            if (old!=evmState.get_accounts().end()) {
                for (const auto& [slot,value]:old->second.storage) if (value.current!=evmc::bytes32{}) {
                    batch.Delete(DBEvmSlotKey(address,slot)); slotWrites++;
                }
            }
        }
        for (const auto& address:execution.state.GetDeleted()) {
            batch.Delete(DBEvmKey("evm/",address)); batch.Delete(DBEvmKey("evm-code/",address)); accountWrites++;
        }
        for (const auto& [address,account]:execution.state.get_accounts()) {
            auto old=evmState.get_accounts().find(address);
            bool fresh=old==evmState.get_accounts().end(),cleared=execution.state.GetCleared().count(address);
            if (fresh||old->second.balance!=account.balance||old->second.nonce!=account.nonce) {
                batch.Put(DBEvmKey("evm/",address),SerializeEvmHeader(account)); accountWrites++;
            }
            if (fresh||old->second.code!=account.code) {
                auto key=DBEvmKey("evm-code/",address);
                if (account.code.empty()) batch.Delete(key);
                else batch.Put(key,rocksdb::Slice(reinterpret_cast<const char*>(account.code.data()),account.code.size()));
            }
            for (const auto& [slot,value]:account.storage) {
                auto previous=cleared?evmc::bytes32{}:evmState.ReadStorage(address,slot);
                if (previous==value.current) continue;
                auto key=DBEvmSlotKey(address,slot);
                if (value.current==evmc::bytes32{}) batch.Delete(key);
                else batch.Put(key,rocksdb::Slice(reinterpret_cast<const char*>(value.current.bytes),32));
                slotWrites++;
            }
        }
        for (const auto& [key,value]:execution.receipts) batch.Put(DBHashKey("receipt/",key),value);
    }
    evmPersistNs+=GetSteadyTime()-persistStart;
    batch.Put("UserBurned",to_string(burned));
    batch.Put("CurrentBlock",rocksdb::Slice(reinterpret_cast<const char*>(hash.data()),32));
    batch.Put("BlockHeight",to_string(height));
    batch.Put("height/"+to_string(height),rocksdb::Slice(reinterpret_cast<const char*>(hash.data()),32));
    auto total=DBReadNumber("ChainTx");
    if (UINT64_MAX-total<num) return false;
    batch.Put("ChainTx",to_string(total+num));
    // 记录本次改写的旧值，回滚与新分支重放共用一个原子写批次。
    struct Undo:rocksdb::WriteBatch::Handler {
        rocksdb::WriteBatch saved;
        void Save(const rocksdb::Slice& key) {
            // 已连续执行的账户序号保证这些索引是新键；直接记录删除即可。
            if (key.starts_with("tx/")||key.starts_with("txheight/")||key.starts_with("input/")||key.starts_with("receipt/")||key.starts_with("height/")) { saved.Delete(key); return; }
            if (key.starts_with("user/")&&key.size()==37) {
                array<uint8_t,32> address; memcpy(address.data(),key.data()+5,32);
                auto found=users.find(address);
                if (found==users.end()) saved.Delete(key); else DBAddUser(saved,address,found->second);
                return;
            }
            string value;
            if (DBGet(key.ToString(),value)) saved.Put(key,value); else saved.Delete(key);
        }
        void Put(const rocksdb::Slice& key,const rocksdb::Slice&) override { Save(key); }
        void Delete(const rocksdb::Slice& key) override { Save(key); }
    } undo;
    CheckDBStatus(batch.Iterate(&undo));
    batch.Put(DBHashKey("undo/",hash),undo.saved.Data());
    batch.Put(DBHashKey("score/",hash),to_string(total+num));
    batch.Put(DBHashKey("block/",hash),rocksdb::Slice(reinterpret_cast<const char*>(data.data()),data.size()));
    batch.Put(DBHashKey("header/",hash),rocksdb::Slice(reinterpret_cast<const char*>(data.data()),112));
    SaveBranchMemory(batch);
    dbPrepareNs+=GetSteadyTime()-prepareStart;
    DBWriteBatch(batch);
    if (!branchBatch) PublishPoolUsers(&changed);
    auto writeCount=changed.size();
    for (const auto& [key,user]:changed) {
        auto found=users.find(key);
        if (found!=users.end()) found->second=user;
    }
    users.merge(changed);
    userBurned=burned;
    if (execution.active) ApplyEvmChanges(execution.state);
    evmAccountWrites+=accountWrites; evmSlotWrites+=slotWrites;
    InvalidateExecutionCache();
    userWriteCount+=writeCount;
    if (!branchBatch) PublishChainHead();
    return true;
}
bool DBCommitBlock(const array<uint8_t,32>& hash,const vector<uint8_t>& data) {
    auto waitStart=GetSteadyTime();
    lock_guard lock(dbCommitMutex);
    commitLockWaitNs+=GetSteadyTime()-waitStart;
    return DBCommitBlockLocked(hash,data);
}
#endif
