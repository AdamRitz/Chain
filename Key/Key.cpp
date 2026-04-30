#include "key.h"

#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/sha.h>
#include <openssl/err.h>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <iomanip>

namespace fs = std::filesystem;

namespace
{
    std::string openssl_error_string()
    {
        unsigned long err_code = ERR_get_error();
        if (err_code == 0) return "unknown openssl error";

        char buf[256] = {0};
        ERR_error_string_n(err_code, buf, sizeof(buf));
        return std::string(buf);
    }

    bool write_text_file(const fs::path& path, const std::string& content, std::string& err)
    {
        std::ofstream ofs(path, std::ios::binary);
        if (!ofs)
        {
            err = "failed to open file for writing: " + path.string();
            return false;
        }
        ofs << content;
        if (!ofs.good())
        {
            err = "failed to write file: " + path.string();
            return false;
        }
        return true;
    }

    bool get_raw_public_key_from_pkey(EVP_PKEY* pkey,
                                      std::vector<unsigned char>& pubkey,
                                      std::string& err)
    {
        size_t len = 0;
        if (EVP_PKEY_get_raw_public_key(pkey, nullptr, &len) != 1)
        {
            err = "EVP_PKEY_get_raw_public_key length failed: " + openssl_error_string();
            return false;
        }

        pubkey.resize(len);
        if (EVP_PKEY_get_raw_public_key(pkey, pubkey.data(), &len) != 1)
        {
            err = "EVP_PKEY_get_raw_public_key failed: " + openssl_error_string();
            return false;
        }
        pubkey.resize(len);
        return true;
    }
}

std::string hex_encode(const std::vector<unsigned char>& data)
{
    std::ostringstream oss;
    oss << std::hex << std::setfill('0');
    for (unsigned char c : data)
    {
        oss << std::setw(2) << static_cast<int>(c);
    }
    return oss.str();
}

bool hex_decode(const std::string& hex, std::vector<unsigned char>& out, std::string& err)
{
    if (hex.size() % 2 != 0)
    {
        err = "hex string length must be even";
        return false;
    }

    out.clear();
    out.reserve(hex.size() / 2);

    auto hex_val = [](char ch) -> int
    {
        if (ch >= '0' && ch <= '9') return ch - '0';
        if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
        if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
        return -1;
    };

    for (size_t i = 0; i < hex.size(); i += 2)
    {
        int hi = hex_val(hex[i]);
        int lo = hex_val(hex[i + 1]);
        if (hi < 0 || lo < 0)
        {
            err = "invalid hex character";
            return false;
        }
        out.push_back(static_cast<unsigned char>((hi << 4) | lo));
    }

    return true;
}

bool derive_address_from_public_key_hex(const std::string& public_key_hex,
                                        std::string& address,
                                        std::string& err)
{
    std::vector<unsigned char> pubkey;
    if (!hex_decode(public_key_hex, pubkey, err))
    {
        return false;
    }

    unsigned char hash[SHA256_DIGEST_LENGTH];
    SHA256(pubkey.data(), pubkey.size(), hash);

    std::vector<unsigned char> addr_bytes(hash, hash + 20);
    address = "0x" + hex_encode(addr_bytes);
    return true;
}

bool load_public_key_hex_from_pem(const std::string& public_pem_path,
                                  std::string& public_key_hex,
                                  std::string& err)
{
    FILE* fp = std::fopen(public_pem_path.c_str(), "rb");
    if (!fp)
    {
        err = "failed to open public pem: " + public_pem_path;
        return false;
    }

    EVP_PKEY* pkey = PEM_read_PUBKEY(fp, nullptr, nullptr, nullptr);
    std::fclose(fp);

    if (!pkey)
    {
        err = "PEM_read_PUBKEY failed: " + openssl_error_string();
        return false;
    }

    std::vector<unsigned char> pubkey;
    bool ok = get_raw_public_key_from_pkey(pkey, pubkey, err);
    EVP_PKEY_free(pkey);

    if (!ok) return false;

    public_key_hex = hex_encode(pubkey);
    return true;
}

bool get_public_key_hex_from_private_pem(const std::string& private_pem_path,
                                         std::string& public_key_hex,
                                         std::string& err)
{
    FILE* fp = std::fopen(private_pem_path.c_str(), "rb");
    if (!fp)
    {
        err = "failed to open private pem: " + private_pem_path;
        return false;
    }

    EVP_PKEY* pkey = PEM_read_PrivateKey(fp, nullptr, nullptr, nullptr);
    std::fclose(fp);

    if (!pkey)
    {
        err = "PEM_read_PrivateKey failed: " + openssl_error_string();
        return false;
    }

    std::vector<unsigned char> pubkey;
    bool ok = get_raw_public_key_from_pkey(pkey, pubkey, err);
    EVP_PKEY_free(pkey);

    if (!ok) return false;

    public_key_hex = hex_encode(pubkey);
    return true;
}

