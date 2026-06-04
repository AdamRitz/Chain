//
// Created by 61485 on 2026/5/8.
//

#ifndef CHAIN_TIME_H
#define CHAIN_TIME_H
#include <chrono>
#include <iostream>
using namespace  std;
using namespace  std::chrono;

// 计时器
steady_clock::time_point Counter;
// 被同步的时间，这个时间只用于初始化
system_clock::time_point localTime;

void InitTime()
{
    localTime = system_clock::now();
    Counter = steady_clock::now();
}

long long SyncTime(vector<array<uint8_t,8>> timeVec){
    int size = timeVec.size();
    vector<long long> temp;
    // 更新时间重新计时
    auto duration = steady_clock::now() - Counter;
    Counter = steady_clock::now();
    // 更新本地时间
    localTime = localTime+duration;
    long long localTimeValue = localTime.time_since_epoch().count();
    // 对收到的时间戳进行排序
    for (auto i : timeVec) {
        long long timei ;
        memcpy(&timei, &i, 8);
        temp.push_back(timei);
    }
    sort(temp.begin(), temp.end());
    // 更新本地时间为中位数的时间


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