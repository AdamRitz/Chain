#pragma once
#include <chrono>
#include <filesystem>
#include <fstream>
#include <mutex>
#include "../Tool/Arguments.h"
#include "../Tool/ChatFunding.h"
// Local, gateway-owned public chain descriptor. Never supplied by a remote request.
string nodeNetworkInfoPath;
inline mutex chatFundingQueueMutex;

inline std::filesystem::file_status ChatFundingFileStatus(const std::filesystem::path &path) {
    std::error_code error;
    auto status = std::filesystem::symlink_status(path, error);
    if (error && error != std::errc::no_such_file_or_directory)
        throw std::filesystem::filesystem_error("Cannot inspect funding queue", path, error);
    return status;
}

// The queue contains signed public intents only. No wallet keys or chain state
// are written by this endpoint; the local gateway decides whether to sponsor.
inline void QueueChatFundingRequest(const nlohmann::json &request) {
    namespace fs = std::filesystem;
    lock_guard queueLock(chatFundingQueueMutex);
    auto directory = Utf8Path(nodeNetworkInfoPath).parent_path() / "funding-requests";
    auto status = ChatFundingFileStatus(directory);
    if (fs::is_symlink(status) || (fs::exists(status) && !fs::is_directory(status)))
        throw runtime_error("Funding queue must be a local directory");
    fs::create_directories(directory);
    auto publicKey = ChatFundingDetail::Hex(request.at("public_key"), 32);
    auto destination = directory / (publicKey + ".json");
    status = ChatFundingFileStatus(destination);
    if (fs::exists(status)) {
        if (!fs::is_regular_file(status) || fs::is_symlink(status)) throw runtime_error("Invalid funding queue entry");
        return; // A single pending request per public key; duplicates do not add files.
    }
    size_t count = 0;
    for (const auto &entry : fs::directory_iterator(directory)) {
        (void)entry;
        if (++count >= 64) throw runtime_error("Funding request queue is full");
    }
    nlohmann::json saved = {
        {"genesis_root", ChatFundingDetail::Hex(request.at("genesis_root"), 32)},
        {"contract", ChatFundingDetail::Hex(request.at("contract"), 20)}, {"public_key", publicKey},
        {"sponsor", ChatFundingDetail::Hex(request.at("sponsor"), 32)},
        {"nonce", ChatFundingDetail::Hex(request.at("nonce"), 16)},
        {"timestamp_ms", ChatFundingDetail::Timestamp(request.at("timestamp_ms"))},
        {"signature", ChatFundingDetail::Hex(request.at("signature"), crypto_sign_BYTES)}};
    auto bytes = saved.dump();
    if (bytes.size() > ChatFundingMaxRequestBytes) throw invalid_argument("Funding request exceeds the size limit");
    array<unsigned char, 16> random{}; randombytes_buf(random.data(), random.size());
    array<char, 33> suffix{}; sodium_bin2hex(suffix.data(), suffix.size(), random.data(), random.size());
    auto temporary = directory / ("." + publicKey + "." + suffix.data() + ".tmp");
    try {
        ofstream output(temporary, ios::binary | ios::trunc);
        if (!output) throw runtime_error("Cannot create funding request");
        output.write(bytes.data(), std::streamsize(bytes.size())); output.flush();
        if (!output) throw runtime_error("Cannot write funding request");
        output.close();
        if (!output) throw runtime_error("Cannot close funding request");
        fs::rename(temporary, destination); // Only a completely written request becomes visible.
    } catch (...) {
        std::error_code ignored; fs::remove(temporary, ignored);
        throw;
    }
}

