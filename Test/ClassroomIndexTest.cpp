#include "../Classroom/Gateway/App.h"
void Require(bool value, const char *message) {
    if (!value)
        throw runtime_error(message);
}
struct MockChain {
    io_context io;
    tcp::acceptor acceptor{io, {ip::address_v4::loopback(), 0}};
    atomic<bool> running{true};
    mutex guard;
    vector<Json> blocks;
    thread worker;
    MockChain() {
        acceptor.non_blocking(true);
        worker = thread([&] {
            while (running) {
                tcp::socket socket(io);
                boost::system::error_code ec;
                acceptor.accept(socket, ec);
                if (ec) {
                    this_thread::sleep_for(std::chrono::milliseconds(2));
                    continue;
                }
                try {
                    array<uint8_t, 1> hello;
                    read(socket, buffer(hello));
                    array<uint8_t, 5> header;
                    read(socket, buffer(header));
                    vector<uint8_t> body(ReadU32(header.data() + 1));
                    read(socket, buffer(body));
                    auto request = Json::parse(body);
                    Json result;
                    {
                        lock_guard lock(guard);
                        auto height = request.at("height").get<size_t>();
                        result = blocks.at(height - 1);
                        if (request.contains("expected_hash") && request["expected_hash"] != result["hash"])
                            result = {{"error", "changed"}};
                    }
                    auto text = result.dump();
                    auto frame = GenerateMessage(47, {reinterpret_cast<const uint8_t *>(text.data()), text.size()});
                    write(socket, buffer(frame));
                } catch (const exception &) {
                }
            }
        });
    }
    ~MockChain() {
        running = false;
        worker.join();
    }
};
Json Block(uint64_t height, char hash, char parent, const string &body = {}, char txHash = 'a') {
    Json txs = Json::array();
    if (!body.empty()) {
        auto content = EncodeString(body), name = EncodeString("Alice");
        auto data = Word(1) + Word(96) + Word(96 + content.size() / 2) + content + name;
        Json log = {{"address", string(40, '4')},
                    {"topics",
                     {Topic("MessageSent(uint256,address,bytes32,uint256,string,string)"), Word(1),
                      string(24, '0') + string(40, '5'), string(64, '6')}},
                    {"data", data}};
        txs.push_back(
            {{"hash", string(64, txHash)}, {"index", 0}, {"receipt", {{"status", 0}, {"logs", Json::array({log})}}}});
    }
    return {{"height", height}, {"hash", string(64, hash)}, {"parent", string(64, parent)}, {"count", txs.size()},
            {"offset", 0},      {"next", txs.size()},       {"transactions", txs}};
}
int main(int argc, char **argv) {
    try {
        if (argc != 3)
            throw invalid_argument("Use root artifact");
        if (sodium_init() < 0)
            throw runtime_error("sodium");
        fs::path root = fs::absolute(argv[1]);
        Require(!fs::exists(root), "use a fresh root");
        fs::create_directories(root);
        MockChain chain;
        chain.blocks = {Block(1, '1', '0'), Block(2, '2', '1', "old branch")};
        ClassroomApp app(root, root / "unused", fs::absolute(argv[2]));
        app.profile = {{"id", string(24, '0')},
                       {"ports", {chain.acceptor.local_endpoint().port()}},
                       {"contract", string(40, '4')}};
        fs::create_directories(app.NetworkDir());
        app.index.Open(app.NetworkDir() / "index.sqlite");
        app.SyncIndex(2);
        Require(app.History().size() == 1 && app.History()[0]["body"] == "old branch", "old branch indexed");
        app.index.Put("outbox", string(64, 'a'), {{"hash", string(64, 'a')}, {"state", "included"}});
        {
            lock_guard lock(chain.guard);
            chain.blocks = {Block(1, '1', '0'), Block(2, '3', '1', "replacement branch", 'b'), Block(3, '4', '3')};
        }
        app.SyncIndex(3);
        Require(app.History().size() == 1 && app.History()[0]["body"] == "replacement branch",
                "reorg deletes stale events and indexes replacement");
        Require(app.index.Rows("SELECT data FROM outbox")[0]["state"] == "reorganized",
                "removed local message returns to reorganized state");
        {
            lock_guard lock(chain.guard);
            chain.blocks = {Block(1, '1', '0')};
        }
        app.SyncIndex(1);
        Require(app.History().empty(), "shorter canonical chain removes all later messages");
        bool invalid = false;
        try {
            AbiString(vector<uint8_t>(31), 0);
        } catch (const exception &) {
            invalid = true;
        }
        Require(invalid, "malformed ABI rejected");
        SaveJson(root / "result.json", {{"reorg_events", true},
                                        {"outbox_reorganized", true},
                                        {"shorter_chain", true},
                                        {"malformed_abi_rejected", true}});
        cout << "Classroom canonical index checks passed\n";
        return 0;
    } catch (const exception &e) {
        cerr << e.what() << '\n';
        return 1;
    }
}
