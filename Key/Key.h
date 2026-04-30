#pragma once

#include <string>
#include <vector>

struct LocalKeyInfo
{
    std::string private_pem_path;
    std::string public_pem_path;
    std::string public_hex_path;
    std::string address_path;

    std::string public_key_hex;
    std::string address;
};

bool generate_and_store_keypair(const std::string& dir, LocalKeyInfo& out, std::string& err);

bool load_public_key_hex_from_pem(const std::string& public_pem_path,
                                  std::string& public_key_hex,
                                  std::string& err);

bool get_public_key_hex_from_private_pem(const std::string& private_pem_path,
                                         std::string& public_key_hex,
                                         std::string& err);

bool derive_address_from_public_key_hex(const std::string& public_key_hex,
                                        std::string& address,
                                        std::string& err);

bool sign_message_with_private_key(const std::string& private_pem_path,
                                   const std::vector<unsigned char>& message,
                                   std::vector<unsigned char>& signature,
                                   std::string& err);

bool verify_message_with_public_key_hex(const std::string& public_key_hex,
                                        const std::vector<unsigned char>& message,
                                        const std::vector<unsigned char>& signature,
                                        std::string& err);

std::string hex_encode(const std::vector<unsigned char>& data);
bool hex_decode(const std::string& hex, std::vector<unsigned char>& out, std::string& err);