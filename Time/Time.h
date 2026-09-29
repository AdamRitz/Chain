#ifndef CHAIN_TIME_H
#define CHAIN_TIME_H
#include <algorithm>
#include <chrono>
#include <cmath>
#include <map>
#include <mutex>
#include <vector>
#include <nlohmann/json.hpp>
#include "../Tool/Tool.h"
using namespace std;
using namespace std::chrono;

uint64_t GetSteadyTime() {
    return duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count();
}
int64_t GetWallTimeUs() {
    return duration_cast<microseconds>(system_clock::now().time_since_epoch()).count();
}
// 实验只调整应用逻辑时钟，系统时钟和性能计时保持独立。
struct ClockState {
    mutex lock;
    int64_t wallStart=0,steadyStart=0,offsetUs=0,correctionUs=0;
    double driftPpm=0,gain=0.25;
    bool enabled=false;
    int periodMs=250,roundMs=0,candidateMs=0;
    uint64_t samples=0,rejected=0;
    map<uint64_t,pair<int64_t,int64_t>> offsets;
};
ClockState nodeClock;
int64_t GetRawClockUsLocked() {
    auto elapsed=int64_t(GetSteadyTime()/1000)-nodeClock.steadyStart;
    return nodeClock.wallStart+elapsed+nodeClock.offsetUs+int64_t(elapsed*nodeClock.driftPpm/1e6);
}
int64_t GetLogicalTimeUs() {
    lock_guard lock(nodeClock.lock);
    return GetRawClockUsLocked()+nodeClock.correctionUs;
}
void InitTime() {
    lock_guard lock(nodeClock.lock);
    nodeClock.wallStart=GetWallTimeUs(); nodeClock.steadyStart=int64_t(GetSteadyTime()/1000);
    nodeClock.correctionUs=0; nodeClock.samples=0; nodeClock.rejected=0; nodeClock.offsets.clear();
}
// 四时间戳去除对端处理时间；按邻居分别保留最新偏差，避免快节点重复加权。
bool RecordClockSample(uint64_t peer,int64_t t1,int64_t t2,int64_t t3,int64_t t4,int64_t elapsedUs) {
    lock_guard lock(nodeClock.lock);
    if (abs(double(t2)-double(t1))>20000000||abs(double(t3)-double(t1))>20000000) {
        nodeClock.rejected++; return false;
    }
    auto processing=t3-t2,delay=elapsedUs-processing;
    if (processing<0||delay<0||delay>1000000||abs(t4-t1-elapsedUs)>10000) { nodeClock.rejected++; return false; }
    auto offset=((t2-t1)+(t3-t4))/2;
    if (abs(offset)>10000000) { nodeClock.rejected++; return false; }
    nodeClock.offsets[peer]={offset+nodeClock.correctionUs,int64_t(GetSteadyTime()/1000)};
    nodeClock.samples++;
    return true;
}
void AdjustClock() {
    lock_guard lock(nodeClock.lock);
    vector<int64_t> offsets{0};
    auto now=int64_t(GetSteadyTime()/1000);
    for (auto it=nodeClock.offsets.begin();it!=nodeClock.offsets.end();) {
        if (now-it->second.second>int64_t(nodeClock.periodMs)*4000) it=nodeClock.offsets.erase(it);
        else { offsets.push_back(it->second.first-nodeClock.correctionUs); ++it; }
    }
    if (!nodeClock.enabled||offsets.size()<2) return;
    sort(offsets.begin(),offsets.end());
    auto middle=offsets.size()/2;
    double median=offsets.size()%2?double(offsets[middle]):(double(offsets[middle-1])+offsets[middle])/2;
    nodeClock.correctionUs+=int64_t(clamp(median*nodeClock.gain,-100000.0,100000.0));
}
nlohmann::json GetClockStats() {
    lock_guard lock(nodeClock.lock);
    auto raw=GetRawClockUsLocked();
    return {{"logical_us",raw+nodeClock.correctionUs},{"raw_us",raw},{"correction_us",nodeClock.correctionUs},
        {"injected_offset_us",nodeClock.offsetUs},{"drift_ppm",nodeClock.driftPpm},{"enabled",nodeClock.enabled},
        {"samples",nodeClock.samples},{"rejected_samples",nodeClock.rejected},{"round_ms",nodeClock.roundMs},
        {"candidate_ms",nodeClock.candidateMs},{"algorithm","four_timestamp_median_coupling"}};
}
vector<uint8_t> GetTime() {
    vector<uint8_t> data(8);
    WriteU64(data.data(),duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count());
    return data;
}
#endif
