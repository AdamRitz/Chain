// Standalone calibration: do not link chain_vdf; upstream definitions live in this TU.
// The signing seed below is a public test fixture, never a node identity.
#include "../Crypto/VDF.cpp"
#include "../Crypto/WorkElection.h"

#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <numeric>
#include <string>
#if defined(__linux__)
#include <sys/resource.h>
#endif

namespace {
namespace work = chain::work_election;
using Clock = std::chrono::steady_clock;
struct Usage { double cpu = 0; long rss = 0; };
Usage CurrentUsage() {
#if defined(__linux__)
    rusage u{};
    if (getrusage(RUSAGE_SELF, &u) != 0) throw std::runtime_error("getrusage failed");
    return {u.ru_utime.tv_sec + u.ru_utime.tv_usec / 1e6 +
            u.ru_stime.tv_sec + u.ru_stime.tv_usec / 1e6, u.ru_maxrss};
#else
    return {};
#endif
}
struct Timing { double wall = 0, cpu = 0; long rss = 0; };
template<class F> Timing Measure(F&& f) {
    const auto usage = CurrentUsage(); const auto start = Clock::now();
    f();
    const auto end = Clock::now(); const auto after = CurrentUsage();
    return {std::chrono::duration<double>(end - start).count(), after.cpu - usage.cpu, after.rss};
}
void PrintTiming(const char* kind, std::uint64_t iterations, std::uint64_t round,
                 unsigned repeat, const Timing& timing, bool valid) {
    std::cout << "{\"type\":\"" << kind << "\",\"iterations\":" << iterations
              << ",\"round\":" << round << ",\"repeat\":" << repeat
              << ",\"wall_seconds\":" << timing.wall << ",\"cpu_seconds\":" << timing.cpu
              << ",\"peak_rss_kib\":" << timing.rss << ",\"valid\":" << (valid ? "true" : "false")
              << "}\n" << std::flush;
}
void PrintStats(const char* kind, std::uint64_t iterations, std::vector<double> values) {
    if (values.empty()) return;
    std::sort(values.begin(), values.end()); const auto n = values.size();
    const auto median = n % 2 ? values[n / 2] : (values[n / 2 - 1] + values[n / 2]) / 2;
    const auto p95 = values[static_cast<std::size_t>(std::ceil(n * 0.95)) - 1];
    std::cout << "{\"type\":\"summary\",\"operation\":\"" << kind << "\",\"iterations\":" << iterations
              << ",\"samples\":" << n << ",\"min_seconds\":" << values.front()
              << ",\"median_seconds\":" << median << ",\"p95_seconds\":" << p95
              << ",\"max_seconds\":" << values.back()
              << ",\"sum_seconds\":" << std::accumulate(values.begin(), values.end(), 0.0) << "}\n" << std::flush;
}
work::Digest FixtureDigest(std::string_view label, std::uint64_t round = 0) {
    std::array<std::uint8_t, 8> number{};
    chain::vdf_election::detail::Write64(number, round);
    return chain::vdf_election::detail::Hash(label, {number});
}
// Same discriminant, generator, squaring, reduction and output encoding as ProveSlow.
// Omits storing intermediate forms and producing the proof, so it measures time to y.
work::Output EvaluateOutput(const chain::vdf::Challenge& challenge, std::uint64_t iterations) {
    [[maybe_unused]] chain::vdf::ScratchForCall scratch;
    std::vector<std::uint8_t> seed(challenge.begin(), challenge.end());
    auto discriminant = CreateDiscriminant(seed, chain::vdf::kDiscriminantBits);
    auto x = DeserializeForm(discriminant, DEFAULT_ELEMENT, chain::vdf::kOutputBytes, true);
    integer bound = root(-discriminant, 4);
    PulmarkReducer reducer;
    form y = form::from_abd(x.a, x.b, discriminant);
    for (std::uint64_t i = 0; i < iterations; ++i) {
        nudupl_form(y, y, discriminant, bound);
        reducer.reduce(y);
    }
    const auto encoded = SerializeForm(y, discriminant.num_bits());
    if (encoded.size() != chain::vdf::kOutputBytes) throw std::runtime_error("Output length mismatch");
    work::Output result{}; std::copy(encoded.begin(), encoded.end(), result.begin()); return result;
}
// Compare the original stored-intermediate prover above the production memory threshold.
// The proof bytes and VerifyWork result are checked against the production prover below.
chain::vdf::Proof ProveWithStoredIntermediates(const chain::vdf::Challenge& challenge,
                                             std::uint64_t iterations) {
    [[maybe_unused]] chain::vdf::ScratchForCall scratch;
    std::vector<std::uint8_t> seed(challenge.begin(), challenge.end());
    auto discriminant = CreateDiscriminant(seed, chain::vdf::kDiscriminantBits);
    auto x = DeserializeForm(discriminant, DEFAULT_ELEMENT, chain::vdf::kOutputBytes, true);
    const auto encoded = ProveSlow(discriminant, x, iterations, "");
    if (encoded.size() != chain::vdf::kProofBytes) throw std::runtime_error("Stored-intermediate proof length mismatch");
    chain::vdf::Proof result{};
    std::copy(encoded.begin(), encoded.end(), result.begin()); return result;
}
std::uint64_t Number(const std::string& value) {
    if (value.empty() || !std::all_of(value.begin(), value.end(), [](char c) { return c >= '0' && c <= '9'; }))
        throw std::invalid_argument("Expected unsigned decimal number");
    std::size_t used = 0; const auto n = std::stoull(value, &used);
    if (used != value.size()) throw std::invalid_argument("Invalid number"); return n;
}
struct Options {
    std::uint64_t iterations = 65536, rounds = 1, repeats = 5, verifyThreads = 1;
    bool skipEval = false, verifyOnly = false, compareStored = false;
    std::filesystem::path corpus;
};
Options Parse(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--skip-eval") { o.skipEval = true; continue; }
        if (arg == "--verify-only") { o.verifyOnly = true; continue; }
        if (arg == "--compare-stored") { o.compareStored = true; continue; }
        if (i + 1 >= argc) throw std::invalid_argument("Missing argument value");
        const std::string value = argv[++i];
        if (arg == "--iterations") o.iterations = Number(value);
        else if (arg == "--rounds") o.rounds = Number(value);
        else if (arg == "--verify-repeats") o.repeats = Number(value);
        else if (arg == "--verify-threads") o.verifyThreads = Number(value);
        else if (arg == "--corpus-dir") o.corpus = value;
        else throw std::invalid_argument("Unknown argument: " + arg);
    }
    if (o.iterations < 4096 || o.iterations > chain::vdf::kMaxIterations ||
        o.rounds == 0 || o.rounds > 100000 || o.repeats == 0 || o.repeats > 10000)
        throw std::invalid_argument("Iterations 4096..100000000; rounds 1..100000; verify-repeats 1..10000");
    if (o.verifyOnly && o.corpus.empty()) throw std::invalid_argument("--verify-only requires --corpus-dir");
    if (o.verifyThreads != 1 && o.verifyThreads != 2) throw std::invalid_argument("--verify-threads must be 1 or 2");
    if (o.verifyOnly && o.compareStored) throw std::invalid_argument("--compare-stored requires proof generation");
    return o;
}
void WriteCorpus(const std::filesystem::path& path, const work::Work& record) {
    if (std::filesystem::exists(path)) throw std::runtime_error("Refusing to overwrite corpus record");
    const auto bytes = work::EncodeWork(record);
    std::ofstream out(path, std::ios::binary);
    out.write(reinterpret_cast<const char*>(bytes.data()), bytes.size()); out.close();
    if (!out) throw std::runtime_error("Cannot write corpus record");
}
work::Work ReadCorpus(const std::filesystem::path& path) {
    if (std::filesystem::file_size(path) != work::kEncodedWorkBytes) throw std::runtime_error("Invalid corpus record length");
    std::ifstream input(path, std::ios::binary);
    std::vector<std::uint8_t> bytes((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    work::Work record;
    if (!input || !work::DecodeWork(bytes, record)) throw std::runtime_error("Invalid corpus record"); return record;
}
} // namespace

int main(int argc, char** argv) {
    try {
        std::cout << std::setprecision(9);
        const auto o = Parse(argc, argv);
        if (sodium_init() < 0) throw std::runtime_error("sodium_init failed");
        const auto chainId = FixtureDigest("Chain/Benchmark/PublicFixtureChain/v1");
        const auto seed = FixtureDigest("Chain/Benchmark/PublicFixtureSigningSeed/v1");
        work::PublicKey publicKey{}; work::SecretKey secretKey{};
        crypto_sign_seed_keypair(publicKey.data(), secretKey.data(), seed.data());
        std::vector<work::Work> records; records.reserve(o.rounds);
        std::vector<double> evalTimes, proveTimes, storedTimes, verifyTimes;
        if (!o.corpus.empty() && !o.verifyOnly) std::filesystem::create_directories(o.corpus);
        std::cout << "{\"type\":\"config\",\"iterations\":" << o.iterations << ",\"rounds\":" << o.rounds
                  << ",\"verify_repeats\":" << o.repeats << ",\"verify_threads\":" << o.verifyThreads
                  << ",\"public_test_fixture\":true,\"verify_only\":"
                  << (o.verifyOnly ? "true" : "false") << ",\"compare_stored\":"
                  << (o.compareStored ? "true" : "false") << "}\n" << std::flush;
        work::Output previous{};
        for (std::uint64_t round = 0; round < o.rounds; ++round) {
            const auto anchor = FixtureDigest("Chain/Benchmark/PublicFixtureAnchor/v1", round);
            const auto path = o.corpus / ("work-" + std::to_string(o.iterations) + "-" + std::to_string(round) + ".bin");
            work::Work record{round, anchor, previous, {}};
            if (o.verifyOnly) record = ReadCorpus(path);
            else {
                work::Output evaluated{}; Timing evaluation;
                if (!o.skipEval) evaluation = Measure([&] {
                    evaluated = EvaluateOutput(work::DeriveWorkChallenge(chainId, publicKey, record, o.iterations), o.iterations);
                });
                const auto proving = Measure([&] { record = work::CreateWork(chainId, publicKey, round, anchor, previous, o.iterations); });
                proveTimes.push_back(proving.wall);
                if (!o.skipEval) {
                    const auto matches = evaluated == work::WorkOutput(record);
                    PrintTiming("evaluate_output", o.iterations, round, 0, evaluation, matches);
                    evalTimes.push_back(evaluation.wall);
                    if (!matches) throw std::runtime_error("Evaluation output differs from CreateWork");
                }
                bool valid = false;
                const auto firstVerify = Measure([&] { valid = work::VerifyWork(record, chainId, publicKey, anchor, o.iterations); });
                PrintTiming("create_work", o.iterations, round, 0, proving, valid);
                PrintTiming("verify_work", o.iterations, round, 0, firstVerify, valid);
                verifyTimes.push_back(firstVerify.wall);
                if (!valid) throw std::runtime_error("Generated Work failed verification");
                if (o.compareStored) {
                    auto stored = record;
                    const auto storedTiming = Measure([&] {
                        stored.proof = ProveWithStoredIntermediates(
                            work::DeriveWorkChallenge(chainId, publicKey, stored, o.iterations), o.iterations);
                    });
                    const bool identical = stored.proof == record.proof;
                    const bool verified = work::VerifyWork(stored, chainId, publicKey, anchor, o.iterations);
                    PrintTiming("create_work_stored_intermediates", o.iterations, round, 0,
                                storedTiming, identical && verified);
                    if (!identical || !verified) throw std::runtime_error("Stored-intermediate proof comparison failed");
                    storedTimes.push_back(storedTiming.wall);
                }
                if (!o.corpus.empty()) WriteCorpus(path, record);
            }
            if (record.round != round || record.anchor != anchor || record.previousOutput != previous)
                throw std::runtime_error("Corpus context or predecessor mismatch");
            for (unsigned repeat = o.verifyOnly ? 0 : 1; repeat < o.repeats; ++repeat) {
                bool valid = false;
                const auto timing = Measure([&] { valid = work::VerifyWork(record, chainId, publicKey, anchor, o.iterations); });
                PrintTiming("verify_work", o.iterations, round, repeat, timing, valid);
                verifyTimes.push_back(timing.wall);
                if (!valid) throw std::runtime_error("Work verification failed");
            }
            previous = work::WorkOutput(record); records.push_back(record);
        }
        PrintStats("evaluate_output", o.iterations, evalTimes);
        PrintStats("create_work", o.iterations, proveTimes);
        PrintStats("create_work_stored_intermediates", o.iterations, storedTimes);
        PrintStats("verify_work", o.iterations, verifyTimes);
        // For >=32 records the production four-by-eight sample verifies exactly 32 proofs.
        work::Context context;
        context.chainId = chainId; context.targetEpoch = o.rounds + 2;
        context.anchor = FixtureDigest("Chain/Benchmark/PublicFixtureElectionAnchor/v1", o.rounds);
        context.randomSeed = FixtureDigest("Chain/Benchmark/PublicFixtureRandomSeed/v1", o.rounds);
        context.matureRounds = o.rounds; context.iterations = o.iterations;
        const auto ticket = work::CreateTicket(context, secretKey, [&](std::uint64_t round, work::Work& out) {
            if (round >= records.size()) return false; out = records[round]; return true;
        });
        sodium_memzero(secretKey.data(), secretKey.size());
        const auto anchors = [&](std::uint64_t round, work::Digest& out) {
            if (round >= records.size()) return false; out = records[round].anchor; return true;
        };
        std::cout << "{\"type\":\"ticket_size\",\"works\":" << ticket.works.size()
                  << ",\"encoded_bytes\":" << work::EncodeTicket(ticket).size() << "}\n";
        std::vector<double> coldTimes, cachedTimes;
        work::VerificationCache cache;
        std::unique_ptr<work::ProofVerifier> verifier;
        if (o.verifyThreads == 2) verifier = std::make_unique<work::ProofVerifier>();
        for (unsigned repeat = 0; repeat < o.repeats; ++repeat) {
            bool valid = false;
            const auto timing = Measure([&] { valid = work::VerifyTicket(ticket, context, anchors, nullptr, nullptr, verifier.get()); });
            PrintTiming("verify_ticket_cold", o.iterations, o.rounds, repeat, timing, valid);
            if (!valid) throw std::runtime_error("Cold ticket verification failed"); coldTimes.push_back(timing.wall);
        }
        bool valid = false;
        const auto fill = Measure([&] { valid = work::VerifyTicket(ticket, context, anchors, nullptr, &cache, verifier.get()); });
        PrintTiming("verify_ticket_cache_fill", o.iterations, o.rounds, 0, fill, valid);
        if (!valid) throw std::runtime_error("Cache fill verification failed");
        for (unsigned repeat = 0; repeat < o.repeats; ++repeat) {
            const auto timing = Measure([&] { valid = work::VerifyTicket(ticket, context, anchors, nullptr, &cache); });
            PrintTiming("verify_ticket_cache_hit", o.iterations, o.rounds, repeat, timing, valid);
            if (!valid) throw std::runtime_error("Cached verification failed"); cachedTimes.push_back(timing.wall);
        }
        PrintStats("verify_ticket_cold", o.iterations, coldTimes);
        PrintStats("verify_ticket_cache_hit", o.iterations, cachedTimes);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "WorkVdfBenchmark: " << error.what() << '\n'; return 1;
    }
}