// Existing queries remain read-only. funding_request only queues a verified
// off-chain intent; no balance, nonce, receipt or consensus rule is changed.
nlohmann::json ClassroomQuery(const nlohmann::json &request) {
    auto method = request.at("method").get<string>();
    if (method == "network" || method == "funding_request") {
        bool funding = method == "funding_request";
        if (funding && request.dump().size() > ChatFundingMaxRequestBytes)
            throw invalid_argument("Funding request exceeds the size limit");
        if (nodeNetworkInfoPath.empty()) throw invalid_argument("Address joining is unavailable on this node");
        ifstream input(Utf8Path(nodeNetworkInfoPath), ios::binary);
        if (!input) throw runtime_error("Public chain configuration is not ready");
        string bytes(256 * 1024 + 1, '\0'); input.read(bytes.data(), bytes.size());
        auto size = size_t(input.gcount());
        if (size > 256 * 1024) throw invalid_argument("Public chain configuration exceeds the size limit");
        auto stored = nlohmann::json::parse(bytes.begin(), bytes.begin() + size);
        nlohmann::json info;
        for (const auto *key : {"version", "title", "genesis", "genesis_root", "contract", "contract_code_sha256"})
            info[key] = stored.at(key);
        if (info.at("version") == 3 && stored.contains("funding_provider"))
            info["funding_provider"] = ChatFundingDetail::Hex(stored.at("funding_provider"), 32);
        if (info.at("genesis_root") != U32ToHex(genesisRoot)) throw invalid_argument("Public chain fingerprint mismatch");
        auto target = EvmBytes(info.at("contract").get<string>());
        if (target.size() != 20) throw runtime_error("Chat contract is not ready");
        evmc::address address; memcpy(address.bytes, target.data(), 20);
        shared_lock lock(dbCommitMutex);
        auto found = evmState.get_accounts().find(address);
        if (found == evmState.get_accounts().end() || found->second.code.empty()) throw runtime_error("Chat contract is not synchronized");
        auto code = EvmHex({found->second.code.data(), found->second.code.size()}); array<uint8_t, 32> hash;
        crypto_hash_sha256(hash.data(), reinterpret_cast<const uint8_t *>(code.data()), code.size());
        if (info.at("contract_code_sha256") != EvmHex(hash)) throw invalid_argument("Public chat code fingerprint mismatch");
        if (!funding) return {{"network", info}, {"head", U32ToHex(DBReadCurrentBlock())}};
        if (info.at("version") != 3 || !info.contains("funding_provider"))
            throw invalid_argument("Automatic funding is unavailable on this chat version");
        auto now = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        if (now < 0 || !ValidateFundingRequest(request, info.at("genesis_root").get<string>(),
                info.at("contract").get<string>(), info.at("funding_provider").get<string>(), uint64_t(now)))
            throw invalid_argument("Invalid or expired funding ownership proof");
        auto key = HexToU32(ChatFundingDetail::Hex(request.at("public_key"), 32));
        auto user = GetUser(users, key);
        if (user.balance != 0 || user.nonce != 0) throw invalid_argument("Funding requires a new zero-balance account");
        lock.unlock();
        QueueChatFundingRequest(request);
        return {{"queued", true}, {"amount", std::to_string(ChatBootstrapAmount)}};
    }
    if (method == "peers") {
        nlohmann::json peers = nlohmann::json::array();
        lock_guard lock(peerMutex);
        for (const auto &[id, peer] : peerPool)
            peers.push_back({{"id", id},
                             {"host", peer->address.to_string()},
                             {"port", peer->port},
                             {"height", peer->remoteHeight.load()},
                             {"ipv6", peer->address.is_v6()}});
        return {{"peers", peers}};
    }
    shared_lock lock(dbCommitMutex);
    auto height = DBReadBlockHeight();
    auto head = DBReadCurrentBlock();
    if (method == "code") {
        auto target = EvmBytes(request.at("to").get<string>());
        if (target.size() != 20)
            throw invalid_argument("Invalid contract address");
        evmc::address address;
        memcpy(address.bytes, target.data(), 20);
        auto found = evmState.get_accounts().find(address);
        return {{"code", found == evmState.get_accounts().end()
                             ? string{}
                             : EvmHex({found->second.code.data(), found->second.code.size()})},
                {"head", U32ToHex(head)}};
    }
    if (method == "block") {
        auto h = request.value("height", height), offset = request.value("offset", uint64_t(0)),
             limit = request.value("limit", uint64_t(10));
        if (!h || h > height || !limit || limit > 20 || offset > maxProtocolTx)
            throw invalid_argument("Invalid block page");
        auto block = UnSerializeBlock(DBReadBlockByHeight(to_string(h)));
        auto expected = request.value("expected_hash", string{});
        if (!expected.empty() && expected != U32ToHex(block.hash))
            throw runtime_error("Canonical block changed; restart page");
        nlohmann::json txs = nlohmann::json::array();
        for (size_t i = offset; i < block.txs.size() && i < offset + limit; i++) {
            auto &tx = block.txs[i];
            nlohmann::json item = {
                {"hash", EvmHex({tx.data() + 80, 32})},        {"sender", EvmHex({tx.data(), 32})},
                {"receiver", EvmHex({tx.data() + 32, 32})},    {"amount", to_string(ReadU64(tx.data() + 64))},
                {"nonce", to_string(ReadU64(tx.data() + 72))}, {"index", i}};
            auto input = block.evm.find(i);
            if (input != block.evm.end()) {
                item["kind"] = input->second[4] == 0 ? "deploy" : "call";
                item["target"] = EvmHex({tx.data() + 44, 20});
                item["input"] = EvmHex(span<const uint8_t>(input->second).subspan(45));
                string receipt;
                if (DBGet(DBHashKey("receipt/", HexToU32(item["hash"])), receipt))
                    item["receipt"] = nlohmann::json::parse(receipt);
            } else
                item["kind"] = "transfer";
            txs.push_back(std::move(item));
        }
        return {{"height", h},
                {"hash", U32ToHex(block.hash)},
                {"parent", U32ToHex(block.previousHash)},
                {"merkle_root", U32ToHex(block.merkleRoot)},
                {"count", block.txs.size()},
                {"offset", offset},
                {"next", offset + txs.size()},
                {"transactions", txs},
                {"observed_head", U32ToHex(head)}};
    }
    if (method == "call") {
        auto target = EvmBytes(request.at("to").get<string>()), input = EvmBytes(request.value("input", string{}));
        auto caller = EvmBytes(request.value("from", string(40, '0')));
        auto gas = request.value("gas", uint64_t(500000));
        if (target.size() != 20 || caller.size() != 20 || input.size() > 4096 || gas < 21000 || gas > 1000000)
            throw invalid_argument("Invalid static call");
        evmc::address address;
        memcpy(address.bytes, target.data(), 20);
        auto account = evmState.get_accounts().find(address);
        if (account == evmState.get_accounts().end() || account->second.code.empty())
            throw invalid_argument("Contract not found");
        EvmState overlay;
        overlay.SetBase(evmState);
        evmone::state::BlockInfo context{};
        context.number = height;
        context.timestamp = height;
        context.gas_limit = maxBlockGas;
        context.base_fee = 1;
        for (uint64_t h = height > 256 ? height - 256 : 1; h < height; h++) {
            auto hash = DBReadBlockHashByHeight(h);
            evmc::bytes32 word;
            memcpy(word.bytes, hash.data(), 32);
            context.known_block_hashes.emplace(h, word);
        }
        evmone::state::Transaction tx{};
        memcpy(tx.sender.bytes, caller.data(), 20);
        tx.chain_id = evmChainId;
        thread_local evmc::VM vm{evmc_create_evmone()};
        evmone::state::Host host(EVMC_SHANGHAI, vm, overlay, context, tx);
        evmc_message message{};
        message.kind = EVMC_CALL;
        message.flags = EVMC_STATIC;
        message.gas = int64_t(gas);
        message.recipient = address;
        message.code_address = address;
        message.sender = tx.sender;
        message.input_data = input.data();
        message.input_size = input.size();
        auto result =
            vm.execute(host, EVMC_SHANGHAI, message, account->second.code.data(), account->second.code.size());
        return {{"status", int(result.status_code)},
                {"output", EvmHex({result.output_data, result.output_size})},
                {"gas_used", gas - uint64_t(max(int64_t(0), result.gas_left))},
                {"height", height},
                {"head", U32ToHex(head)},
                {"read_only", true}};
    }
    throw invalid_argument("Unknown classroom query");
}
