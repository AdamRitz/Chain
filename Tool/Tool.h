//
// Created by DonQuixote on 2026/5/5.
//

#ifndef CHAIN_TOOL_H
#define CHAIN_TOOL_H
#include <stdio.h>
#include <array>
#include <cstdint>
using namespace std;
void PrintHexForArray32(const array<uint8_t, 32>& data) {
    cout<<"0x";
    for (auto i:data) {
        cout<<hex<<setw(2)<<setfill('0')<<int(i);
    }
}
void PrintHexForString(const std::string& s) {
    for (unsigned char c : s) {
        std::cout << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(c);
    }
    std::cout << std::dec << std::endl;
}
#endif //CHAIN_TOOL_H