#pragma once
// Optional read-only teaching queries. No state, nonce or receipt is committed here.
nlohmann::json ClassroomQuery(const nlohmann::json &request) {
    auto method = request.at("method").get<string>();
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
