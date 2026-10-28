#pragma once
#include "Support.h"

string Word(uint64_t value) {
    ostringstream s;
    s << hex << setw(64) << setfill('0') << value;
    return s.str();
}
uint64_t AbiNumber(const vector<uint8_t> &bytes, size_t offset) {
    if (offset > bytes.size() || bytes.size() - offset < 32)
        throw runtime_error("Invalid ABI word");
    for (size_t i = 0; i < 24; i++)
        if (bytes[offset + i])
            throw runtime_error("ABI value out of range");
    uint64_t value = 0;
    for (size_t i = 24; i < 32; i++)
        value = (value << 8) | bytes[offset + i];
    return value;
}
string AbiString(const vector<uint8_t> &bytes, size_t word) {
    auto offset = AbiNumber(bytes, word * 32), length = AbiNumber(bytes, size_t(offset));
    if (offset > bytes.size() - 32 || length > bytes.size() - offset - 32 || length > 4096)
        throw runtime_error("Invalid ABI string");
    return string(reinterpret_cast<const char *>(bytes.data() + offset + 32), size_t(length));
}
string EncodeString(const string &value) {
    auto hex = EvmHex({reinterpret_cast<const uint8_t *>(value.data()), value.size()});
    hex.resize((hex.size() + 63) / 64 * 64, '0');
    return Word(value.size()) + hex;
}
string Topic(const string &text) {
    auto hash = ethash::keccak256(reinterpret_cast<const uint8_t *>(text.data()), text.size());
    return EvmHex({hash.bytes, 32});
}
string Sha256(const string &text) {
    array<uint8_t, 32> hash;
    crypto_hash_sha256(hash.data(), reinterpret_cast<const uint8_t *>(text.data()), text.size());
    return EvmHex(hash);
}

