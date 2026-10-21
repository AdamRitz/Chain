#ifndef CHAIN_CLOCK_CONTROL_H
#define CHAIN_CLOCK_CONTROL_H
#include <algorithm>
#include <cmath>
#include <cstdint>

struct ClockMapping {
    int64_t steadyUs=0,logicalUs=0;
    double rate=1;
    uint64_t version=0;
    int64_t ToSteady(int64_t logical) const { return steadyUs+int64_t(std::ceil((logical-logicalUs)/rate)); }
};
// A continuous piecewise-affine application clock. No OS clock writes or negative steps.
struct ClockServo {
    int64_t steadyUs=0,validUntilUs=0;
    long double logicalUs=0;
    double rawDriftPpm=0,slewPpm=0,maxSlewPpm=1000;
    uint64_t version=0;
    void Init(int64_t now,int64_t logical,double drift,double limit) {
        steadyUs=now; logicalUs=logical; rawDriftPpm=drift; maxSlewPpm=limit; slewPpm=0; validUntilUs=now; version=0;
    }
    double Rate() const { return 1+(rawDriftPpm+slewPpm)/1e6; }
    int64_t Advance(int64_t now) {
        now=std::max(now,steadyUs);
        if (slewPpm&&now>=validUntilUs) {
            auto boundary=std::max(steadyUs,validUntilUs);
            logicalUs+=(boundary-steadyUs)*static_cast<long double>(Rate()); steadyUs=boundary;
            slewPpm=0; version++;
        }
        logicalUs+=(now-steadyUs)*static_cast<long double>(Rate()); steadyUs=now;
        return int64_t(logicalUs);
    }
    void SetRate(int64_t now,double requested,int64_t expiry) {
        Advance(now);
        auto bounded=std::clamp(requested,-maxSlewPpm,maxSlewPpm);
        if (bounded!=slewPpm) { slewPpm=bounded; version++; }
        validUntilUs=expiry;
    }
    ClockMapping Snapshot(int64_t now) { auto logical=Advance(now); return {steadyUs,logical,Rate(),version}; }
};
#endif
