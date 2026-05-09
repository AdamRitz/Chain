//
// Created by 61485 on 2026/5/8.
//

#ifndef CHAIN_TIME_H
#define CHAIN_TIME_H
#include <chrono>
#include <iostream>
using namespace  std;
chrono::steady_clock::time_point timer;

void InitTime()
{
    localTime = chrono::system_clock::now();
    timer = chrono::steady_clock::now();
}

void QueryTime() {

}

void SyncTime() {

}

void SendTime() {

}
#endif //CHAIN_TIME_H