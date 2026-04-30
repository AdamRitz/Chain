#include <openssl/evp.h>

#include <chrono>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

struct BenchResult {
    std::string name;
    std::size_t input_size;
    std::uint64_t iterations;
    double seconds;
    double mb_per_sec;
    double hashes_per_sec;
    double ns_per_hash;
    unsigned int digest_guard;
};

void check_openssl(int ok, const std::string& msg)
{
    if (ok != 1) {
        throw std::runtime_error(msg);
    }
}

unsigned int hash_once(EVP_MD_CTX* ctx, const EVP_MD* md, const std::vector<unsigned char>& input)
{
    unsigned char out[EVP_MAX_MD_SIZE];
    unsigned int out_len = 0;

    check_openssl(EVP_DigestInit_ex(ctx, md, nullptr), "EVP_DigestInit_ex failed");
    check_openssl(EVP_DigestUpdate(ctx, input.data(), input.size()), "EVP_DigestUpdate failed");
    check_openssl(EVP_DigestFinal_ex(ctx, out, &out_len), "EVP_DigestFinal_ex failed");

    unsigned int guard = 0;
    for (unsigned int i = 0; i < out_len; ++i) {
        guard ^= out[i];
    }
    return guard;
}

BenchResult benchmark_hash(const std::string& name, const EVP_MD* md, std::size_t input_size, std::uint64_t iterations)
{
    std::vector<unsigned char> input(input_size);

    for (std::size_t i = 0; i < input.size(); ++i) {
        input[i] = static_cast<unsigned char>(i & 0xff);
    }

    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    if (ctx == nullptr) {
        throw std::runtime_error("EVP_MD_CTX_new failed");
    }

    unsigned int guard = 0;

    for (int i = 0; i < 1000; ++i) {
        guard ^= hash_once(ctx, md, input);
    }

    const auto start = std::chrono::steady_clock::now();

    for (std::uint64_t i = 0; i < iterations; ++i) {
        guard ^= hash_once(ctx, md, input);
    }

    const auto end = std::chrono::steady_clock::now();

    EVP_MD_CTX_free(ctx);

    const double seconds = std::chrono::duration<double>(end - start).count();
    const double total_bytes = static_cast<double>(input_size) * static_cast<double>(iterations);
    const double mb_per_sec = total_bytes / 1024.0 / 1024.0 / seconds;
    const double hashes_per_sec = static_cast<double>(iterations) / seconds;
    const double ns_per_hash = seconds * 1e9 / static_cast<double>(iterations);

    return BenchResult{name, input_size, iterations, seconds, mb_per_sec, hashes_per_sec, ns_per_hash, guard};
}

void print_result(const BenchResult& r)
{
    std::cout << std::left << std::setw(10) << r.name
              << std::right << std::setw(10) << r.input_size
              << std::setw(14) << r.iterations
              << std::setw(12) << std::fixed << std::setprecision(4) << r.seconds
              << std::setw(14) << std::fixed << std::setprecision(2) << r.mb_per_sec
              << std::setw(16) << std::fixed << std::setprecision(2) << r.hashes_per_sec
              << std::setw(16) << std::fixed << std::setprecision(2) << r.ns_per_hash
              << std::setw(10) << r.digest_guard
              << '\n';
}

int main()
{
    try {
        const std::vector<std::size_t> input_sizes = {
            64,
            256,
            1024,
            4096,
            1024 * 1024
        };

        const std::uint64_t target_bytes = 128ULL * 1024ULL * 1024ULL;

        std::cout << std::left << std::setw(10) << "Hash"
                  << std::right << std::setw(10) << "Bytes"
                  << std::setw(14) << "Iterations"
                  << std::setw(12) << "Seconds"
                  << std::setw(14) << "MB/s"
                  << std::setw(16) << "Hash/s"
                  << std::setw(16) << "ns/hash"
                  << std::setw(10) << "Guard"
                  << '\n';

        std::cout << std::string(102, '-') << '\n';

        for (std::size_t size : input_sizes) {
            std::uint64_t iterations = target_bytes / size;
            if (iterations < 1000) {
                iterations = 1000;
            }

            print_result(benchmark_hash("SHA2", EVP_sha256(), size, iterations));
            print_result(benchmark_hash("SHA3", EVP_sha3_256(), size, iterations));
        }

        return 0;
    } catch (const std::exception& e) {
        std::cerr << "benchmark failed: " << e.what() << std::endl;
        return 1;
    }
}