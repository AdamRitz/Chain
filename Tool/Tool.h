#ifndef CHAIN_TOOL_H
#define CHAIN_TOOL_H
#include <array>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
using namespace std;

struct GetMapHash {
    size_t operator()(const array<uint8_t,32>& data) const noexcept {
        size_t hash=0;
        memcpy(&hash,data.data(),sizeof(hash));
        return hash;
    }
};
array<uint8_t,32> HexToU32(const string& text) {
    if (text.size()!=64) throw invalid_argument("Public key must contain 64 hexadecimal characters");
    array<uint8_t,32> result{};
    auto digit=[](char c) -> uint8_t {
        if (c>='0'&&c<='9') return uint8_t(c-'0');
        if (c>='a'&&c<='f') return uint8_t(c-'a'+10);
        if (c>='A'&&c<='F') return uint8_t(c-'A'+10);
        throw invalid_argument("Invalid hexadecimal public key");
    };
    for (size_t i=0;i<32;i++) result[i]=uint8_t((digit(text[i*2])<<4)|digit(text[i*2+1]));
    return result;
}

// 网络和数据库统一使用小端整数。
void WriteU64(uint8_t* data,uint64_t value) {
    for (int i=0;i<8;i++) data[i]=uint8_t(value>>(i*8));
}
uint64_t ReadU64(const uint8_t* data) {
    uint64_t value=0;
    for (int i=0;i<8;i++) value|=uint64_t(data[i])<<(i*8);
    return value;
}
void WriteU32(uint8_t* data,uint32_t value) {
    for (int i=0;i<4;i++) data[i]=uint8_t(value>>(i*8));
}
uint32_t ReadU32(const uint8_t* data) {
    uint32_t value=0;
    for (int i=0;i<4;i++) value|=uint32_t(data[i])<<(i*8);
    return value;
}
string U32ToHex(const array<uint8_t,32>& data) {
    stringstream ss;
    for (auto i:data) ss<<hex<<setw(2)<<setfill('0')<<int(i);
    return ss.str();
}
void PrintHexForArray32(const array<uint8_t,32>& data) { cout<<"0x"<<U32ToHex(data); }
void PrintHexForString(const string& data) {
    for (uint8_t i:data) cout<<hex<<setw(2)<<setfill('0')<<int(i);
    cout<<dec<<endl;
}
#endif
