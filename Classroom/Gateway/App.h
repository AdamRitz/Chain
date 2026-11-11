#pragma once
#include "Support.h"
#include "NetworkSetup.h"
#include "Invite.h"
#include "../../Tool/ChatFunding.h"

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
    Json artifact, legacyArtifact, legacy2Artifact, profile, cache = Json::object();
    Vault vault;
    Database index;
    vector<unique_ptr<Child>> children;
    mutex guard;
    NetworkSetup networkSetup;
    Json addressState, invitePreview = Json::object(), previewInvite, previewSource;
    future<Json> previewTask;
    string previewTaskId;
    Json incomingInvitation;
    string active = "main", nickname = "成员";
    bool ipv6 = HasIPv6();
    string lastError;
    uint64_t reorgs = 0;
    int64_t lastSampleMs = 0;
    uint64_t lastHeight = 0, lastTxs = 0;
    string lastConnection;
    bool resumeOnLaunch = false;
    vector<Json> networkCatalog;
    Json fundingState = Json::object();
    future<Json> fundingTask;
    string fundingTaskNetwork;
    int64_t nextFundingMs = 0;
    size_t fundingEndpoint = 0;
    ClassroomApp(fs::path directory, fs::path executable, fs::path contract)
        : root(std::move(directory)), binary(std::move(executable)), artifactPath(std::move(contract)) {
        fs::create_directories(root / "networks");
        vault.path = root / "wallet.vault.json";
        artifact = ReadJson(artifactPath).at("contracts").at("ClassroomChat");
        legacyArtifact = ReadJson(artifactPath.parent_path() / "ClassroomChat.legacy-v1.compiled.json").at("contracts").at("ClassroomChat");
        legacy2Artifact = ReadJson(artifactPath.parent_path() / "ClassroomChat.legacy-v2.compiled.json").at("contracts").at("ClassroomChat");
        if (fs::exists(root / "session.json")) {
            auto saved = ReadJson(root / "session.json");
            auto id = saved.at("id").get<string>();
            profile = ReadProfile(id);
            active = saved.value("active", string("alice"));
            nickname = saved.value("nickname", string("成员"));
            resumeOnLaunch = saved.value("running", true); // Old desktops always resumed their selected chain.
            index.Open(NetworkDir() / "index.sqlite");
        }
        LoadConnection();
        RefreshNetworks();
    }
    ~ClassroomApp() {
        Stop();
    }
    fs::path NetworkDir() const {
        return root / "networks" / profile.at("id").get<string>();
    }
    Json ReadProfile(const string &id) {
        if (id.size() != 24 || id.find_first_not_of("0123456789abcdef") != string::npos)
            throw invalid_argument("保存的链 ID 无效");
        auto directory = root / "networks" / id;
        if (fs::is_symlink(directory) || fs::is_symlink(directory / "profile.json"))
            throw invalid_argument("保存的链路径无效");
        auto saved = ReadJson(directory / "profile.json");
        if (saved.at("id") != id) throw invalid_argument("保存的链路径与 ID 不一致");
        auto fingerprint = saved.at("genesis_root").get<string>();
        HexToU32(fingerprint);
        auto contract = saved.at("contract").get<string>();
        if (!contract.empty() && EvmBytes(contract).size() != 20) throw invalid_argument("保存的聊天室地址无效");
        auto version = saved.value("chat_version", 1);
        if (version < 1 || version > 3) throw invalid_argument("保存的聊天室版本无效");
        auto ports = saved.at("ports");
        if (!ports.is_array() || ports.empty()) throw invalid_argument("保存的节点端口无效");
        for (const auto &port : ports)
            if (!port.is_number_integer() || port.get<int64_t>() < 1 || port.get<int64_t>() > 65535)
                throw invalid_argument("保存的节点端口无效");
        auto previous = GetGenesisConfig();
        try {
            SetGenesisUsers(saved.at("genesis"));
            if (U32ToHex(genesisRoot) != fingerprint) throw invalid_argument("保存的链创世指纹不一致");
        } catch (...) { SetGenesisUsers(previous); throw; }
        SetGenesisUsers(previous);
        return saved;
    }
    vector<Json> SavedProfiles() {
        vector<Json> saved;
        for (const auto &entry : fs::directory_iterator(root / "networks")) {
            if (entry.is_symlink() || !entry.is_directory()) continue;
            try { saved.push_back(ReadProfile(PathUtf8(entry.path().filename()))); }
            catch (...) {} // A damaged unrelated profile must not disable the active chain.
        }
        sort(saved.begin(), saved.end(), [](const Json &a, const Json &b) { return a.at("id") < b.at("id"); });
        return saved;
    }
    Json Networks() {
        Json rows = Json::array();
        for (const auto &saved : networkCatalog) {
            bool selected = profile.is_object() && saved.at("id") == profile.at("id");
            rows.push_back({{"id", saved.at("id")}, {"title", saved.value("title", string{})},
                            {"genesis_root", saved.at("genesis_root")}, {"contract", saved.at("contract")},
                            {"chat_version", saved.value("chat_version", 1)}, {"selected", selected},
                            {"running", selected && !children.empty() && cache.value("running", false)}});
        }
        return rows;
    }
    void RefreshNetworks() { networkCatalog = SavedProfiles(); }
    void LoadConnection() {
        lastConnection.clear();
        if (!profile.is_object()) return;
        try {
            auto path = NetworkDir() / "last-connection.json";
            if (!fs::exists(path)) path = root / "last-connection.json"; // Import only a matching old selected chain.
            if (fs::exists(path)) {
                auto saved = ReadJson(path);
                if (saved.value("genesis_root", string{}) == profile.at("genesis_root").get<string>())
                    lastConnection = JoinAddress(saved.at("endpoint")).value_or("");
            }
        } catch (...) {}
        if (lastConnection.empty() && !profile.value("seeds", Json::array()).empty()) {
            try { lastConnection = JoinAddress(profile["seeds"][0]).value_or(""); } catch (...) {}
        }
    }
    void ClearChainState() {
        cache = Json::object(); addressState = Json::object();
        invitePreview = Json::object(); previewInvite = nullptr; previewSource = nullptr; previewTaskId.clear();
        fundingState = Json::object(); fundingTaskNetwork.clear(); nextFundingMs = 0; fundingEndpoint = 0;
        lastError.clear(); reorgs = 0; lastSampleMs = 0; lastHeight = 0; lastTxs = 0;
        LoadConnection();
    }
    void SelectProfile(const Json &next) {
        auto previous = profile;
        auto previousActive = active, previousNickname = nickname;
        bool wasRunning = !children.empty(), previousResume = resumeOnLaunch;
        if (profile.is_object()) Persist();
        Stop();
        try {
            profile = next;
            active = profile.value("active_wallet", previousActive);
            nickname = profile.value("nickname", previousNickname);
            if (vault.Unlocked() && !vault.secrets.contains(active)) active = vault.secrets.begin().key();
            ClearChainState();
            fs::create_directories(NetworkDir());
            index.Open(NetworkDir() / "index.sqlite");
            UpdateFundingProvider();
            Start();
        } catch (const exception &e) {
            auto error = string(e.what());
            Stop(); profile = previous; active = previousActive; nickname = previousNickname;
            resumeOnLaunch = previousResume;
            ClearChainState();
            if (profile.is_object()) {
                index.Open(NetworkDir() / "index.sqlite");
                if (wasRunning) {
                    try { Start(); }
                    catch (const exception &restore) { error += string("；原链恢复失败：") + restore.what(); }
                }
                Persist();
            } else {
                index.Close();
                if (fs::exists(root / "session.json")) fs::remove(root / "session.json");
            }
            lastError = error;
            throw runtime_error(error);
        }
    }
    void SwitchNetwork(const string &id) {
        auto next = ReadProfile(id); // Validate before stopping the current node.
        if (profile.is_object() && profile.at("id") == id) {
            if (children.empty()) Start();
            return;
        }
        SelectProfile(next);
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
    int ChatVersion() const { return profile.is_object() ? profile.value("chat_version", 1) : 3; }
    const Json &ChatArtifact(int version) const {
        if (version == 1) return legacyArtifact;
        if (version == 2) return legacy2Artifact;
        if (version == 3) return artifact;
        throw invalid_argument("不支持的聊天室合约版本");
    }
    string Method(const string &signature) {
        return ChatArtifact(ChatVersion()).at("evm").at("methodIdentifiers").at(signature);
    }
    string Runtime(int version = 0) const {
        return ChatArtifact(version ? version : ChatVersion()).at("evm").at("deployedBytecode").at("object");
    }
    Json ValidateInvite(const Json &input) {
        auto invite = DecodeInvite(input);
        try {
            auto version = invite.at("version").get<int>();
            if ((version < 1 || version > 3) || invite.at("contract_code_sha256") != Sha256(Runtime(version)))
                throw invalid_argument("这个邀请需要兼容的 NornV2 版本，请让朋友重新发送");
            auto previous = GetGenesisConfig(); string root;
            try { SetGenesisUsers(invite.at("genesis")); root = U32ToHex(genesisRoot); }
            catch (...) { SetGenesisUsers(previous); throw; }
            SetGenesisUsers(previous);
            if (root != invite.at("genesis_root").get<string>()) throw invalid_argument("邀请中的聊天室指纹不一致");
            auto contract = EvmBytes(invite.at("contract"));
            if (contract.size() != 20) throw invalid_argument("邀请中的聊天室地址无效");
            auto title = invite.at("title").get<string>();
            if (title.empty() || title.size() > 160) throw invalid_argument("邀请中的聊天室名称无效");
            auto seeds = invite.at("seeds");
            if (!seeds.is_array() || seeds.empty() || seeds.size() > 8) throw invalid_argument("邀请需要 1 至 8 个连接入口");
            Json normalized = Json::array(); set<string> seen;
            for (const auto &seed : seeds) {
                auto [host, port] = ParseEndpoint(seed.get<string>()); boost::system::error_code ec;
                auto address = ip::make_address(host, ec);
                if (!ec) host = CanonicalAddress(address).to_string();
                auto endpoint = FormatEndpoint(host, port);
                if (seen.insert(endpoint).second) normalized.push_back(endpoint);
            }
            invite["seeds"] = normalized;
            return invite;
        } catch (const invalid_argument &) { throw; }
        catch (...) { throw invalid_argument("邀请内容不完整或格式不正确，请让朋友重新发送"); }
    }
    void LaunchPreview() {
        auto source = previewSource; auto runtime1 = Runtime(1), runtime2 = Runtime(2), runtime3 = Runtime(3);
        previewTaskId = invitePreview.at("id").get<string>();
        previewTask = async(launch::async, [source, runtime1, runtime2, runtime3] { return CheckJoinSource(source, runtime1, runtime2, runtime3); });
    }
    void PollPreview() {
        if (!previewTask.valid() || previewTask.wait_for(std::chrono::seconds(0)) != future_status::ready) return;
        auto result = previewTask.get();
        if (!invitePreview.contains("id") || previewSource.is_null()) return;
        if (previewTaskId != invitePreview.at("id").get<string>()) { LaunchPreview(); return; }
        if (result.value("ok", false)) {
            try {
                // Genesis validation uses process globals and stays on the
                // gateway's serialized API/tick path, never a network worker.
                previewInvite = ValidateInvite(result.at("invite"));
                invitePreview["title"] = previewInvite.at("title");
                invitePreview["same_chain"] = profile.is_object() && profile.value("genesis_root", string{}) == previewInvite.at("genesis_root").get<string>() &&
                    profile.value("contract", string{}) == previewInvite.at("contract").get<string>();
            } catch (const exception &e) { result["ok"] = false; result["error"] = e.what(); }
        }
        result.erase("invite");
        invitePreview.update(result);
        invitePreview["phase"] = result["ok"] == true ? "ready" : "error";
        invitePreview["checked_ms"] = NowMs();
        if (result["ok"] != true) {
            Json local = Json::object();
            try { addressState = CollectInviteAddresses(profile.is_object() ? Port() : 0); local = addressState.at("ipv6"); } catch (...) {}
            invitePreview["local_ipv6"] = local;
            if (result.value("error", string{}).empty()) invitePreview["error"] = InviteFailureMessage(result, local);
        }
    }
    void SaveNetworkInfo() {
        if (profile.at("contract").get<string>().empty()) return;
        Json accounts = Json::array();
        for (const auto &account : profile.at("genesis").at("accounts"))
            accounts.push_back({{"public_key", account.at("public_key")}, {"balance", account.at("balance")}});
        Json genesis = {{"accounts", accounts}, {"base_fee", profile.at("genesis").value("base_fee", Json(0))}};
        Json info = {{"version", ChatVersion()}, {"title", profile.at("title")},
            {"genesis", genesis}, {"genesis_root", profile.at("genesis_root")},
            {"contract", profile.at("contract")}, {"contract_code_sha256", Sha256(Runtime())}};
        if (ChatVersion() == 3 && profile.contains("funding_provider")) info["funding_provider"] = profile["funding_provider"];
        auto path = NetworkDir() / "network-info.json";
        // Public metadata is immutable for a ready chain. Ordinary profile
        // saves must not replace a file that peers may be reading on Windows.
        if (fs::exists(path) && ReadJson(path) == info) return;
        SaveJson(path, info);
    }
    void Persist() {
        profile["active_wallet"] = active;
        profile["nickname"] = nickname;
        SaveJson(NetworkDir() / "profile.json", profile);
        RefreshNetworks();
        SaveNetworkInfo();
        SaveJson(root / "session.json", {{"id", profile["id"]}, {"active", active}, {"nickname", nickname}, {"running", resumeOnLaunch}});
    }
    Json PublicWallets() {
        Json wallets = Json::array();
        if (!vault.Unlocked())
            return wallets;
        for (const auto &[name, secret] : vault.secrets.items()) {
            auto key = vault.Key(name);
            auto evm = GetEvmAddress(key.public_key);
            wallets.push_back(
                {{"id", name}, {"name", "账户 " + to_string(wallets.size() + 1)}, {"public_key", U32ToHex(key.public_key)}, {"evm_address", EvmHex(evm.bytes)}});
            sodium_memzero(key.private_key.data(), key.private_key.size());
        }
        return wallets;
    }
    void Stop(bool remember = false) {
        children.clear();
        fundingState = {{"phase", "stopped"}};
        nextFundingMs = 0;
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
        if (remember && profile.is_object()) { resumeOnLaunch = false; Persist(); }
    }
    void Start() {
        if (!profile.is_object())
            throw runtime_error("请先创建或加入链");
        Stop();
        SetGenesisUsers(profile.at("genesis"));
        if (U32ToHex(genesisRoot) != profile.at("genesis_root").get<string>())
            throw runtime_error("保存的链创世指纹不一致");
        auto directory = NetworkDir();
        SaveJson(directory / "genesis.json", profile["genesis"]);
        bool migrate = profile.value("mode", string{}) != "network" || profile.contains("teacher");
        if (profile.at("ports").size() > 1) {
            profile["ports"] = Json::array({profile["ports"].at(0)});
            migrate = true;
        }
        if (!profile.contains("chat_version")) { profile["chat_version"] = 1; migrate = true; }
        profile["mode"] = "network";
        profile.erase("teacher");
        if (migrate) Persist();
        SaveNetworkInfo();
        auto ports = profile.at("ports");
        try {
            for (size_t i = 0; i < ports.size(); i++) {
                // A ready foreign listener must never masquerade as this child.
                io_context probeIo;
                tcp::acceptor probe(probeIo, tcp::endpoint(ip::address_v4::any(), ports[i].get<uint16_t>()));
                probe.close();
                auto node = directory / ("node" + to_string(i + 1));
                fs::create_directories(node);
                SaveJson(node / "seeds.json", profile.value("seeds", Json::array()));
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
                                       "0.0.0.0",
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
                args.push_back("--seeds-file"); args.push_back(PathUtf8(node / "seeds.json"));
                args.push_back("--network-info"); args.push_back(PathUtf8(directory / "network-info.json"));
                if (ipv6) {
                    args.push_back("--bind-v6");
                    args.push_back("::");
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
            for (size_t i = 0; i < ports.size(); i++) {
                while (true) {
                    if (!children[i]->Running()) throw runtime_error("节点进程已退出，请检查端口、数据库和节点日志");
                    try {
                        auto port = ports[i].get<uint16_t>();
                        auto stats = Query(Host(), port, 13);
                        if (stats.value("failed", false))
                            throw runtime_error("Node failed");
                        auto head = Rpc(Host(), port, 19);
                        if (head.size() != 80 || memcmp(head.data(), genesisRoot.data(), 32))
                            throw runtime_error("节点数据库创世指纹与所选链不一致");
                        if (!children[i]->Running()) throw runtime_error("Node exited");
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
            if (!cache.value("running", false)) throw runtime_error(lastError.empty() ? "节点启动状态无效" : lastError);
            resumeOnLaunch = true;
            Persist();
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
        if (!vault.Unlocked()) throw runtime_error("请先登录本机账户");
        auto requestedVersion = request.value("chat_version", 3);
        if (!joining && (requestedVersion < 1 || requestedVersion > 3)) throw invalid_argument("不支持的聊天室版本");
        Json next = {{"id", RandomHex(12)}, {"mode", "network"}, {"chat_version", requestedVersion},
                     {"title", request.value("title", string("学术交流"))}, {"contract", ""},
                     {"seeds", Json::array()}};
        auto identity = vault.secrets.contains("main") ? string("main") : active;
        // Validate everything that can be checked locally before touching the
        // running chain. Genesis parsing uses process-global account metadata.
        auto previousGenesis = GetGenesisConfig();
        try {
            if (joining) {
                auto invite = request.at("invite");
                if (invite.is_string()) invite = Json::parse(invite.get<string>());
                auto version = invite.at("version").get<int>();
                if ((version < 1 || version > 3) || invite.at("contract_code_sha256") != Sha256(Runtime(version)))
                    throw invalid_argument("链邀请与支持的聊天室合约版本不匹配");
                next["chat_version"] = version;
                next["title"] = invite.at("title");
                next["genesis"] = invite.at("genesis");
                SetGenesisUsers(next["genesis"]);
                if (U32ToHex(genesisRoot) != invite.at("genesis_root").get<string>())
                    throw invalid_argument("链的创世指纹不匹配");
                auto address = EvmBytes(invite.at("contract"));
                if (address.size() != 20) throw invalid_argument("聊天室合约地址无效");
                next["contract"] = EvmHex(address);
                next["genesis_root"] = invite["genesis_root"];
                auto seeds = invite.at("seeds");
                if (!seeds.is_array() || seeds.empty() || seeds.size() > 8)
                    throw invalid_argument("链邀请需要 1 至 8 个节点入口");
                for (const auto &seed : seeds) ParseEndpoint(seed.get<string>());
                next["seeds"] = seeds;
            } else {
                auto key = vault.Key(identity), anchor = GenerateWallet();
                next["genesis"] = {{"base_fee", 1}, {"accounts", Json::array({
                    {{"public_key", U32ToHex(key.public_key)}, {"balance", requestedVersion == 3 ? ChatGenesisSupply : 1000000000000ULL}},
                    {{"public_key", U32ToHex(anchor.public_key)}, {"balance", 0}}})}};
                sodium_memzero(key.private_key.data(), key.private_key.size());
                sodium_memzero(anchor.private_key.data(), anchor.private_key.size());
                SetGenesisUsers(next["genesis"]);
                next["genesis_root"] = U32ToHex(genesisRoot);
            }
            if (next.at("chat_version") == 3) {
                auto key = vault.Key(identity);
                next["funding_provider"] = U32ToHex(key.public_key);
                sodium_memzero(key.private_key.data(), key.private_key.size());
            }
            auto title = next["title"].get<string>();
            if (title.empty() || title.size() > 160) throw invalid_argument("链名称需要 1 至 160 个 UTF-8 字节");
            io_context io;
            tcp::acceptor probe(io, tcp::endpoint(ip::address_v4::loopback(), 0));
            next["ports"] = Json::array({probe.local_endpoint().port()});
        } catch (...) { SetGenesisUsers(previousGenesis); throw; }
        if (joining && profile.is_object() && profile.value("genesis_root", string{}) == next.at("genesis_root").get<string>() &&
            profile.value("contract", string{}) == next.at("contract").get<string>() && ChatVersion() == next.at("chat_version").get<int>()) {
            Json seeds = Json::array(); set<string> seen;
            auto localAddresses = CollectInviteAddresses(Port());
            for (const auto &seed : next["seeds"]) {
                auto [host, port] = ParseEndpoint(seed.get<string>()); boost::system::error_code ec;
                auto address = ip::make_address(host, ec); bool self = false;
                if (!ec && port == Port()) {
                    self = address.is_loopback();
                    for (const auto &local : localAddresses["addresses"]) self |= local["address"] == address.to_string();
                }
                if (!self && seen.insert(seed.get<string>()).second) seeds.push_back(seed);
            }
            for (const auto &seed : profile.value("seeds", Json::array()))
                if (seeds.size() < 8 && seen.insert(seed.get<string>()).second) seeds.push_back(seed);
            profile["seeds"] = seeds; UpdateFundingProvider(); Persist();
            SaveJson(NetworkDir() / "node1" / "seeds.json", seeds);
            if (children.empty()) Start();
            Tick();
            return;
        }
        if (joining) {
            for (auto saved : SavedProfiles()) {
                if (saved.at("genesis_root") != next.at("genesis_root") || saved.at("contract") != next.at("contract")) continue;
                if (saved.value("chat_version", 1) != next.at("chat_version").get<int>())
                    throw invalid_argument("保存的聊天室版本与邀请不一致");
                Json seeds = Json::array(); set<string> seen;
                auto localAddresses = CollectInviteAddresses(saved.at("ports").at(0).get<uint16_t>());
                for (const auto &seed : next.at("seeds")) {
                    auto [host, port] = ParseEndpoint(seed.get<string>()); boost::system::error_code ec;
                    auto address = ip::make_address(host, ec); bool self = false;
                    if (!ec && port == saved.at("ports").at(0).get<uint16_t>()) {
                        self = address.is_loopback();
                        for (const auto &local : localAddresses["addresses"]) self |= local["address"] == address.to_string();
                    }
                    if (!self && seen.insert(seed.get<string>()).second) seeds.push_back(seed);
                }
                for (const auto &seed : saved.value("seeds", Json::array()))
                    if (seeds.size() < 8 && seen.insert(seed.get<string>()).second) seeds.push_back(seed);
                saved["seeds"] = seeds;
                SelectProfile(saved);
                return;
            }
        }
        next["active_wallet"] = identity;
        next["nickname"] = nickname;
        SelectProfile(next);
        if (!joining) {
            auto code = EvmBytes(ChatArtifact(ChatVersion()).at("evm").at("bytecode").at("object"));
            auto sent = Submit(active, "deploy", {}, code, ChatVersion() == 3 ? ChatTreasuryAmount : 0, 2000000, Json::object());
            profile["deploy_tx"] = sent.at("hash");
            Persist();
            auto deadline = Clock::now() + std::chrono::seconds(20);
            Json receipt;
            do {
                receipt = Query(Host(), Port(), 23, EvmBytes(sent.at("hash")));
                if (!receipt.is_null()) break;
                this_thread::sleep_for(std::chrono::milliseconds(100));
            } while (Clock::now() < deadline);
            if (receipt.is_null() || receipt.at("status") != 0)
                throw runtime_error("聊天室部署尚未成功，请查看交易记录后恢复节点");
            profile["contract"] = receipt.at("contract");
            Persist();
            if (!ContractPresent()) throw runtime_error("聊天室合约尚未同步");
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
            throw runtime_error("代币不足");
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
    string FundingPublicKey() {
        auto key = vault.Key(active);
        auto text = U32ToHex(key.public_key);
        sodium_memzero(key.private_key.data(), key.private_key.size());
        return text;
    }
    void UpdateFundingProvider() {
        if (!profile.is_object() || ChatVersion() != 3 || !vault.Unlocked()) return;
        auto key = FundingPublicKey();
        if (profile.value("funding_provider", string{}) != key) {
            profile["funding_provider"] = key;
            Persist();
        }
    }
    uint64_t FundingView(const string &signature, const string &argument = "") {
        auto result = NodeQuery({{"method", "call"}, {"to", profile.at("contract")},
                                 {"input", Method(signature) + argument}, {"gas", 100000}});
        if (result.at("status") != 0) throw runtime_error("暂时无法读取聊天室补贴状态");
        auto bytes = EvmBytes(result.at("output"));
        if (bytes.size() != 32) throw runtime_error("聊天室补贴状态格式无效");
        return AbiNumber(bytes, 0);
    }
    bool FundingClaimed(const string &publicKey) {
        auto address = GetEvmAddress(HexToU32(publicKey));
        return FundingView("claimed(address)", string(24, '0') + EvmHex(address.bytes)) != 0;
    }
    void RetryFundingTransaction(Json saved) {
        auto account = Account(Host(), Port(), saved.at("sender"));
        if (stoull(saved.at("nonce").get<string>()) <= stoull(account.at("nonce").get<string>())) return;
        if (NowMs() - saved.value("last_sent_ms", int64_t(0)) < 3000) return;
        auto bytes = EvmBytes(saved.at("raw"));
        Rpc(Host(), Port(), saved.at("type").get<uint8_t>(), bytes, false);
        saved["last_sent_ms"] = NowMs();
        index.Put("outbox", saved.at("hash"), saved);
    }
    Json ClaimFunding(bool manual = false) {
        if (!profile.is_object() || ChatVersion() != 3) throw runtime_error("此聊天室沿用原有资助规则");
        if (!vault.Unlocked()) throw runtime_error("请先解锁钱包");
        if (children.empty() || !cache.value("contract_ready", false)) throw runtime_error("请等待聊天室同步就绪");
        auto key = FundingPublicKey();
        if (FundingClaimed(key)) {
            fundingState = {{"phase", "ready"}, {"claimed", true}};
            return {{"claimed", true}, {"already_claimed", true}};
        }
        auto account = Account(Host(), Port(), key);
        auto nonce = stoull(account.at("nonce").get<string>());
        for (const auto &saved : index.Rows("SELECT data FROM outbox ORDER BY rowid DESC")) {
            if (saved.value("sender", string{}) != key || saved.value("funding", string{}) != "claim") continue;
            if (stoull(saved.at("nonce").get<string>()) > nonce) {
                RetryFundingTransaction(saved);
                fundingState = {{"phase", "claiming"}, {"claimed", false}, {"hash", saved.at("hash")}};
                return {{"claimed", false}, {"pending", true}, {"hash", saved.at("hash")}};
            }
            if (!manual && saved.value("state", string{}) == "included" && saved.value("execution_status", 0) != 0)
                throw runtime_error("领取交易未执行成功，请查看交易回执后重试");
            break;
        }
        auto balance = stoull(account.at("balance").get<string>());
        auto fee = ReadUserNumber(profile.at("genesis").value("base_fee", Json(0)));
        if (balance < fee || balance - fee < ChatClaimGas) {
            if (balance != 0 || nonce != 0) throw runtime_error("代币不足");
            fundingState = {{"phase", "waiting"}, {"claimed", false}}; nextFundingMs = 0;
            return {{"claimed", false}, {"pending", true}};
        }
        if (FundingView("Reserve()") < ChatGrantAmount) throw runtime_error("聊天室补贴储备不足");
        auto sent = Submit(active, "call", EvmBytes(profile.at("contract")), EvmBytes(Method("Claim()")),
                           0, ChatClaimGas, {{"funding", "claim"}});
        fundingState = {{"phase", "claiming"}, {"claimed", false}, {"hash", sent.at("hash")}};
        return {{"claimed", false}, {"pending", true}, {"hash", sent.at("hash")}};
    }
    void SponsorFundingRequests(const string &sponsor) {
        auto directory = NetworkDir() / "funding-requests";
        if (!fs::exists(directory)) return;
        if (fs::is_symlink(directory) || !fs::is_directory(directory)) throw runtime_error("补贴请求目录无效");
        if (FundingView("Reserve()") < ChatGrantAmount) return;
        unsigned scanned = 0;
        for (const auto &entry : fs::directory_iterator(directory)) {
            if (++scanned > 64) break;
            auto filename = PathUtf8(entry.path().filename());
            if (entry.is_symlink() || !entry.is_regular_file() || filename.size() != 69 ||
                !filename.ends_with(".json") || filename.substr(0, 64).find_first_not_of("0123456789abcdef") != string::npos) continue;
            Json request;
            try {
                if (entry.file_size() > ChatFundingMaxRequestBytes) throw invalid_argument("Oversized request");
                request = ReadJson(entry.path());
                if (!ValidateFundingRequest(request, profile.at("genesis_root"), profile.at("contract"), sponsor, uint64_t(NowMs())) ||
                    request.at("public_key") != filename.substr(0, 64)) throw invalid_argument("Invalid funding proof");
            } catch (...) { fs::remove(entry.path()); continue; }
            auto recipient = request.at("public_key").get<string>();
            auto target = Account(Host(), Port(), recipient);
            if (stoull(target.at("balance").get<string>()) != 0 || stoull(target.at("nonce").get<string>()) != 0 || FundingClaimed(recipient)) {
                fs::remove(entry.path()); continue;
            }
            bool prior = false;
            for (const auto &saved : index.Rows("SELECT data FROM outbox ORDER BY rowid DESC")) {
                if (saved.value("funding", string{}) == "bootstrap" && saved.value("receiver", string{}) == recipient) {
                    prior = true;
                    RetryFundingTransaction(saved);
                    break;
                }
            }
            if (!prior) {
                Submit(active, "transfer", EvmBytes(recipient), {}, ChatBootstrapAmount, 0,
                       {{"receiver", recipient}, {"amount", to_string(ChatBootstrapAmount)}, {"funding", "bootstrap"}});
            }
            return; // At most one signed sponsorship/retry per tick; outbox is durable before I/O.
        }
    }
    void LaunchFundingDiscovery(const string &key, const string &endpoint) {
        auto genesis = profile.at("genesis_root").get<string>();
        auto contract = profile.at("contract").get<string>();
        auto codeHash = Sha256(Runtime());
        fundingTaskNetwork = profile.at("id").get<string>() + "/" + key;
        fundingTask = async(launch::async, [endpoint, genesis, contract, codeHash] {
            try {
                auto [host, port] = ParseEndpoint(endpoint);
                auto text = Json({{"method", "network"}}).dump();
                auto bytes = Rpc(host, port, 46, {text.begin(), text.end()}, true, 2, MaxInviteBytes);
                auto info = Json::parse(bytes).at("network");
                if (info.at("version") != 3 || info.at("genesis_root") != genesis || info.at("contract") != contract ||
                    info.at("contract_code_sha256") != codeHash) throw runtime_error("补贴入口与当前聊天室不一致");
                auto sponsor = ChatFundingDetail::Hex(info.at("funding_provider"), 32);
                return Json({{"ok", true}, {"stage", "discover"}, {"endpoint", endpoint}, {"sponsor", sponsor}});
            } catch (const exception &) {
                return Json({{"ok", false}, {"stage", "discover"}, {"error", "等待可用的自动资助入口"}});
            }
        });
    }
    void PollFundingRequest(const string &key) {
        if (!fundingTask.valid() || fundingTask.wait_for(std::chrono::seconds(0)) != future_status::ready) return;
        auto result = fundingTask.get();
        if (fundingTaskNetwork != profile.at("id").get<string>() + "/" + key) return;
        if (!result.value("ok", false)) {
            fundingState = {{"phase", "waiting"}, {"claimed", false}, {"error", result.value("error", string("等待自动资助"))}};
            nextFundingMs = NowMs() + 1000;
            return;
        }
        if (result.at("stage") == "request") { nextFundingMs = NowMs() + 5000; return; }
        auto account = Account(Host(), Port(), key);
        if (account.at("balance") != "0" || account.at("nonce") != "0" || FundingClaimed(key)) return;
        auto sponsor = result.at("sponsor").get<string>();
        if (sponsor == key) { nextFundingMs = NowMs() + 1000; return; }
        Json request = {{"method", "funding_request"}, {"genesis_root", profile.at("genesis_root")},
                        {"contract", profile.at("contract")}, {"public_key", key}, {"sponsor", sponsor},
                        {"nonce", RandomHex(16)}, {"timestamp_ms", uint64_t(NowMs())}};
        auto message = FundingRequestMessage(request);
        auto wallet = vault.Key(active);
        array<unsigned char, crypto_sign_BYTES> signature{};
        crypto_sign_detached(signature.data(), nullptr, reinterpret_cast<const unsigned char *>(message.data()), message.size(), wallet.private_key.data());
        sodium_memzero(wallet.private_key.data(), wallet.private_key.size());
        request["signature"] = EvmHex(signature);
        auto endpoint = result.at("endpoint").get<string>();
        // The worker owns only a public signed intent and endpoint, never a key,
        // vault, profile reference or process-global genesis metadata.
        fundingTask = async(launch::async, [endpoint, request] {
            try {
                auto [host, port] = ParseEndpoint(endpoint); auto text = request.dump();
                auto bytes = Rpc(host, port, 46, {text.begin(), text.end()}, true, 2, 4096);
                auto answer = Json::parse(bytes);
                if (!answer.value("queued", false) || answer.value("amount", string{}) != to_string(ChatBootstrapAmount))
                    throw runtime_error("等待自动资助");
                return Json({{"ok", true}, {"stage", "request"}});
            } catch (const exception &) { return Json({{"ok", false}, {"stage", "request"}, {"error", "等待自动资助入口处理"}}); }
        });
    }
    void TickFunding() {
        if (!profile.is_object() || ChatVersion() != 3) { fundingState = {{"phase", "legacy"}}; return; }
        if (!vault.Unlocked()) { fundingState = {{"phase", "waiting"}, {"error", "请解锁账户以继续自动领取"}}; return; }
        if (!cache.value("contract_ready", false)) { fundingState = {{"phase", "waiting"}}; return; }
        UpdateFundingProvider();
        auto key = FundingPublicKey();
        PollFundingRequest(key);
        if (FundingClaimed(key)) {
            fundingState = {{"phase", "ready"}, {"claimed", true}};
            try { SponsorFundingRequests(key); }
            catch (const exception &e) { fundingState["sponsor_error"] = e.what(); }
            return;
        }
        auto account = Account(Host(), Port(), key);
        auto balance = stoull(account.at("balance").get<string>()), nonce = stoull(account.at("nonce").get<string>());
        auto fee = ReadUserNumber(profile.at("genesis").value("base_fee", Json(0)));
        if (balance >= fee && balance - fee >= ChatClaimGas) { ClaimFunding(); return; }
        if (balance != 0 || nonce != 0) { fundingState = {{"phase", "error"}, {"claimed", false}, {"error", "代币不足"}}; return; }
        if (FundingView("Reserve()") < ChatGrantAmount) {
            fundingState = {{"phase", "unavailable"}, {"claimed", false}, {"error", "聊天室补贴储备不足"}};
            return;
        }
        if (fundingState.value("phase", string{}) != "waiting") fundingState = {{"phase", "waiting"}, {"claimed", false}};
        if (fundingTask.valid() || NowMs() < nextFundingMs) return;
        vector<string> endpoints; set<string> seen, localHosts;
        try {
            auto local = CollectInviteAddresses(Port());
            for (const auto &address : local.at("addresses")) localHosts.insert(address.at("address").get<string>());
        } catch (...) {}
        auto add = [&](const string &text) {
            try {
                auto [host, port] = ParseEndpoint(text); boost::system::error_code ec;
                auto address = ip::make_address(host, ec);
                if (!ec) {
                    address = CanonicalAddress(address); host = address.to_string();
                    if (port == Port() && (address.is_loopback() || address.is_unspecified() || localHosts.count(host))) return;
                }
                auto endpoint = FormatEndpoint(host, port);
                if (seen.insert(endpoint).second) endpoints.push_back(endpoint);
            } catch (...) {}
        };
        // Configured seeds retain priority; connected members remain usable
        // sponsors after the original creator/seed has gone offline.
        for (const auto &seed : profile.value("seeds", Json::array())) add(seed.get<string>());
        for (const auto &node : cache.value("nodes", Json::array())) for (const auto &peer : node.value("connections", Json::array())) {
            auto port = peer.value("port", uint16_t(0));
            if (port) add(FormatEndpoint(peer.at("host").get<string>(), port));
        }
        if (endpoints.empty()) { fundingState["error"] = "等待可用的自动资助入口"; return; }
        auto endpoint = endpoints.at(fundingEndpoint++ % endpoints.size());
        LaunchFundingDiscovery(key, endpoint);
    }
    void RememberConnection() {
        vector<pair<string, uint16_t>> connected;
        for (const auto &node : cache.value("nodes", Json::array())) for (const auto &peer : node.value("connections", Json::array())) {
            auto port = peer.value("port", uint16_t(0));
            if (!port || peer.value("height", uint64_t(0)) == 0) continue;
            connected.emplace_back(peer.at("host").get<string>(), port);
        }
        if (connected.empty()) return;
        string endpoint;
        // Newly joined entries lead the seeds list. Prefer the first entry
        // that is actually connected over an older peer's iteration order.
        for (const auto &seed : profile.value("seeds", Json::array())) {
            auto [seedHost, seedPort] = ParseEndpoint(seed.get<string>());
            for (const auto &[host, port] : connected)
                if (seedHost == host && seedPort == port) { endpoint = seed.get<string>(); break; }
            if (!endpoint.empty()) break;
        }
        if (endpoint.empty()) endpoint = FormatEndpoint(connected.front().first, connected.front().second);
        endpoint = JoinAddress(endpoint).value_or("");
        if (endpoint.empty() || endpoint == lastConnection) return;
        SaveJson(NetworkDir() / "last-connection.json", {{"endpoint", endpoint}, {"genesis_root", profile.at("genesis_root")}, {"connected_ms", NowMs()}});
        lastConnection = endpoint;
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
            cache["running"] = false;
            cache["contract_ready"] = false;
            if (cache.contains("nodes")) for (auto &node : cache["nodes"]) node["online"] = false;
        }
        if (cache.value("running", false)) {
            try { RememberConnection(); } catch (...) {}
            try { TickFunding(); }
            catch (const exception &e) { fundingState = {{"phase", "error"}, {"error", e.what()}}; }
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
        PollPreview();
        auto inbox = root / "incoming-invites";
        if (incomingInvitation.is_null() && fs::is_directory(inbox)) {
            unsigned scanned = 0;
            for (const auto &entry : fs::directory_iterator(inbox)) {
                if (++scanned > 16) break;
                auto name = PathUtf8(entry.path().filename());
                if (!name.starts_with("invite-") || entry.path().extension() != ".json" ||
                    entry.is_symlink() || !entry.is_regular_file()) continue;
                try {
                    if (entry.file_size() > MaxInviteBytes * 2 + 1024) throw runtime_error("邀请文件过大");
                    auto packet = ReadJson(entry.path());
                    incomingInvitation = {{"id", name}, {"text", packet.value("text", string{})}, {"error", packet.value("error", string{})}};
                } catch (...) { incomingInvitation = {{"id", name}, {"error", "邀请文件无法读取，请重新保存后再试。"}}; }
                fs::remove(entry.path()); // Only the exact consumed transient packet.
                break;
            }
        }
        auto result = cache;
        result["networks"] = Networks();
        result["incoming_invitation"] = incomingInvitation;
        result["invite_preview"] = invitePreview;
        result["network_addresses"] = addressState;
        result["network_tools"] = networkSetup.Status();
        result.update({{"unlocked", vault.Unlocked()},
                       {"has_wallet", fs::exists(vault.path)},
                       {"active_wallet", active},
                       {"nickname", nickname},
                       {"last_connection", lastConnection},
                       {"funding_state", fundingState},
                       {"ipv6_available", ipv6},
                       {"last_error", lastError},
                       {"reorg_count", reorgs}});
        if (vault.Unlocked() && !result.contains("wallets"))
            result["wallets"] = PublicWallets();
        if (profile.is_object()) {
            auto publicProfile = profile;
            publicProfile.erase("grants");
            result["network"] = publicProfile;
            bool connected = false; uint64_t remoteHeight = 0;
            for (const auto &node : cache.value("nodes", Json::array())) for (const auto &peer : node.value("connections", Json::array())) {
                auto height = peer.value("height", uint64_t(0)); connected |= height != 0; remoteHeight = max(remoteHeight, height);
            }
            bool local = profile.value("seeds", Json::array()).empty();
            auto indexed = cache.value("indexed_height", uint64_t(0));
            string phase = !cache.value("running", false) ? "stopped" :
                           !connected && !local ? "connecting" :
                           !cache.value("contract_ready", false) || indexed < remoteHeight ? "syncing" : "ready";
            result["connection_state"] = {{"phase", phase}, {"connected", connected}, {"indexed_height", indexed}, {"remote_height", remoteHeight}};
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
        if (action != "status" && action != "networks_refresh" && request.contains("network_id") &&
            (!request.at("network_id").is_string() || request.at("network_id").get<string>() !=
                (profile.is_object() ? profile.at("id").get<string>() : string{})))
            throw runtime_error("当前链已切换，请刷新后重试");
        if (action == "network_switch") {
            SwitchNetwork(request.at("id").get<string>());
            return Status();
        }
        if (action == "networks_refresh") { RefreshNetworks(); return Status(); }
        if (action == "invite_received") {
            if (incomingInvitation.is_object() && incomingInvitation.value("id", string{}) == request.value("id", string{})) incomingInvitation = nullptr;
            return {{"ok", true}};
        }
        if (action == "network_addresses") {
            addressState = CollectInviteAddresses(profile.is_object() ? Port() : 0);
            return addressState;
        }
        if (action == "invite_preview") {
            auto address = JoinAddress(request.at("invite"));
            previewSource = address ? Json(*address) : ValidateInvite(request.at("invite"));
            previewInvite = nullptr;
            invitePreview = {{"id", RandomHex(12)}, {"title", address ? Json(*address) : previewSource.at("title")}, {"phase", "checking"},
                {"same_chain", false}, {"by_address", bool(address)}, {"checked_ms", NowMs()}};
            if (!previewTask.valid()) LaunchPreview();
            return invitePreview;
        }
        if (action == "status")
            return Status();
        if (action == "funding_claim") return ClaimFunding(true);
        if (action == "network_status" || action == "network_configure" || action == "network_remove") {
            if (action == "network_configure" && !profile.is_object())
                throw runtime_error("请先创建或加入链");
            networkSetup.Start(action == "network_status" ? "status" : action == "network_configure" ? "configure" : "remove", request, binary, root);
            return Status();
        }
        if (action == "network_probe") {
            if (!profile.is_object()) throw runtime_error("请先创建或加入链");
            networkSetup.Probe(request.at("endpoint").get<string>(), profile.value("id", string{}));
            return Status();
        }
        if (action == "wallet_create") {
            vault.Create(request.at("password"));
            cache.erase("wallets");
            fundingState = Json::object(); nextFundingMs = 0;
            UpdateFundingProvider();
            return Status();
        }
        if (action == "unlock") {
            vault.Unlock(request.at("password"));
            if (!vault.secrets.contains(active)) active = vault.secrets.begin().key();
            cache.erase("wallets");
            fundingState = Json::object(); nextFundingMs = 0;
            UpdateFundingProvider();
            return Status();
        }
        if (action == "lock") {
            vault.Lock();
            cache.erase("wallets");
            fundingState = {{"phase", "waiting"}, {"error", "请解锁账户以继续自动领取"}};
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
            if (!vault.secrets.contains(active)) active = vault.secrets.begin().key();
            fundingState = Json::object(); nextFundingMs = 0;
            UpdateFundingProvider();
            return Status();
        }
        if (action == "create" || action == "join") {
            auto values = request;
            if (action == "join") {
                if (request.contains("preview_id")) {
                    PollPreview();
                    if (invitePreview.value("id", string{}) != request.at("preview_id").get<string>() ||
                        invitePreview.value("phase", string{}) != "ready" || NowMs() - invitePreview.at("checked_ms").get<int64_t>() > 60000)
                        throw runtime_error("请重新检查这份邀请的连接");
                    values["invite"] = previewInvite;
                    // Reachable entries go first; keep fallbacks for future reconnects.
                    auto seeds = invitePreview.at("seeds"); set<string> seen;
                    for (const auto &seed : seeds) seen.insert(seed.get<string>());
                    for (const auto &seed : previewInvite.at("seeds"))
                        if (seen.insert(seed.get<string>()).second) seeds.push_back(seed);
                    values["invite"]["seeds"] = seeds;
                } else {
                    auto address = JoinAddress(request.at("invite"));
                    values["invite"] = ValidateInvite(address ? FetchNetworkInvite(*address) : request.at("invite"));
                    auto checked = ProbeInvite(values["invite"], Runtime(values["invite"].at("version").get<int>()));
                    if (checked["ok"] != true) throw runtime_error("暂时联系不上邀请中的聊天室，请重新获取邀请");
                }
            }
            NewNetwork(values, action == "join");
            return Status();
        }
        if (action == "start") {
            Start();
            return Status();
        }
        if (action == "stop") {
            Stop(true);
            return Status();
        }
        if (action == "select_wallet") {
            auto name = request.at("wallet").get<string>();
            auto key = vault.Key(name);
            sodium_memzero(key.private_key.data(), 64);
            active = name;
            fundingState = Json::object(); nextFundingMs = 0;
            if (profile.is_object()) {
                UpdateFundingProvider();
                Persist();
            }
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
        if (action == "invite" || action == "invite_prepare") {
            if (!profile.is_object() || !ContractPresent())
                throw runtime_error("请先等待链和聊天室同步完成");
            if (action == "invite_prepare") {
                try {
                    addressState = CollectInviteAddresses(Port()); bool listening = false;
                    if (ipv6) {
                        try { auto reply = Rpc("::1", Port(), 19, {}, true, 1, 80); listening = reply.size() == 80 && EvmHex(span<const uint8_t>(reply).first(32)) == profile.at("genesis_root").get<string>(); }
                        catch (...) {}
                    }
                    if (!listening) {
                        auto diagnostic = addressState.at("ipv6");
                        auto addresses = addressState["addresses"];
                        for (auto &entry : addresses) if (entry["family"] == "IPv6") entry["preferred"] = false;
                        addressState = SelectInviteAddresses(addresses, Port());
                        addressState["ipv6"] = diagnostic;
                    }
                    addressState["ipv6_listening"] = listening;
                } catch (const exception &e) {
                    if (!request.contains("seeds")) throw;
                    addressState = {{"addresses", Json::array()}, {"seeds", Json::array()}, {"scope", "none"}, {"checked_ms", NowMs()}, {"error", e.what()}};
                }
            }
            auto seeds = request.value("seeds", action == "invite_prepare" ? addressState.at("seeds") : Json::array());
            if (!seeds.is_array() || seeds.empty() || seeds.size() > 8)
                throw invalid_argument("没有找到可分享的连接入口，请连接网络后重新检查，或在连接详情中添加已有入口");
            for (const auto &seed : seeds)
                ParseEndpoint(seed.get<string>());
            Json invite = {{"version", ChatVersion()},
                    {"title", profile["title"]},
                    {"genesis", profile["genesis"]},
                    {"genesis_root", profile["genesis_root"]},
                    {"contract", profile["contract"]},
                    {"contract_code_sha256", Sha256(Runtime())},
                    {"seeds", seeds}};
            if (action == "invite") return invite;
            invite["created_ms"] = NowMs();
            invite["scope"] = request.contains("seeds") ? "custom" : addressState.at("scope");
            auto token = EncodeInvite(invite);
            auto address = seeds.at(0).get<string>();
            return {{"invite", invite}, {"file", InviteEnvelope(invite)}, {"token", token}, {"address", address}, {"share_text", address}, {"connectivity", addressState}};
        }
        if (action == "transfer") {
            auto receiver = EvmBytes(request.at("receiver"));
            auto text = request.at("amount").get<string>();
            if (text.empty() || text.size() > 20 || text.find_first_not_of("0123456789") != string::npos)
                throw invalid_argument("金额必须是正整数");
            return Submit(active, "transfer", receiver, {}, stoull(text), 0,
                          {{"receiver", request["receiver"]}, {"amount", text}});
        }
        if (action == "retry") {
            auto hash = request.at("hash").get<string>();
            HexToU32(hash);
            auto rows = index.Rows("SELECT data FROM outbox WHERE hash='" + hash + "'");
            if (rows.empty())
                throw runtime_error("找不到原交易");
            if (rows[0].value("state", string{}) == "included")
                throw runtime_error("交易已入块；执行失败时请重新发送一笔消息");
            auto bytes = EvmBytes(rows[0].at("raw"));
            Rpc(Host(), Port(), rows[0].at("type").get<uint8_t>(), bytes, false);
            return {{"hash", hash}};
        }
        if (action == "message" || action == "room") {
            if (!ContractPresent()) throw runtime_error("聊天室合约尚未同步，请等待或检查链入口");
            string input;
            Json extra = Json::object();
            if (action == "message") {
                auto body = request.at("body").get<string>();
                if (body.empty() || body.size() > 512) throw invalid_argument("消息需要 1 至 512 个 UTF-8 字节");
                auto room = request.value("room", uint64_t(1));
                auto id = request.value("client_id", RandomHex(32)); HexToU32(id);
                auto first = EncodeString(body), second = EncodeString(nickname);
                input = Method("SendMessage(uint256,bytes32,string,string)") + Word(room) + id + Word(128) +
                        Word(128 + first.size() / 2) + first + second;
                extra = {{"body", body}, {"nickname", nickname}, {"room", room}, {"client_id", id}};
            } else {
                auto name = request.at("name").get<string>();
                if (name.empty() || name.size() > 96) throw invalid_argument("讨论分组名称需要 1 至 96 个 UTF-8 字节");
                input = Method("CreateRoom(string)") + Word(32) + EncodeString(name);
            }
            return Submit(active, "call", EvmBytes(profile.at("contract")), EvmBytes(input), 0, 500000, extra);
        }
        if (action == "rebuild_index") {
            if (!index.handle)
                throw runtime_error("请先加入链");
            index.Exec("BEGIN IMMEDIATE; DELETE FROM events;DELETE FROM transactions;DELETE FROM blocks;COMMIT;");
            Tick();
            return Status();
        }
        if (action == "diagnostics")
            return {{"schema", 1},
                    {"application", "NornV2 Peer Chat 0.4.2"},
                    {"ipv6_available", ipv6},
                    {"status", Status()},
                    {"note", "No wallet seeds, signed outbox bytes or node log bodies included"}};
        throw invalid_argument("Unknown chat action");
    }
};
