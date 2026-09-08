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
