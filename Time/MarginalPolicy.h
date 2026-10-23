#ifndef CHAIN_MARGINAL_POLICY_H
#define CHAIN_MARGINAL_POLICY_H
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <deque>

// B3 learns a local retained transaction-count advantage, not counterfactual
// global TPS. Inputs and action thresholds are frozen before an experiment.
using MarginalFeatures=std::array<double,12>;
struct MarginalDecision {
    bool defer=false;
    const char* reason="no_alternative";
    double prediction=0,upperGain=0,cost=0,margin=0;
};
struct CandidateMarginalModel {
    MarginalFeatures weights{};
    std::deque<double> errors;
    uint64_t samples=0;
    unsigned consecutiveSkips=0;
    static MarginalFeatures Features(size_t ready,size_t maximum,size_t pendingTx,size_t pendingBlock,size_t metadata,
            int64_t prepare,int64_t headAge,size_t alternative,double overlap,int64_t remaining) {
        auto cap=double(std::max(size_t(1),maximum));
        auto gain=double(std::min(ready,maximum)>alternative?std::min(ready,maximum)-alternative:0)/cap;
        return {1.,std::min(2.,std::log2(1.+ready/cap)/8.),
            std::min(2.,std::log2(1.+double(pendingTx))/16.),std::min(2.,std::log2(1.+double(pendingBlock))/16.),
            std::min(2.,std::log2(1.+double(metadata))/6.),std::clamp(prepare/100000.,0.,2.),
            std::clamp(headAge/200000.,0.,2.),std::min(2.,alternative/cap),gain,std::clamp(overlap,0.,1.),
            double(alternative>0),std::clamp(remaining/100000.,0.,2.)};
    }
    double Predict(const MarginalFeatures& features) const {
        double score=0; for (size_t i=0;i<features.size();++i) score+=features[i]*weights[i];
        return 1./(1.+std::exp(-std::clamp(score,-12.,12.)));
    }
    void Observe(const MarginalFeatures& features,double target,double decisionPrediction) {
        if (!std::isfinite(target)||target<0||target>1||!std::isfinite(decisionPrediction)) return;
        for (auto feature:features) if (!std::isfinite(feature)) return;
        auto prediction=Predict(features);
        for (size_t i=0;i<features.size();++i) weights[i]=std::clamp(weights[i]+.05*(target-prediction)*features[i],-4.,4.);
        errors.push_back(std::abs(target-decisionPrediction)); if (errors.size()>128) errors.pop_front();
        ++samples;
    }
    MarginalDecision Decide(const MarginalFeatures& features,size_t alternative,size_t maximum,
            int64_t prepare,int64_t remaining,int64_t period,bool compatible) const {
        MarginalDecision result; result.prediction=Predict(features); result.upperGain=features[8];
        if (!compatible||!alternative) return result;
        if (consecutiveSkips>=2) { result.reason="forced_attempt"; return result; }
        if (prepare<5000||remaining<=0||period<=0) { result.reason="cheap_or_closed"; return result; }
        // A full alternative cannot be beaten in local transaction count even
        // if the pool fills while waiting. This is a scheduling rule, not a
        // claim that equal-size candidates have identical effects or hashes.
        if (alternative>=maximum) { result.defer=true; result.reason="full_alternative"; return result; }
        if (samples<16) { result.reason="warming_up"; return result; }
        for (auto error:errors) result.margin+=error;
        result.margin=.05+(errors.empty()?1.:result.margin/errors.size());
        // Explicit opportunity-cost heuristic: at most 10% of one full block.
        // Do not label this score as measured saved CPU or additional TPS.
        result.cost=.1*std::min(1.,double(alternative)/std::max(size_t(1),maximum))*std::min(1.,double(prepare)/period);
        result.defer=result.prediction+result.margin<result.cost;
        result.reason=result.defer?"low_marginal_value":"potential_gain";
        return result;
    }
};
#endif
