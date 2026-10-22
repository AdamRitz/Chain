#ifndef CHAIN_ADAPTIVE_POLICY_H
#define CHAIN_ADAPTIVE_POLICY_H
#include <array>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <deque>
#include <optional>
#include <vector>

// Main-loop-owned state. These predictions never decide block validity.
struct AdaptivePreparation {
    struct Key {
        uint64_t state=0,features=0,work=0;
        int64_t round=0;
        size_t ready=0,tx=0,block=0;
        bool operator==(const Key&) const = default;
    };
    Key key;
    bool cached=false,modelUsed=false;
    int64_t cachedAt=0,rawUs=1000;
    std::array<double,32> features{};
    std::array<std::deque<double>,2> residuals;
    static size_t QueueBucket(size_t value) {
        size_t bucket=0; while (value>0) { value>>=1; ++bucket; } return bucket;
    }
    bool Hit(const Key& next,int64_t now) const {
        return cached&&key==next&&now>=cachedAt&&now-cachedAt<10000;
    }
    int64_t Budget(size_t bucket) const {
        const auto& history=residuals.at(bucket);
        if (!modelUsed||history.size()<16) return rawUs;
        std::vector<double> sorted(history.begin(),history.end()); std::sort(sorted.begin(),sorted.end());
        auto correction=sorted[size_t(std::ceil(.95*(sorted.size()-1)))];
        auto bound=std::min(10000.0,.25*double(rawUs));
        return int64_t(std::ceil(std::max(1000.0,double(rawUs)+std::clamp(correction,-bound,bound))));
    }
    void Observe(size_t bucket,int64_t prediction,int64_t actual,bool complete) {
        if (!complete||!modelUsed||prediction<1000||actual<0||actual>1000000) return;
        auto& history=residuals.at(bucket); history.push_back(double(actual-prediction));
        if (history.size()>128) history.pop_front();
    }
};

// Two small prequential logistic heads: the parent branch survives a delayed
// observation; conditional on that, the candidate is on the local canonical chain.
// No counterfactual label is assigned to a deferred candidate.
using CandidateValueFeatures=std::array<double,8>;
struct CandidateOutcomeHead {
    CandidateValueFeatures weights{};
    uint64_t samples=0;
    double loss=0,brier=0;
    double Predict(const CandidateValueFeatures& x) const {
        double score=0; for (size_t i=0;i<x.size();++i) score+=weights[i]*x[i];
        return 1/(1+std::exp(-std::clamp(score,-12.0,12.0)));
    }
    void Observe(const CandidateValueFeatures& x,bool success) {
        auto p=Predict(x),y=double(success);
        loss+=-(y*std::log(p)+(1-y)*std::log(1-p)); brier+=(p-y)*(p-y);
        // Fixed learning rule; every update uses only a fully observed past event.
        for (size_t i=0;i<x.size();++i) weights[i]=std::clamp(weights[i]+.08*(y-p)*x[i],-4.0,4.0);
        ++samples;
    }
};
struct CandidateValueModel {
    CandidateOutcomeHead survival,retention;
    unsigned consecutiveSkips=0;
    static CandidateValueFeatures Features(int64_t prepare,int64_t untilOffer,int64_t headAge,
            size_t ready,size_t maximum,size_t pending,bool alternative) {
        return {1.,std::clamp(prepare/100000.,0.,2.),std::clamp(untilOffer/100000.,0.,2.),
            std::clamp(headAge/100000.,0.,2.),double(std::min(ready,maximum))/std::max(size_t(1),maximum),
            std::min(2.,std::log1p(double(pending))/4.),double(alternative),double(ready>=maximum)};
    }
    bool ShouldDefer(const CandidateValueFeatures& x,bool alternative,int64_t prepare,int64_t remaining) const {
        if (!alternative||consecutiveSkips>=2||survival.samples<12||retention.samples<12||prepare<5000||remaining<=0) return false;
        auto useful=survival.Predict(x)*retention.Predict(x);
        // A meaningful expensive alternative is required, evaluated against the
        // selected future round, never the already-closed current round.
        return useful<.35&&prepare>=remaining/4;
    }
};
#endif
