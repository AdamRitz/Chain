//
// Created by 61485 on 2026/5/9.
//

#ifndef CHAIN_MESSAGE_H
#define CHAIN_MESSAGE_H
#include <cstdint>
#include <iostream>
#include <vector>
#include "../DB/DB.h"
#include "../Network/Client.h"
using namespace std;
vector<uint8_t> GenerateBlockMessage(const uint64_t& blockNum) {
    auto block = DBReadBlockByHeight(to_string(blockNum));
    vector<uint8_t> byte{};
    uint32_t length = block.size()+1+4+8;
    byte.resize(length);
    // 填充 type
    uint8_t messageType = 7;
    int offset = 0;
    memcpy(byte.data()+offset, &messageType, 1);
    offset += 1;
    // 填充 length
    memcpy(byte.data()+offset,&length,4);
    offset += 4;
    // 填充 blockNum
    memcpy(byte.data()+offset,&blockNum,8);
    offset += 8;
    // 填充 blockByte
    memcpy(byte.data()+offset, block.data() , block.size());
    return byte;
}

vector<uint8_t> GenerateRequestDiscoveryMessage() {
    vector<uint8_t> message;
    message.resize(5);
    // 填充 type
    uint8_t type = 8;
    int offset = 0;
    memcpy(message.data()+offset,&type,1);
    offset += 1;
    // 填充 length
    uint32_t length = 0;
    memcpy(message.data()+offset,&length,4);
    return message;
};

#endif //CHAIN_MESSAGE_H