bool sign_message_with_private_key(const std::string& private_pem_path,
                                   const std::vector<unsigned char>& message,
                                   std::vector<unsigned char>& signature,
                                   std::string& err)
{
    FILE* fp = std::fopen(private_pem_path.c_str(), "rb");
    if (!fp)
    {
        err = "failed to open private pem: " + private_pem_path;
        return false;
    }

    EVP_PKEY* pkey = PEM_read_PrivateKey(fp, nullptr, nullptr, nullptr);
    std::fclose(fp);

    if (!pkey)
    {
        err = "PEM_read_PrivateKey failed: " + openssl_error_string();
        return false;
    }

    EVP_MD_CTX* mdctx = EVP_MD_CTX_new();
    if (!mdctx)
    {
        EVP_PKEY_free(pkey);
        err = "EVP_MD_CTX_new failed";
        return false;
    }

    bool ok = false;
    do
    {
        if (EVP_DigestSignInit(mdctx, nullptr, nullptr, nullptr, pkey) != 1)
        {
            err = "EVP_DigestSignInit failed: " + openssl_error_string();
            break;
        }

        size_t sig_len = 0;
        if (EVP_DigestSign(mdctx, nullptr, &sig_len, message.data(), message.size()) != 1)
        {
            err = "EVP_DigestSign length failed: " + openssl_error_string();
            break;
        }

        signature.resize(sig_len);
        if (EVP_DigestSign(mdctx, signature.data(), &sig_len, message.data(), message.size()) != 1)
        {
            err = "EVP_DigestSign failed: " + openssl_error_string();
            break;
        }

        signature.resize(sig_len);
        ok = true;
    }
    while (false);

    EVP_MD_CTX_free(mdctx);
    EVP_PKEY_free(pkey);
    return ok;
}

bool verify_message_with_public_key_hex(const std::string& public_key_hex,
                                        const std::vector<unsigned char>& message,
                                        const std::vector<unsigned char>& signature,
                                        std::string& err)
{
    std::vector<unsigned char> pubkey;
    if (!hex_decode(public_key_hex, pubkey, err))
    {
        return false;
    }

    EVP_PKEY* pkey = EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, nullptr, pubkey.data(), pubkey.size());
    if (!pkey)
    {
        err = "EVP_PKEY_new_raw_public_key failed: " + openssl_error_string();
        return false;
    }

    EVP_MD_CTX* mdctx = EVP_MD_CTX_new();
    if (!mdctx)
    {
        EVP_PKEY_free(pkey);
        err = "EVP_MD_CTX_new failed";
        return false;
    }

    bool ok = false;
    do
    {
        if (EVP_DigestVerifyInit(mdctx, nullptr, nullptr, nullptr, pkey) != 1)
        {
            err = "EVP_DigestVerifyInit failed: " + openssl_error_string();
            break;
        }

        int rc = EVP_DigestVerify(mdctx,
                                  signature.data(),
                                  signature.size(),
                                  message.data(),
                                  message.size());

        if (rc != 1)
        {
            err = "signature verify failed";
            break;
        }

        ok = true;
    }
    while (false);

    EVP_MD_CTX_free(mdctx);
    EVP_PKEY_free(pkey);
    return ok;
}

bool generate_and_store_keypair(const std::string& dir, LocalKeyInfo& out, std::string& err)
{
    fs::create_directories(dir);

    fs::path base(dir);
    fs::path private_pem = base / "private.pem";
    fs::path public_pem  = base / "public.pem";
    fs::path public_hex  = base / "public.hex";
    fs::path address_txt = base / "address.txt";

    EVP_PKEY_CTX* pctx = EVP_PKEY_CTX_new_id(EVP_PKEY_ED25519, nullptr);
    if (!pctx)
    {
        err = "EVP_PKEY_CTX_new_id failed: " + openssl_error_string();
        return false;
    }

    EVP_PKEY* pkey = nullptr;

    bool ok = false;
    do
    {
        if (EVP_PKEY_keygen_init(pctx) != 1)
        {
            err = "EVP_PKEY_keygen_init failed: " + openssl_error_string();
            break;
        }

        if (EVP_PKEY_keygen(pctx, &pkey) != 1)
        {
            err = "EVP_PKEY_keygen failed: " + openssl_error_string();
            break;
        }

        {
            FILE* fp = std::fopen(private_pem.string().c_str(), "wb");
            if (!fp)
            {
                err = "failed to open private.pem for writing";
                break;
            }
            int rc = PEM_write_PrivateKey(fp, pkey, nullptr, nullptr, 0, nullptr, nullptr);
            std::fclose(fp);
            if (rc != 1)
            {
                err = "PEM_write_PrivateKey failed: " + openssl_error_string();
                break;
            }
        }

        {
            FILE* fp = std::fopen(public_pem.string().c_str(), "wb");
            if (!fp)
            {
                err = "failed to open public.pem for writing";
                break;
            }
            int rc = PEM_write_PUBKEY(fp, pkey);
            std::fclose(fp);
            if (rc != 1)
            {
                err = "PEM_write_PUBKEY failed: " + openssl_error_string();
                break;
            }
        }

        std::vector<unsigned char> pubkey;
        if (!get_raw_public_key_from_pkey(pkey, pubkey, err))
        {
            break;
        }

        std::string pub_hex_str = hex_encode(pubkey);
        std::string address;
        if (!derive_address_from_public_key_hex(pub_hex_str, address, err))
        {
            break;
        }

        if (!write_text_file(public_hex, pub_hex_str, err)) break;
        if (!write_text_file(address_txt, address, err)) break;

        out.private_pem_path = private_pem.string();
        out.public_pem_path = public_pem.string();
        out.public_hex_path = public_hex.string();
        out.address_path = address_txt.string();
        out.public_key_hex = pub_hex_str;
        out.address = address;

        ok = true;
    }
    while (false);

    if (pkey) EVP_PKEY_free(pkey);
    EVP_PKEY_CTX_free(pctx);
    return ok;
}