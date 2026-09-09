#ifndef CHAIN_MESSAGE_H
#define CHAIN_MESSAGE_H
#include <span>
#include "../DB/DB.h"
#include "../Time/Time.h"
using namespace std;

constexpr size_t maxMessageSize=112+6000*176;
vector<uint8_t> GenerateMessage(uint8_t type,span<const uint8_t> payload) {
    if (payload.size()>maxMessageSize) throw invalid_argument("Message too large");
    vector<uint8_t> message(5+payload.size());
    message[0]=type;
    WriteU32(message.data()+1,uint32_t(payload.size()));
    if (!payload.empty()) memcpy(message.data()+5,payload.data(),payload.size());
    return message;
}
bool VerifyMessageSize(uint8_t type,uint32_t size) {
    if (size>maxMessageSize) return false;
    switch (type) {
        case 1:return size==176;
        case 2:case 7:return size>=112&&(size-112)%176==0;
        case 4:case 8:case 13:return size==0;
        case 5:case 6:case 12:return size==8;
        case 9:return size%6==0&&size<=64*6;
        case 10:return size>0&&size%176==0&&size<=6000*176;
        case 11:return size==16;
        case 14:return size<=16384;
        case 15:return size==32;
        case 16:return size==17;
        default:return false;
    }
}
vector<uint8_t> GenerateNewBlockMessage(const vector<uint8_t>& block) { return GenerateMessage(2,block); }
vector<uint8_t> GenerateHeightMessage(uint8_t type=12) {
    array<uint8_t,8> data;
    WriteU64(data.data(),DBReadBlockHeight());
    return GenerateMessage(type,data);
}
vector<uint8_t> GenerateBlockMessage(uint64_t height) {
    auto block=DBReadBlockByHeight(to_string(height));
    if (block.empty()) return {};
    return GenerateMessage(7,block);
}
vector<uint8_t> GenerateRequestDiscoveryMessage() { return GenerateMessage(8,{}); }
vector<uint8_t> GenerateTxTimeACKMessage(const vector<uint8_t>& T2) {
    if (T2.size()!=8) throw invalid_argument("Invalid timestamp");
    array<uint8_t,16> data;
    auto T3=GetTime();
    memcpy(data.data(),T2.data(),8);
    memcpy(data.data()+8,T3.data(),8);
    return GenerateMessage(11,data);
}
#endif