class ClassroomApp {
  public:
    fs::path root, binary, artifactPath;
    Json artifact, profile, cache = Json::object();
    Vault vault;
    Database index;
    vector<unique_ptr<Child>> children;
    mutex guard;
    string active = "alice", nickname = "同学";
    bool ipv6 = HasIPv6();
    string lastError;
    uint64_t reorgs = 0;
    int64_t lastSampleMs = 0;
    uint64_t lastHeight = 0, lastTxs = 0;
    ClassroomApp(fs::path directory, fs::path executable, fs::path contract)
        : root(std::move(directory)), binary(std::move(executable)), artifactPath(std::move(contract)) {
        fs::create_directories(root / "networks");
        vault.path = root / "wallet.vault.json";
        artifact = ReadJson(artifactPath).at("contracts").at("ClassroomChat");
        if (fs::exists(root / "session.json")) {
            auto saved = ReadJson(root / "session.json");
            auto id = saved.at("id").get<string>();
            if (id.size() != 24 || id.find_first_not_of("0123456789abcdef") != string::npos)
                throw runtime_error("Invalid saved network ID");
            profile = ReadJson(root / "networks" / id / "profile.json");
            if (profile.at("id").get<string>() != id)
                throw runtime_error("Saved network path mismatch");
            active = saved.value("active", string("alice"));
            nickname = saved.value("nickname", string("同学"));
            index.Open(NetworkDir() / "index.sqlite");
        }
    }
    ~ClassroomApp() {
        Stop();
    }
    fs::path NetworkDir() const {
        return root / "networks" / profile.at("id").get<string>();
    }
    string Host() const {
        return "127.0.0.1";
    }
    uint16_t Port() const {
        return profile.at("ports").at(0).get<uint16_t>();
    }
    Json NodeQuery(const Json &request) {
        return ClassroomRpc(Host(), Port(), request);
    }
    string Method(const string &signature) {
        return artifact.at("evm").at("methodIdentifiers").at(signature);
    }
    string Runtime() {
        return artifact.at("evm").at("deployedBytecode").at("object");
    }
    void Persist() {
        SaveJson(NetworkDir() / "profile.json", profile);
        SaveJson(root / "session.json", {{"id", profile["id"]}, {"active", active}, {"nickname", nickname}});
    }
    Json PublicWallets() {
        Json wallets = Json::array();
        if (!vault.Unlocked())
            return wallets;
        for (auto name : {"alice", "bob", "teacher"}) {
            auto key = vault.Key(name);
            auto evm = GetEvmAddress(key.public_key);
            wallets.push_back(
                {{"id", name}, {"public_key", U32ToHex(key.public_key)}, {"evm_address", EvmHex(evm.bytes)}});
            sodium_memzero(key.private_key.data(), key.private_key.size());
        }
        return wallets;
    }
    void Stop() {
        children.clear();
        cache["running"] = false;
        cache["contract_ready"] = false;
        cache["recent_tps"] = 0;
        cache["recent_block_ms"] = nullptr;
        if (cache.contains("nodes"))
            for (auto &node : cache["nodes"]) {
                node["online"] = false;
                node["peers"] = 0;
                node["connections"] = Json::array();
            }
    }
    void Start() {
        if (!profile.is_object())
            throw runtime_error("请先创建或加入课堂");
        Stop();
        SetGenesisUsers(profile.at("genesis"));
        auto directory = NetworkDir();
        SaveJson(directory / "genesis.json", profile["genesis"]);
        auto ports = profile.at("ports");
        auto demo = profile.value("mode", string{}) == "demo";
        try {
            for (size_t i = 0; i < ports.size(); i++) {
                auto node = directory / ("node" + to_string(i + 1));
                fs::create_directories(node);
                vector<string> args = {"--data",
                                       PathUtf8((node / "db")),
                                       "--genesis",
                                       PathUtf8((directory / "genesis.json")),
                                       "--metrics",
                                       PathUtf8((node / "final.json")),
                                       "--stop-file",
                                       PathUtf8((node / "stop.request")),
                                       "--port",
                                       to_string(ports[i].get<unsigned>()),
                                       "--bind",
                                       demo ? "127.0.0.1" : "0.0.0.0",
                                       "--io-threads",
                                       "2",
                                       "--verify-threads",
                                       "2",
                                       "--candidate-threads",
                                       "1",
                                       "--block-verify-threads",
                                       "1",
                                       "--sync",
                                       "1",
                                       "--relay-transactions",
                                       "1",
                                       "--peer-protocol",
                                       "2",
                                       "--clock-sync",
                                       "1",
                                       "--round-ms",
                                       "50",
                                       "--candidate-ms",
                                       "12"};
                if (ipv6) {
                    args.push_back("--bind-v6");
                    args.push_back(demo ? "::1" : "::");
                }
                for (size_t j = 0; j < i; j++) {
                    args.push_back("--seed");
                    args.push_back("127.0.0.1:" + to_string(ports[j].get<unsigned>()));
                }
                for (const auto &seed : profile.value("seeds", Json::array())) {
                    args.push_back("--seed");
                    args.push_back(seed.get<string>());
                }
                children.push_back(make_unique<Child>(binary, args, node, node / "stop.request"));
            }
            auto deadline = Clock::now() + std::chrono::seconds(15);
            for (const auto &port : ports) {
                while (true) {
                    try {
                        auto stats = Query(Host(), port.get<uint16_t>(), 13);
                        if (stats.value("failed", false))
                            throw runtime_error("Node failed");
                        break;
                    } catch (...) {
                        if (Clock::now() > deadline)
                            throw runtime_error("节点未能启动，请检查端口占用与诊断信息");
                        this_thread::sleep_for(std::chrono::milliseconds(100));
                    }
                }
            }
            cache = Json::object();
            cache["running"] = true;
            lastError.clear();
            lastSampleMs = 0;
            Tick();
        } catch (...) {
            Stop();
            throw;
        }
    }
    bool ContractPresent() {
        if (!profile.contains("contract") || profile["contract"].get<string>().empty())
            return false;
        auto code = NodeQuery({{"method", "code"}, {"to", profile["contract"]}}).at("code").get<string>();
        if (code.empty())
            return false;
        if (code != Runtime())
            throw runtime_error("聊天室代码与随附版本不一致");
        return true;
    }
    void NewNetwork(const Json &request, bool joining) {
        if (!vault.Unlocked())
            throw runtime_error("请先创建或解锁钱包");
        Stop();
        active = "alice";
        Json next = {{"id", RandomHex(12)},
                     {"mode", joining ? "student" : request.value("mode", string("demo"))},
                     {"title", request.value("title", string("区块链课堂"))},
                     {"teacher", !joining},
                     {"contract", ""},
                     {"seeds", Json::array()},
                     {"grants", Json::object()}};
        if (next["title"].get<string>().size() > 160)
            throw invalid_argument("课堂名称过长");
        if (joining) {
            auto invite = request.at("invite");
            if (invite.is_string())
                invite = Json::parse(invite.get<string>());
            if (invite.at("version") != 1 || invite.at("contract_code_sha256") != Sha256(Runtime()))
                throw invalid_argument("课堂版本不兼容");
            next["title"] = invite.at("title");
            next["genesis"] = invite.at("genesis");
            SetGenesisUsers(next["genesis"]);
            if (U32ToHex(genesisRoot) != invite.at("genesis_root").get<string>())
                throw invalid_argument("创世指纹不匹配");
            auto address = EvmBytes(invite.at("contract"));
            if (address.size() != 20)
                throw invalid_argument("Invalid classroom contract");
            next["contract"] = EvmHex(address);
            next["genesis_root"] = invite["genesis_root"];
            auto seeds = invite.at("seeds");
            if (!seeds.is_array() || seeds.empty() || seeds.size() > 8)
                throw invalid_argument("课堂需要 1 至 8 个入口");
            for (const auto &seed : seeds)
                ParseEndpoint(seed.get<string>());
            next["seeds"] = seeds;
        } else {
            auto accounts = Json::array();
            for (const auto &wallet : PublicWallets())
                accounts.push_back({{"public_key", wallet["public_key"]}, {"balance", 1000000000000ULL}});
            auto anchor = GenerateWallet();
            accounts.push_back({{"public_key", U32ToHex(anchor.public_key)}, {"balance", 0}});
            sodium_memzero(anchor.private_key.data(), 64);
            next["genesis"] = {{"base_fee", 1}, {"accounts", accounts}};
            SetGenesisUsers(next["genesis"]);
            next["genesis_root"] = U32ToHex(genesisRoot);
        }
        io_context io;
        vector<unique_ptr<tcp::acceptor>> probes;
        next["ports"] = Json::array();
        for (int i = 0; i < (joining ? 1 : 3); i++) {
            auto a = make_unique<tcp::acceptor>(io, tcp::endpoint(ip::address_v4::loopback(), 0));
            next["ports"].push_back(a->local_endpoint().port());
            probes.push_back(std::move(a));
        }
        probes.clear();
        profile = next;
        fs::create_directories(NetworkDir());
        index.Open(NetworkDir() / "index.sqlite");
        Persist();
        Start();
        if (!joining) {
            auto code = EvmBytes(artifact.at("evm").at("bytecode").at("object"));
            auto sent = Submit("teacher", "deploy", {}, code, 0, 2000000, Json::object());
            auto hash = sent.at("hash").get<string>();
            profile["deploy_tx"] = hash;
            Persist();
            auto deadline = Clock::now() + std::chrono::seconds(20);
            Json receipt;
            do {
                receipt = Query(Host(), Port(), 23, EvmBytes(hash));
                if (!receipt.is_null())
                    break;
                this_thread::sleep_for(std::chrono::milliseconds(100));
            } while (Clock::now() < deadline);
            if (receipt.is_null() || receipt.at("status") != 0)
                throw runtime_error("聊天室部署尚未成功，请保留诊断信息并重试启动");
            profile["contract"] = receipt.at("contract");
            Persist();
            if (!ContractPresent())
                throw runtime_error("聊天室代码未同步");
        }
        Tick();
    }
    Json Submit(const string &walletName, const string &kind, const vector<uint8_t> &target,
                const vector<uint8_t> &input, uint64_t amount, uint64_t gas, Json extra) {
        if (children.empty())
            throw runtime_error("请先启动节点");
        SetGenesisUsers(profile.at("genesis"));
        auto key = vault.Key(walletName);
        struct Wipe {
            Wallet &key;
            ~Wipe() {
                sodium_memzero(key.private_key.data(), key.private_key.size());
            }
        } wipe{key};
        auto publicKey = U32ToHex(key.public_key);
        auto account = Account(Host(), Port(), publicKey);
        auto nonce = stoull(account["nonce"].get<string>()), balance = stoull(account["balance"].get<string>());
        if (nonce == UINT64_MAX || amount > balance || baseFee > balance - amount || gas > balance - amount - baseFee)
            throw runtime_error("余额不足以支付金额和费用，请向教师领取测试币");
        auto head = Rpc(Host(), Port(), 19);
        if (head.size() != 80 || memcmp(head.data(), genesisRoot.data(), 32))
            throw runtime_error("节点创世指纹发生变化");
        for (const auto &item : index.Rows("SELECT data FROM outbox"))
            if (item.at("sender") == publicKey && stoull(item.at("nonce").get<string>()) > nonce)
                throw runtime_error("上一笔交易仍等待入块，请稍后发送或重试原交易");
        vector<uint8_t> bytes;
        uint8_t type;
        if (kind == "transfer") {
            if (target.size() != 32 || !amount)
                throw invalid_argument("收款公钥或金额无效");
            array<uint8_t, 32> receiver;
            memcpy(receiver.data(), target.data(), 32);
            auto tx = GenerateTx(key.public_key, receiver, amount, nonce + 1, key);
            bytes.assign(tx.begin(), tx.end());
            type = 1;
        } else {
            bytes = MakeEvmTx(key, target, input, nonce + 1, amount, gas, kind == "deploy");
            type = 17;
        }
        auto hash = EvmHex({bytes.data() + 80, 32});
        extra.update({{"hash", hash},
                      {"sender", publicKey},
                      {"nonce", to_string(nonce + 1)},
                      {"kind", kind},
                      {"state", "submitted"},
                      {"raw", EvmHex(bytes)},
                      {"type", type},
                      {"submitted_ms", NowMs()},
                      {"last_sent_ms", NowMs()}});
        index.Put("outbox", hash, extra); // Save before network I/O: retries always reuse the identical signed bytes.
        Rpc(Host(), Port(), type, bytes, false);
        return {{"hash", hash}, {"state", "submitted"}};
    }
    Json DecodeEvents(const Json &tx, uint64_t height, const string &block) {
        Json events = Json::array();
        if (!tx.contains("receipt") || tx["receipt"].at("status") != 0 || !profile.contains("contract"))
            return events;
        size_t i = 0;
        for (const auto &log : tx["receipt"].at("logs")) {
            auto logIndex = i++;
            if (log.at("address") != profile["contract"] || log.at("topics").empty())
                continue;
            auto topic = log["topics"][0].get<string>();
            auto data = EvmBytes(log.at("data"));
            Json event = {{"id", tx.at("hash").get<string>() + "-" + to_string(logIndex)},
                          {"tx_hash", tx["hash"]},
                          {"block_hash", block},
                          {"height", height},
                          {"tx_index", tx["index"]},
                          {"log_index", logIndex},
                          {"observed_ms", NowMs()}};
            if (topic == Topic("MessageSent(uint256,address,bytes32,uint256,string,string)")) {
                if (log["topics"].size() != 4)
                    throw runtime_error("Invalid chat event topics");
                event.update({{"type", "message"},
                              {"room", AbiNumber(EvmBytes(log["topics"][1]), 0)},
                              {"sender", log["topics"][2].get<string>().substr(24)},
                              {"client_id", log["topics"][3]},
                              {"message_id", AbiNumber(data, 0)},
                              {"body", AbiString(data, 1)},
                              {"nickname", AbiString(data, 2)}});
            } else if (topic == Topic("RoomCreated(uint256,string)"))
                event.update({{"type", "room"},
                              {"room", AbiNumber(EvmBytes(log["topics"][1]), 0)},
                              {"name", AbiString(data, 0)}});
            else if (topic == Topic("RoomOpen(uint256,bool)"))
                event.update({{"type", "room_open"},
                              {"room", AbiNumber(EvmBytes(log["topics"][1]), 0)},
                              {"open", AbiNumber(data, 0) != 0}});
            else
                continue;
            events.push_back(event);
        }
        return events;
    }
    void SyncIndex(uint64_t height) {
        auto stored = index.Rows("SELECT data FROM blocks ORDER BY height DESC LIMIT 1");
        uint64_t cursor = stored.empty() ? 0 : stored[0].at("height").get<uint64_t>();
        auto oldCursor = cursor;
        cursor = min(cursor, height);
        while (cursor) {
            auto rows = index.Rows("SELECT data FROM blocks WHERE height=" + to_string(cursor));
            if (!rows.empty() &&
                NodeQuery({{"method", "block"}, {"height", cursor}, {"limit", 1}}).at("hash") == rows[0].at("hash"))
                break;
            cursor--;
        }
        if (cursor < oldCursor) {
            index.Exec("BEGIN IMMEDIATE");
            try {
                for (const auto &table : {"blocks", "transactions", "events"})
                    index.Exec(string("DELETE FROM ") + table + " WHERE height>" + to_string(cursor));
                index.Exec("COMMIT");
                reorgs++;
            } catch (...) {
                index.Exec("ROLLBACK");
                throw;
            }
        }
        for (size_t work = 0; cursor < height && work < 20; work++) {
            auto h = cursor + 1;
            auto block = NodeQuery({{"method", "block"}, {"height", h}, {"limit", 10}});
            auto page = block;
            Json txs = page["transactions"];
            while (page.at("next").get<uint64_t>() < block.at("count").get<uint64_t>()) {
                page = NodeQuery({{"method", "block"},
                                  {"height", h},
                                  {"limit", 10},
                                  {"offset", page["next"]},
                                  {"expected_hash", block["hash"]}});
                for (const auto &tx : page["transactions"])
                    txs.push_back(tx);
            }
            index.Exec("BEGIN IMMEDIATE");
            try {
                for (auto tx : txs) {
                    tx["height"] = h;
                    tx["block_hash"] = block["hash"];
                    tx["observed_ms"] = NowMs();
                    index.Put("transactions", tx.at("hash"), tx, int64_t(h));
                    for (const auto &event : DecodeEvents(tx, h, block["hash"]))
                        index.Put("events", event.at("id"), event, int64_t(h));
                }
                block.erase("transactions");
                index.Put("blocks", block.at("hash"), block, int64_t(h));
                index.Exec("COMMIT");
            } catch (...) {
                index.Exec("ROLLBACK");
                throw;
            }
            cursor = h;
        }
        cache["indexed_height"] = cursor;
        for (auto item : index.Rows("SELECT data FROM outbox")) {
            auto hash = item.at("hash").get<string>();
            HexToU32(hash);
            auto rows = index.Rows("SELECT data FROM transactions WHERE hash='" + hash + "'");
            auto state = rows.empty()
                             ? (item.value("state", string{}) == "included" ? "reorganized"
                                                                            : item.value("state", string("submitted")))
                             : "included";
            item["state"] = state;
            if (!rows.empty()) {
                item["height"] = rows[0]["height"];
                item["block_hash"] = rows[0]["block_hash"];
                if (rows[0].contains("receipt"))
                    item["execution_status"] = rows[0]["receipt"]["status"];
            }
            index.Put("outbox", hash, item);
        }
    }
    void RecoverDeployment() {
        if (!profile.value("teacher", false))
            return;
        auto deployments =
            index.Rows("SELECT data FROM outbox WHERE json_extract(data,'$.kind')='deploy' ORDER BY rowid LIMIT 1");
        if (deployments.empty())
            return;
        auto saved = deployments[0];
        auto hash = saved.at("hash").get<string>();
        auto receipt = Query(Host(), Port(), 23, EvmBytes(hash));
        if (!receipt.is_null()) {
            if (receipt.at("status") != 0)
                throw runtime_error("聊天室部署交易执行回退，请查看交易回执");
            profile["contract"] = receipt.at("contract");
            profile["deploy_tx"] = hash;
            Persist();
        } else if (NowMs() - saved.value("last_sent_ms", int64_t(0)) > 5000) {
            Rpc(Host(), Port(), 17, EvmBytes(saved.at("raw")), false);
            saved["last_sent_ms"] = NowMs();
            index.Put("outbox", hash, saved);
        }
    }
    void Tick() {
        if (children.empty())
            return;
        try {
            auto stats = Query(Host(), Port(), 13);
            auto height = stats.at("height").get<uint64_t>(), txs = stats.at("chain_transactions").get<uint64_t>();
            auto now = NowMs();
            cache["stats"] = stats;
            cache["running"] = true;
            if (lastSampleMs && now > lastSampleMs) {
                cache["recent_tps"] = txs >= lastTxs ? double(txs - lastTxs) * 1000 / (now - lastSampleMs) : 0;
                cache["recent_block_ms"] =
                    height > lastHeight ? Json(double(now - lastSampleMs) / (height - lastHeight)) : Json(nullptr);
            }
            lastSampleMs = now;
            lastHeight = height;
            lastTxs = txs;
            cache["nodes"] = Json::array();
            for (size_t i = 0; i < profile["ports"].size(); i++) {
                auto port = profile["ports"][i].get<uint16_t>();
                try {
                    auto s = Query(Host(), port, 13);
                    auto peers = ClassroomRpc(Host(), port, {{"method", "peers"}});
                    cache["nodes"].push_back({{"id", i + 1},
                                              {"port", port},
                                              {"height", s["height"]},
                                              {"head", s["head"]},
                                              {"peers", s["peers"]},
                                              {"connections", peers["peers"]},
                                              {"online", true}});
                } catch (const exception &e) {
                    cache["nodes"].push_back({{"id", i + 1}, {"port", port}, {"online", false}, {"error", e.what()}});
                }
            }
            auto present = ContractPresent();
            if (!present) {
                RecoverDeployment();
                present = ContractPresent();
            }
            cache["contract_ready"] = present;
            SyncIndex(height);
            lastError.clear();
            if (vault.Unlocked()) {
                cache["wallets"] = PublicWallets();
                for (auto &wallet : cache["wallets"])
                    wallet.update(Account(Host(), Port(), wallet["public_key"]));
            }
        } catch (const exception &e) {
            lastError = e.what();
        }
    }
    Json History(uint64_t offset = 0, uint64_t limit = 50) {
        if (offset > 1000000 || !limit || limit > 100)
            throw invalid_argument("Invalid history page");
        if (!index.handle)
            return Json::array();
        return index.Rows("SELECT data FROM events WHERE json_extract(data,'$.type')='message' ORDER BY height "
                          "DESC,json_extract(data,'$.tx_index') DESC,json_extract(data,'$.log_index') DESC LIMIT " +
                          to_string(limit) + " OFFSET " + to_string(offset));
    }
    Json Status() {
        auto result = cache;
        result.update({{"unlocked", vault.Unlocked()},
                       {"has_wallet", fs::exists(vault.path)},
                       {"active_wallet", active},
                       {"nickname", nickname},
                       {"ipv6_available", ipv6},
                       {"last_error", lastError},
                       {"reorg_count", reorgs}});
        if (vault.Unlocked() && !result.contains("wallets"))
            result["wallets"] = PublicWallets();
        if (profile.is_object()) {
            auto publicProfile = profile;
            publicProfile.erase("grants");
            result["network"] = publicProfile;
            result["messages"] = History();
            result["blocks"] = index.Rows("SELECT data FROM blocks ORDER BY height DESC LIMIT 20");
            result["transactions"] = index.Rows("SELECT data FROM transactions ORDER BY height DESC LIMIT 40");
            result["outbox"] = Json::array();
            for (auto item : index.Rows("SELECT data FROM outbox ORDER BY rowid DESC LIMIT 50")) {
                item.erase("raw");
                result["outbox"].push_back(item);
            }
            map<uint64_t, Json> rooms;
            for (const auto &event :
                 index.Rows("SELECT data FROM events WHERE json_extract(data,'$.type')!='message' ORDER BY "
                            "height,json_extract(data,'$.tx_index'),json_extract(data,'$.log_index')")) {
                auto id = event.at("room").get<uint64_t>();
                if (event["type"] == "room")
                    rooms[id] = {{"id", id}, {"name", event["name"]}, {"open", true}};
                else if (rooms.count(id))
                    rooms[id]["open"] = event["open"];
            }
            result["rooms"] = Json::array();
            for (const auto &[id, room] : rooms)
                result["rooms"].push_back(room);
        }
        return result;
    }
    Json Api(const Json &request) {
        auto action = request.at("action").get<string>();
        if (action == "status")
            return Status();
        if (action == "wallet_create") {
            vault.Create(request.at("password"));
            cache.erase("wallets");
            return Status();
        }
        if (action == "unlock") {
            vault.Unlock(request.at("password"));
            cache.erase("wallets");
            return Status();
        }
        if (action == "lock") {
            vault.Lock();
            cache.erase("wallets");
            return Status();
        }
        if (action == "backup") {
            if (!vault.Unlocked())
                throw runtime_error("请先解锁钱包");
            return vault.Encrypt(request.at("password"), vault.secrets);
        }
        if (action == "restore") {
            if (fs::exists(vault.path))
                throw runtime_error("已有钱包，请在新的应用目录恢复备份");
            auto value = vault.Decrypt(request.at("password"), request.at("backup"));
            SaveJson(vault.path, request.at("backup"));
            vault.secrets = value;
            return Status();
        }
        if (action == "create" || action == "join") {
            NewNetwork(request, action == "join");
            return Status();
        }
        if (action == "start") {
            Start();
            return Status();
        }
        if (action == "stop") {
            Stop();
            return Status();
        }
        if (action == "select_wallet") {
            auto name = request.at("wallet").get<string>();
            auto key = vault.Key(name);
            sodium_memzero(key.private_key.data(), 64);
            active = name;
            if (profile.is_object())
                Persist();
            return Status();
        }
        if (action == "nickname") {
            auto value = request.at("nickname").get<string>();
            if (value.empty() || value.size() > 48)
                throw invalid_argument("昵称需要 1 至 48 个 UTF-8 字节");
            nickname = value;
            if (profile.is_object())
                Persist();
            return Status();
        }
        if (action == "block") {
            if (children.empty())
                throw runtime_error("节点未启动");
            auto query = request;
            query.erase("action");
            query["method"] = "block";
            return NodeQuery(query);
        }
        if (action == "history")
            return History(request.value("offset", uint64_t(0)), request.value("limit", uint64_t(50)));
        if (action == "call") {
            auto q = request;
            q.erase("action");
            q["method"] = "call";
            return NodeQuery(q);
        }
        if (action == "invite") {
            if (!profile.value("teacher", false))
                throw runtime_error("只有本机创建的课堂可以生成邀请");
            auto seeds = request.at("seeds");
            if (!seeds.is_array() || seeds.empty() || seeds.size() > 8)
                throw invalid_argument("请填写学生可访问的课堂入口");
            for (const auto &seed : seeds)
                ParseEndpoint(seed.get<string>());
            return {{"version", 1},
                    {"title", profile["title"]},
                    {"genesis", profile["genesis"]},
                    {"genesis_root", profile["genesis_root"]},
                    {"contract", profile["contract"]},
                    {"contract_code_sha256", Sha256(Runtime())},
                    {"seeds", seeds}};
        }
        if (action == "transfer" || action == "fund") {
            auto wallet = action == "fund" ? string("teacher") : active;
            if (action == "fund" && !profile.value("teacher", false))
                throw runtime_error("当前设备不是课堂教师");
            auto receiver = EvmBytes(request.at("receiver"));
            auto text = request.at("amount").get<string>();
            if (text.empty() || text.size() > 20 || text.find_first_not_of("0123456789") != string::npos)
                throw invalid_argument("金额必须是正整数");
            auto amount = stoull(text);
            if (action == "fund" && amount > 100000000)
                throw invalid_argument("每次最多发放一亿测试币");
            return Submit(wallet, "transfer", receiver, {}, amount, 0,
                          {{"receiver", request["receiver"]}, {"amount", to_string(amount)}});
        }
        if (action == "retry") {
            auto hash = request.at("hash").get<string>();
            HexToU32(hash);
            auto rows = index.Rows("SELECT data FROM outbox WHERE hash='" + hash + "'");
            if (rows.empty())
                throw runtime_error("找不到原交易");
            auto bytes = EvmBytes(rows[0].at("raw"));
            Rpc(Host(), Port(), rows[0].at("type").get<uint8_t>(), bytes, false);
            return {{"hash", hash}};
        }
        if (action == "message" || action == "room" || action == "room_open" || action == "member" ||
            action == "members_only") {
            if (!ContractPresent())
                throw runtime_error("聊天室合约尚未同步，请等待或检查课堂入口");
            string input;
            Json extra = Json::object();
            auto wallet = active;
            if (action == "message") {
                auto body = request.at("body").get<string>();
                if (body.empty() || body.size() > 512)
                    throw invalid_argument("消息需要 1 至 512 个 UTF-8 字节");
                auto room = request.value("room", uint64_t(1));
                auto id = request.value("client_id", RandomHex(32));
                HexToU32(id);
                auto first = EncodeString(body), second = EncodeString(nickname);
                input = Method("SendMessage(uint256,bytes32,string,string)") + Word(room) + id + Word(128) +
                        Word(128 + first.size() / 2) + first + second;
                extra = {{"body", body}, {"nickname", nickname}, {"room", room}, {"client_id", id}};
            } else {
                if (!profile.value("teacher", false))
                    throw runtime_error("当前设备不是课堂教师");
                wallet = "teacher";
                if (action == "room") {
                    auto name = request.at("name").get<string>();
                    if (name.empty() || name.size() > 96)
                        throw invalid_argument("房间名称需要 1 至 96 个 UTF-8 字节");
                    input = Method("CreateRoom(string)") + Word(32) + EncodeString(name);
                }
                if (action == "room_open")
                    input = Method("SetRoomOpen(uint256,bool)") + Word(request.at("room")) +
                            Word(request.at("open").get<bool>());
                if (action == "members_only")
                    input = Method("SetMembersOnly(bool)") + Word(request.at("enabled").get<bool>());
                if (action == "member") {
                    auto member = EvmBytes(request.at("member"));
                    if (member.size() != 20)
                        throw invalid_argument("成员需要 20 字节 EVM 地址");
                    input = Method("SetMember(address,bool)") + string(24, '0') + EvmHex(member) +
                            Word(request.at("allowed").get<bool>());
                }
            }
            return Submit(wallet, "call", EvmBytes(profile.at("contract")), EvmBytes(input), 0, 500000, extra);
        }
        if (action == "rebuild_index") {
            if (!index.handle)
                throw runtime_error("请先加入课堂");
            index.Exec("BEGIN IMMEDIATE; DELETE FROM events;DELETE FROM transactions;DELETE FROM blocks;COMMIT;");
            Tick();
            return Status();
        }
        if (action == "diagnostics")
            return {{"schema", 1},
                    {"application", "Chain Classroom 0.1.0"},
                    {"ipv6_available", ipv6},
                    {"status", Status()},
                    {"note", "No wallet seeds, signed outbox bytes or node log bodies included"}};
        throw invalid_argument("Unknown classroom action");
    }
};
