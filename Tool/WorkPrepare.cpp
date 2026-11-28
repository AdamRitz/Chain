// Prepare real historical Work from public chain anchors and node public keys.
// This executable does not open a ledger, read a signing key or start a node.
#include "../Crypto/WorkElection.h"
#include "Path.h"

#include <nlohmann/json.hpp>

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_set>

namespace {
namespace work = chain::work_election;
namespace fs = std::filesystem;
using Json = nlohmann::json;

struct Options {
    fs::path anchors, identities, output;
    unsigned threads = 2;
};

std::uint64_t Number(const std::string& value) {
    if (value.empty() || !std::all_of(value.begin(), value.end(), [](char c) { return c >= '0' && c <= '9'; }))
        throw std::invalid_argument("Expected an unsigned decimal number");
    std::size_t used = 0;
    const auto number = std::stoull(value, &used);
    if (used != value.size()) throw std::invalid_argument("Invalid number");
    return number;
}

Options Parse(int argc, char** argv) {
    Options result;
    for (int i = 1; i < argc; ++i) {
        const std::string option = argv[i];
        if (option == "--help") {
            std::cout << "work_prepare --anchors anchors.json [--identities identities.json] --out ROOT [--threads 2]\n";
            std::exit(0);
        }
        if (i + 1 == argc) throw std::invalid_argument("Missing option value");
        const std::string value = argv[++i];
        if (option == "--anchors") result.anchors = Utf8Path(value);
        else if (option == "--identities") result.identities = Utf8Path(value);
        else if (option == "--out") result.output = Utf8Path(value);
        else if (option == "--threads") {
            const auto threads = Number(value);
            if (!threads || threads > 32) throw std::invalid_argument("threads must be in 1..32");
            result.threads = static_cast<unsigned>(threads);
        } else throw std::invalid_argument("Unknown option");
    }
    if (result.anchors.empty() || result.output.empty()) throw std::invalid_argument("--anchors and --out are required");
    return result;
}

fs::path NativePath(fs::path path) {
    path = fs::absolute(path).lexically_normal();
#ifdef _WIN32
    auto native = path.make_preferred().wstring();
    if (!native.starts_with(L"\\\\?\\"))
        native = native.starts_with(L"\\\\") ? L"\\\\?\\UNC\\" + native.substr(2) : L"\\\\?\\" + native;
    return fs::path(native);
#else
    return path;
#endif
}

Json ReadJson(const fs::path& path) {
    if (!fs::is_regular_file(path) || fs::file_size(path) > 64 * 1024 * 1024)
        throw std::invalid_argument("Input JSON must be a regular file no larger than 64 MiB");
    std::ifstream input(path);
    if (!input) throw std::runtime_error("Cannot open input JSON");
    // Do not echo malformed input: identities may include unrelated private fields.
    try {
        Json result;
        input >> result;
        if (input.bad()) throw std::runtime_error("Cannot read input JSON");
        return result;
    } catch (const Json::exception&) { throw std::invalid_argument("Invalid input JSON"); }
}

template<std::size_t N> std::string Hex(const std::array<std::uint8_t, N>& bytes) {
    static constexpr char digits[] = "0123456789abcdef";
    std::string text(N * 2, '0');
    for (std::size_t i = 0; i < N; ++i) { text[2 * i] = digits[bytes[i] >> 4]; text[2 * i + 1] = digits[bytes[i] & 15]; }
    return text;
}

work::Digest Digest(const Json& value) {
    if (!value.is_string()) throw std::invalid_argument("Hash and public key fields must be hexadecimal strings");
    const auto& text = value.get_ref<const std::string&>();
    if (text.size() != 64) throw std::invalid_argument("Hash and public key fields require 64 hexadecimal characters");
    const auto digit = [](char c) -> unsigned {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        throw std::invalid_argument("Invalid hexadecimal hash or public key");
    };
    work::Digest result{};
    for (std::size_t i = 0; i < result.size(); ++i) result[i] = static_cast<std::uint8_t>((digit(text[2 * i]) << 4) | digit(text[2 * i + 1]));
    if (result == work::Digest{}) throw std::invalid_argument("Hash and public key fields must be nonzero");
    return result;
}

struct Inputs {
    work::Digest chain{};
    std::uint64_t iterations = 65536;
    std::vector<work::Digest> anchors;
    std::vector<work::PublicKey> identities;
};

Inputs Load(const Options& options) {
    const auto manifest = ReadJson(NativePath(options.anchors));
    if (!manifest.is_object() || !manifest.contains("chain_id") || !manifest.contains("iterations") ||
        !manifest.contains("anchors")) throw std::invalid_argument("Anchor JSON requires chain_id, iterations and anchors");
    Inputs result;
    result.chain = Digest(manifest.at("chain_id"));
    if (!manifest.at("iterations").is_number_unsigned()) throw std::invalid_argument("iterations must be an unsigned integer");
    result.iterations = manifest.at("iterations").get<std::uint64_t>();
    if (!work::detail::ValidIterations(result.iterations)) throw std::invalid_argument("iterations must be in 4096..100000000");
    const auto& anchors = manifest.at("anchors");
    if (!anchors.is_array() || anchors.empty() || anchors.size() > 100000)
        throw std::invalid_argument("anchors must contain 1..100000 consecutive rounds");
    for (std::size_t i = 0; i < anchors.size(); ++i) {
        const auto& entry = anchors[i];
        if (!entry.is_object() || !entry.contains("round") || !entry.at("round").is_number_unsigned() ||
            entry.at("round").get<std::uint64_t>() != i || !entry.contains("anchor"))
            throw std::invalid_argument("anchors must list every round in order starting at zero");
        result.anchors.push_back(Digest(entry.at("anchor")));
    }
    const auto identities = options.identities.empty() ? manifest.value("identities", Json{}) : ReadJson(NativePath(options.identities));
    if (!identities.is_array() || identities.empty() || identities.size() > 100000)
        throw std::invalid_argument("identities must contain 1..100000 public keys");
    std::unordered_set<std::string> seen;
    for (const auto& entry : identities) {
        if (!entry.is_string() && (!entry.is_object() || !entry.contains("public_key")))
            throw std::invalid_argument("An identity requires public_key");
        const auto key = Digest(entry.is_string() ? entry : entry.at("public_key"));
        if (!seen.insert(Hex(key)).second) throw std::invalid_argument("Identity public keys must be unique");
        result.identities.push_back(key);
    }
    return result;
}

work::Work ReadWork(const fs::path& path, const Inputs& input, const work::PublicKey& key,
                    std::size_t round, const work::Output& previous) {
    if (fs::is_symlink(path) || !fs::is_regular_file(path) || fs::file_size(path) != work::kEncodedWorkBytes)
        throw std::runtime_error("Existing Work is not a regular 346-byte record");
    std::ifstream stream(path, std::ios::binary);
    std::vector<std::uint8_t> bytes(work::kEncodedWorkBytes);
    stream.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    work::Work result;
    if (!stream || stream.peek() != std::char_traits<char>::eof() || !work::DecodeWork(bytes, result) ||
        result.round != round || result.anchor != input.anchors[round] || result.previousOutput != previous ||
        !work::VerifyWork(result, input.chain, key, input.anchors[round], input.iterations))
        throw std::runtime_error("Existing Work failed context, previous output or VDF verification; file preserved");
    return result;
}

void WriteWork(const fs::path& path, const work::Work& record) {
    std::array<std::uint8_t, 8> random{};
    randombytes_buf(random.data(), random.size());
    auto temporary = path;
    temporary += ".tmp-" + Hex(random);
    struct Cleanup {
        const fs::path& path;
        ~Cleanup() { std::error_code ignored; fs::remove(path, ignored); }
    } cleanup{temporary};
    if (fs::exists(temporary) || fs::is_symlink(temporary)) throw std::runtime_error("Work temporary filename already exists");
    const auto bytes = work::EncodeWork(record);
    std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
    if (!output) throw std::runtime_error("Cannot create temporary Work file");
    output.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    output.close();
    if (!output) throw std::runtime_error("Cannot write temporary Work file");
    // Linking a completed file publishes it atomically and never replaces an
    // existing destination. Both paths are on the same filesystem.
    std::error_code error;
    fs::create_hard_link(temporary, path, error);
    if (error) throw std::runtime_error("Cannot publish Work file without replacing an existing destination");
}

Json Prepare(const Options& options, const Inputs& input) {
    const auto started = std::chrono::steady_clock::now();
    const auto root = NativePath(options.output) / "work-vdf" / Hex(input.chain);
    fs::create_directories(root);
    const auto count = std::min<std::size_t>(options.threads, input.identities.size());
    std::atomic<std::size_t> next{0};
    std::atomic<std::uint64_t> generated{0}, reused{0}, completed{0};
    std::atomic<bool> stop{false};
    std::mutex errorMutex;
    std::string firstError;
    const auto fail = [&](std::string message) {
        stop.store(true);
        std::lock_guard lock(errorMutex);
        if (firstError.empty()) firstError = std::move(message);
    };
    const auto run = [&] {
        while (!stop.load()) {
            const auto index = next.fetch_add(1);
            if (index >= input.identities.size()) return;
            std::size_t round = 0;
            try {
                const auto& key = input.identities[index];
                const auto directory = root / Hex(key) / std::to_string(input.iterations) / "proofs";
                fs::create_directories(directory);
                work::Output previous{};
                for (; round < input.anchors.size(); ++round) {
                    if (stop.load()) return;
                    const auto path = directory / (std::to_string(round) + "-" + Hex(input.anchors[round]) + ".work");
                    work::Work record;
                    if (fs::exists(path) || fs::is_symlink(path)) {
                        record = ReadWork(path, input, key, round, previous);
                        ++reused;
                    } else {
                        record = work::CreateWork(input.chain, key, round, input.anchors[round], previous, input.iterations);
                        if (!work::VerifyWork(record, input.chain, key, input.anchors[round], input.iterations))
                            throw std::runtime_error("Generated Work failed VDF verification");
                        WriteWork(path, record);
                        ++generated;
                    }
                    previous = work::WorkOutput(record);
                }
                ++completed;
            } catch (const std::exception& error) {
                fail("Identity index " + std::to_string(index) + ", round " + std::to_string(round) + ": " + error.what());
            } catch (...) { fail("Work preparation failed with an unknown exception"); }
        }
    };
    std::vector<std::thread> workers;
    try {
        workers.reserve(count);
        for (std::size_t i = 0; i < count; ++i) workers.emplace_back(run);
    } catch (...) {
        stop.store(true);
        for (auto& worker : workers) worker.join();
        throw;
    }
    for (auto& worker : workers) worker.join();
    if (!firstError.empty()) throw std::runtime_error(firstError);
    const auto elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
    return { {"status", "prepared"}, {"chain_id", Hex(input.chain)}, {"iterations", input.iterations},
             {"identities", completed.load()}, {"rounds_per_identity", input.anchors.size()},
             {"files", generated.load() + reused.load()}, {"generated", generated.load()}, {"reused", reused.load()},
             {"record_bytes", work::kEncodedWorkBytes}, {"threads", count}, {"elapsed_seconds", elapsed} };
}
} // namespace

int main(int argc, char** argv) {
    try {
        if (sodium_init() < 0) throw std::runtime_error("Cannot initialize libsodium");
        const auto options = Parse(argc, argv);
        const auto input = Load(options);
        std::cout << Prepare(options, input).dump() << '\n';
        return 0;
    } catch (const nlohmann::json::exception&) {
        std::cerr << "Work preparation failed: invalid JSON field type\n";
    } catch (const std::exception& error) {
        std::cerr << "Work preparation failed: " << error.what() << '\n';
    }
    return 1;
}
