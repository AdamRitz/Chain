#ifndef CHAIN_TIME_H
#define CHAIN_TIME_H
#include <chrono>
#include <vector>
#include "../Tool/Tool.h"
using namespace std;
using namespace std::chrono;

uint64_t GetSteadyTime() {
    return duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count();
}
// 时间消息只用于观测，不修改出块高度，也不决定共识确认。
vector<uint8_t> GetTime() {
    vector<uint8_t> data(8);
    WriteU64(data.data(),duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count());
    return data;
}
void InitTime() {}
#endif
