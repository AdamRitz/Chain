//
// Created by 61485 on 2026/5/8.
//

#ifndef CHAIN_TIME_H
#define CHAIN_TIME_H
#include <chrono>
#include <iostream>
using namespace  std;
using namespace  std::chrono;

steady_clock::time_point timer;
system_clock::time_point localTime;

void InitTime()
{
    localTime = system_clock::now();
    timer = steady_clock::now();
}

long long SyncTime(vector<array<uint8_t,8>> timeVec){
    int size = timeVec.size();
    vector<long long> temp;
    // 更新时间重新计时
    auto duration = steady_clock::now() - timer;
    timer = steady_clock::now();
    // 更新本地时间
    localTime = localTime+duration;
    long long localTimeValue = localTime.time_since_epoch().count();
    // 比较最接近的时间
    long long minDiff = INT64_MAX;

    for (auto i : timeVec) {
        long long timei ;
        memcpy(&timei, &i, 8);
        temp.push_back(timei);
    }
    sort(temp.begin(), temp.end());
    return temp[size/2];
}

vector<uint8_t> GetTime() {
    long long  timeValue =duration_cast<nanoseconds>( localTime.time_since_epoch()).count();
    vector<uint8_t> data;
    data.resize(8);
    memcpy(data.data(),&timeValue,8);
    return data;
}

void TestTime() {
    int a;
   auto p =  make_shared<int>(a);
}
#endif //CHAIN_TIME_H