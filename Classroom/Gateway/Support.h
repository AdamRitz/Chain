#pragma once
#include <utility>
#include <boost/asio.hpp>
#include <boost/beast.hpp>
#include <fstream>
#include <filesystem>
#include <future>
#include <sqlite3.h>
#include "../../Transaction/Transaction.h"
#include "../../Message/Message.h"
#include "../../EVM/BuildTransaction.h"
#include "../../Network/Address.h"
#include "../../Tool/Arguments.h"
#ifdef _WIN32
#include <windows.h>
#else
#include <sys/wait.h>
#include <unistd.h>
#include <signal.h>
#endif
using Json = nlohmann::json;
namespace fs = std::filesystem;
namespace beast = boost::beast;
namespace http = beast::http;
using namespace std;
using namespace boost::asio;
using Clock = std::chrono::steady_clock;

int64_t NowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch())
        .count();
}
string RandomHex(size_t count = 16) {
    vector<uint8_t> bytes(count);
    randombytes_buf(bytes.data(), bytes.size());
    return EvmHex(bytes);
}
Json ReadJson(const fs::path &path) {
    ifstream stream(path);
    if (!stream)
        throw runtime_error("Cannot read " + PathUtf8(path.filename()));
    Json data;
    stream >> data;
    return data;
}
void SaveJson(const fs::path &path, const Json &data) {
    auto temp = path;
    temp += ".tmp";
    {
        ofstream stream(temp, ios::binary | ios::trunc);
        stream << data.dump(2) << '\n';
        if (!stream)
            throw runtime_error("Cannot save " + PathUtf8(path.filename()));
    }
#ifdef _WIN32
    if (!MoveFileExW(temp.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
        throw runtime_error("Cannot replace saved settings");
#else
    fs::rename(temp, path);
    fs::permissions(path, fs::perms::owner_read | fs::perms::owner_write);
#endif
}
vector<uint8_t> Rpc(const string &host, uint16_t port, uint8_t kind, const vector<uint8_t> &data = {},
                    bool reply = true, int seconds = 4) {
    io_context io;
    tcp::socket socket(io);
    tcp::resolver resolver(io);
    steady_timer timer(io);
    timer.expires_after(std::chrono::seconds(seconds));
    timer.async_wait([&](boost::system::error_code ec) {
        if (!ec) {
            resolver.cancel();
            boost::system::error_code ignored;
            socket.close(ignored);
        }
    });
    auto future = co_spawn(
        io,
        [&]() -> awaitable<vector<uint8_t>> {
            auto entries = co_await resolver.async_resolve(host, to_string(port), use_awaitable);
            co_await async_connect(socket, entries, use_awaitable);
            socket.set_option(tcp::no_delay(true));
            auto frame = GenerateMessage(kind, data);
            frame.insert(frame.begin(), 2);
            co_await async_write(socket, buffer(frame), use_awaitable);
            if (!reply)
                co_return vector<uint8_t>{};
            array<uint8_t, 5> header;
            co_await async_read(socket, buffer(header), use_awaitable);
            auto size = ReadU32(header.data() + 1);
            if (header[0] != kind + 1 || size > maxBlockBytes)
                throw runtime_error("Invalid node response");
            vector<uint8_t> body(size);
            co_await async_read(socket, buffer(body), use_awaitable);
            co_return body;
        },
        use_future);
    // Completion cancels the timer without waiting the full timeout.
    while (future.wait_for(std::chrono::seconds(0)) != future_status::ready)
        io.run_one();
    timer.cancel();
    return future.get();
}
Json Query(const string &host, uint16_t port, uint8_t kind, const vector<uint8_t> &data = {}) {
    auto bytes = Rpc(host, port, kind, data);
    return Json::parse(bytes.begin(), bytes.end());
}
Json ClassroomRpc(const string &host, uint16_t port, const Json &request) {
    auto text = request.dump();
    auto result = Query(host, port, 46, {text.begin(), text.end()});
    if (result.contains("error"))
        throw runtime_error(result["error"].get<string>());
    return result;
}
Json Account(const string &host, uint16_t port, const string &key) {
    auto k = HexToU32(key);
    auto data = Rpc(host, port, 15, {k.begin(), k.end()});
    if (data.size() != 17)
        throw runtime_error("Invalid account response");
    return {{"balance", to_string(ReadU64(data.data() + 1))}, {"nonce", to_string(ReadU64(data.data() + 9))}};
}
uint16_t FreePort() {
    io_context io;
    tcp::acceptor a(io, {ip::address_v4::loopback(), 0});
    return a.local_endpoint().port();
}
bool HasIPv6() {
    try {
        io_context io;
        tcp::acceptor a(io, {ip::address_v6::loopback(), 0});
        return true;
    } catch (...) {
        return false;
    }
}

struct Database {
    sqlite3 *handle = nullptr;
    ~Database() {
        if (handle)
            sqlite3_close(handle);
    }
    void Open(const fs::path &path) {
        if (handle)
            sqlite3_close(handle);
        handle = nullptr;
        if (sqlite3_open(PathUtf8(path).c_str(), &handle) != SQLITE_OK)
            throw runtime_error("Cannot open classroom index");
        Exec(
            "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=3000; CREATE TABLE IF NOT EXISTS blocks(height INTEGER "
            "PRIMARY KEY,hash TEXT NOT NULL,data TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS transactions(hash TEXT PRIMARY KEY,height INTEGER NOT NULL,data TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS outbox(hash TEXT PRIMARY KEY,data TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS events(hash TEXT PRIMARY KEY,height INTEGER NOT NULL,data TEXT NOT NULL);");
    }
    void Exec(const string &sql) {
        char *error = nullptr;
        if (sqlite3_exec(handle, sql.c_str(), nullptr, nullptr, &error) != SQLITE_OK) {
            string message = error ? error : "Index error";
            sqlite3_free(error);
            throw runtime_error(message);
        }
    }
    void Put(const string &table, const string &key, const Json &data, int64_t height = 0) {
        string sql = table == "outbox" ? "INSERT OR REPLACE INTO outbox VALUES(?,?)"
                                       : "INSERT OR REPLACE INTO " + table + " VALUES(?,?,?)";
        sqlite3_stmt *statement = nullptr;
        if (sqlite3_prepare_v2(handle, sql.c_str(), -1, &statement, nullptr) != SQLITE_OK)
            throw runtime_error("Index prepare failed");
        if (table == "blocks")
            sqlite3_bind_int64(statement, 1, height);
        else
            sqlite3_bind_text(statement, 1, key.c_str(), -1, SQLITE_TRANSIENT);
        if (table == "transactions" || table == "events")
            sqlite3_bind_int64(statement, 2, height);
        else if (table == "blocks")
            sqlite3_bind_text(statement, 2, key.c_str(), -1, SQLITE_TRANSIENT);
        auto text = data.dump();
        sqlite3_bind_text(statement, table == "outbox" ? 2 : 3, text.c_str(), int(text.size()), SQLITE_TRANSIENT);
        auto rc = sqlite3_step(statement);
        sqlite3_finalize(statement);
        if (rc != SQLITE_DONE)
            throw runtime_error("Index write failed");
    }
    Json Rows(const string &sql) {
        Json rows = Json::array();
        sqlite3_stmt *statement = nullptr;
        if (sqlite3_prepare_v2(handle, sql.c_str(), -1, &statement, nullptr) != SQLITE_OK)
            throw runtime_error("Index read failed");
        while (sqlite3_step(statement) == SQLITE_ROW)
            rows.push_back(Json::parse(reinterpret_cast<const char *>(sqlite3_column_text(statement, 0))));
        sqlite3_finalize(statement);
        return rows;
    }
};

struct Vault {
    fs::path path;
    Json secrets;
    array<uint8_t, 32> encryptionKey{};
    ~Vault() {
        Lock();
    }
    void Lock() {
        if (secrets.is_object())
            for (auto &[name, value] : secrets.items())
                if (value.is_string()) {
                    auto &text = value.get_ref<string &>();
                    sodium_memzero(text.data(), text.size());
                }
        secrets = Json{};
        sodium_memzero(encryptionKey.data(), encryptionKey.size());
    }
    bool Unlocked() const {
        return secrets.is_object();
    }
    Json Encrypt(const string &password, const Json &secret) {
        if (password.size() < 8 || password.size() > 256)
            throw invalid_argument("钱包密码需要 8 至 256 个字节");
        array<uint8_t, crypto_pwhash_SALTBYTES> salt;
        randombytes_buf(salt.data(), salt.size());
        if (crypto_pwhash(encryptionKey.data(), encryptionKey.size(), password.data(), password.size(), salt.data(),
                          crypto_pwhash_OPSLIMIT_INTERACTIVE, crypto_pwhash_MEMLIMIT_INTERACTIVE,
                          crypto_pwhash_ALG_ARGON2ID13))
            throw runtime_error("Cannot derive wallet key");
        array<uint8_t, crypto_secretbox_NONCEBYTES> nonce;
        randombytes_buf(nonce.data(), nonce.size());
        auto text = secret.dump();
        vector<uint8_t> cipher(text.size() + crypto_secretbox_MACBYTES);
        crypto_secretbox_easy(cipher.data(), reinterpret_cast<const uint8_t *>(text.data()), text.size(), nonce.data(),
                              encryptionKey.data());
        sodium_memzero(text.data(), text.size());
        return {{"version", 1},
                {"kdf", "argon2id-interactive"},
                {"salt", EvmHex(salt)},
                {"nonce", EvmHex(nonce)},
                {"cipher", EvmHex(cipher)}};
    }
    Json Decrypt(const string &password, const Json &saved) {
        if (saved.at("version") != 1 || saved.at("kdf") != "argon2id-interactive" || password.size() > 256)
            throw invalid_argument("Unsupported wallet backup");
        auto salt = EvmBytes(saved.at("salt")), nonce = EvmBytes(saved.at("nonce")),
             cipher = EvmBytes(saved.at("cipher"));
        if (salt.size() != crypto_pwhash_SALTBYTES || nonce.size() != crypto_secretbox_NONCEBYTES ||
            cipher.size() < crypto_secretbox_MACBYTES || cipher.size() > 16384)
            throw invalid_argument("Invalid encrypted wallet");
        if (crypto_pwhash(encryptionKey.data(), encryptionKey.size(), password.data(), password.size(), salt.data(),
                          crypto_pwhash_OPSLIMIT_INTERACTIVE, crypto_pwhash_MEMLIMIT_INTERACTIVE,
                          crypto_pwhash_ALG_ARGON2ID13))
            throw runtime_error("Cannot derive wallet key");
        vector<uint8_t> plain(cipher.size() - crypto_secretbox_MACBYTES);
        if (crypto_secretbox_open_easy(plain.data(), cipher.data(), cipher.size(), nonce.data(),
                                       encryptionKey.data())) {
            sodium_memzero(encryptionKey.data(), 32);
            throw invalid_argument("钱包密码不正确或备份已损坏");
        }
        auto result = Json::parse(plain.begin(), plain.end());
        sodium_memzero(plain.data(), plain.size());
        for (auto name : {"alice", "bob", "teacher"})
            HexToU32(result.at(name));
        return result;
    }
    void Create(const string &password) {
        if (fs::exists(path))
            throw runtime_error("Wallet already exists");
        Json value;
        for (auto name : {"alice", "bob", "teacher"})
            value[name] = RandomHex(32);
        SaveJson(path, Encrypt(password, value));
        secrets = value;
    }
    void Unlock(const string &password) {
        Lock();
        secrets = Decrypt(password, ReadJson(path));
    }
    Wallet Key(const string &name) {
        if (!Unlocked() || (name != "alice" && name != "bob" && name != "teacher"))
            throw runtime_error("请先解锁钱包");
        auto seed = HexToU32(secrets.at(name));
        Wallet wallet;
        crypto_sign_seed_keypair(wallet.public_key.data(), wallet.private_key.data(), seed.data());
        sodium_memzero(seed.data(), 32);
        return wallet;
    }
};

struct Child {
    fs::path stopFile;
#ifdef _WIN32
    HANDLE process = nullptr, job = nullptr;
    static wstring Quote(const wstring &text) {
        wstring out = L"\"";
        size_t slashes = 0;
        for (wchar_t c : text) {
            if (c == L'\\') {
                slashes++;
                continue;
            }
            if (c == L'\"')
                out.append(slashes * 2 + 1, L'\\');
            else
                out.append(slashes, L'\\');
            slashes = 0;
            out += c;
        }
        out.append(slashes * 2, L'\\');
        return out + L"\"";
    }
#else
    pid_t pid = -1;
#endif
    Child(const fs::path &binary, const vector<string> &args, const fs::path &directory, const fs::path &stop)
        : stopFile(stop) {
        if (fs::exists(stopFile))
            fs::remove(stopFile);
#ifdef _WIN32
        auto widen = [](const string &text) { return Utf8Path(text).wstring(); };
        wstring command = Quote(binary.wstring());
        for (const auto &arg : args)
            command += L" " + Quote(widen(arg));
        STARTUPINFOW startup{};
        startup.cb = sizeof(startup);
        PROCESS_INFORMATION info{};
        auto log = CreateFileW((directory / L"node.log").c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                               nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (log == INVALID_HANDLE_VALUE)
            throw runtime_error("Cannot open node log");
        SetFilePointer(log, 0, nullptr, FILE_END);
        SetHandleInformation(log, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdOutput = log;
        startup.hStdError = log;
        startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
        job = CreateJobObjectW(nullptr, nullptr);
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if (!job || !SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
            CloseHandle(log);
            throw runtime_error("Cannot create node job");
        }
        if (!CreateProcessW(binary.c_str(), command.data(), nullptr, nullptr, true, CREATE_NO_WINDOW | CREATE_SUSPENDED,
                            nullptr, directory.c_str(), &startup, &info)) {
            CloseHandle(log);
            CloseHandle(job);
            throw runtime_error("Cannot start Chain node");
        }
        CloseHandle(log);
        process = info.hProcess;
        if (!AssignProcessToJobObject(job, process)) {
            TerminateProcess(process, 1);
            CloseHandle(process);
            CloseHandle(info.hThread);
            CloseHandle(job);
            throw runtime_error("Cannot supervise Chain node");
        }
        ResumeThread(info.hThread);
        CloseHandle(info.hThread);
#else
        pid = fork();
        if (pid < 0)
            throw runtime_error("Cannot fork node");
        if (!pid) {
            chdir(directory.c_str());
            auto log = fopen("node.log", "a");
            if (log) {
                dup2(fileno(log), 1);
                dup2(fileno(log), 2);
            }
            vector<string> values{PathUtf8(binary)};
            values.insert(values.end(), args.begin(), args.end());
            vector<char *> argv;
            for (auto &a : values)
                argv.push_back(a.data());
            argv.push_back(nullptr);
            execv(binary.c_str(), argv.data());
            _exit(127);
        }
#endif
    }
    ~Child() {
        Stop();
    }
    void Stop() {
        {
            ofstream signal(stopFile);
            signal << "stop\n";
        }
#ifdef _WIN32
        if (process) {
            if (WaitForSingleObject(process, 15000) == WAIT_TIMEOUT)
                TerminateProcess(process, 1);
            WaitForSingleObject(process, 5000);
            CloseHandle(process);
            process = nullptr;
        }
        if (job) {
            CloseHandle(job);
            job = nullptr;
        }
#else
        if (pid > 0) {
            for (int i = 0; i < 150; i++) {
                if (waitpid(pid, nullptr, WNOHANG) == pid) {
                    pid = -1;
                    break;
                }
                this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            if (pid > 0) {
                kill(pid, SIGKILL);
                waitpid(pid, nullptr, 0);
                pid = -1;
            }
        }
#endif
    }
};
