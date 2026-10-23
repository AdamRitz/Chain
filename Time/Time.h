#ifndef CHAIN_TIME_H
#define CHAIN_TIME_H
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <deque>
#include <set>
#include <map>
#include <mutex>
#include <vector>
#include <nlohmann/json.hpp>
#include "../Tool/Tool.h"
#include "ClockModel.h"
#include "Round.h"
#include "CandidatePolicy.h"
#include "MarginalPolicy.h"
#include "AdaptivePolicy.h"
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
    string control="legacy",scheduler="legacy";
    bool joinCurrentRound=true;
    bool candidateTiming=false,candidateRisk=false;
    bool candidateAdaptive=false,candidateLifecycle=false;
    int candidateValue=0; // 0 disabled, 1 shadow, 2 bounded action.
    int candidateMarginal=0; // B3: 0 disabled, 1 shadow, 2 bounded action.
    uint64_t marginalPredictions=0,marginalSuggestions=0,marginalDeferrals=0,marginalRuleSuggestions=0;
    uint64_t marginalLabels=0,marginalCensored=0;
    double marginalAbsoluteError=0;
    uint64_t workVersion=0,decisionCacheHits=0,decisionInferenceNs=0,decisionBudgetNs=0;
    uint64_t valuePredictions=0,valueSuggestions=0,valueDeferrals=0,valueSurvivalLabels=0,valueRetentionLabels=0,valueCensored=0;
    double valueSurvivalBrier=0,valueRetentionBrier=0;
    uint64_t decisionPredictions=0,decisionFallbacks=0,riskDeferrals=0;
    double maxSlewPpm=1000,prepareQuantile=.95;
    int minPeers=3,modelPeriodMs=500,modelTtlMs=2000;
    int64_t guardUs=500;
    set<uint64_t> members;
    ClockServo servo;
    struct Source { double target=0,rtt=0,jitter=0; int64_t at=0; };
    map<uint64_t,Source> sources;
    struct Work { int64_t at; double duration; bool late; };
    deque<Work> preparations,commits;
    struct Frame { int64_t at; double mad,rtt,prepare,commit,late; };
    deque<Frame> history;
    ClockFeatures features{};
    ClockModel model;
    ClockPrediction prediction;
    uint64_t featureVersion=0,modelRuns=0,modelFallbacks=0,unboundSamples=0;
    uint64_t rounds=0,joinedRounds=0,skippedRounds=0,budgetSkips=0,lateCandidates=0,staleCandidates=0,cancelledCandidates=0;
    uint64_t inferenceNs=0,maxInferenceNs=0;
    uint64_t learnedPrepareUses=0,learnedPrepareReductions=0;
    double selectedPrepareUs=1000,selectedTimeUs=1000000;
    atomic<uint64_t> traceDropped{0};
    int64_t featureAt=0,predictionAt=0,lastAdjust=0,lastSamples=0;
    double center=0,lastCenter=0,mad=0,driftEstimatePpm=0,baselineTimeUs=1000000,baselinePrepareUs=1000;
    string fallback="warming_up",tracePath;
    ofstream trace;
    mutex traceLock;
    deque<nlohmann::json> traceQueue;
    uint64_t traceRecords=0;
};
ClockState nodeClock;
int64_t GetRawClockUsLocked() {
    auto elapsed=int64_t(GetSteadyTime()/1000)-nodeClock.steadyStart;
    return nodeClock.wallStart+elapsed+nodeClock.offsetUs+int64_t(elapsed*nodeClock.driftPpm/1e6);
}
int64_t GetLogicalTimeUs() {
    lock_guard lock(nodeClock.lock);
    if (nodeClock.control!="legacy") return nodeClock.servo.Advance(int64_t(GetSteadyTime()/1000));
    return GetRawClockUsLocked()+nodeClock.correctionUs;
}
ClockMapping GetClockMapping() {
    lock_guard lock(nodeClock.lock);
    auto now=int64_t(GetSteadyTime()/1000);
    if (nodeClock.control!="legacy") return nodeClock.servo.Snapshot(now);
    return {now,GetRawClockUsLocked()+nodeClock.correctionUs,1+nodeClock.driftPpm/1e6,0};
}
void WriteClockTrace(nlohmann::json row) {
    if (nodeClock.tracePath.empty()) return;
    unique_lock lock(nodeClock.traceLock,try_to_lock);
    if (!lock||nodeClock.traceQueue.size()>=1024) { nodeClock.traceDropped++; return; }
    row["schema"]=1; nodeClock.traceQueue.push_back(std::move(row));
}
void FlushClockTrace() {
    deque<nlohmann::json> rows;
    { lock_guard lock(nodeClock.traceLock); rows.swap(nodeClock.traceQueue); }
    for (const auto& row:rows) {
        if (nodeClock.traceRecords>=100000) { nodeClock.traceDropped++; continue; }
        nodeClock.trace<<row.dump()<<'\n'; nodeClock.traceRecords++;
        if (!nodeClock.trace) { nodeClock.traceDropped++; nodeClock.trace.clear(); }
    }
    if (!rows.empty()) nodeClock.trace.flush();
}
void InitTime() {
    lock_guard lock(nodeClock.lock);
    nodeClock.wallStart=GetWallTimeUs(); nodeClock.steadyStart=int64_t(GetSteadyTime()/1000);
    nodeClock.correctionUs=0; nodeClock.samples=0; nodeClock.rejected=0; nodeClock.offsets.clear();
    nodeClock.sources.clear(); nodeClock.history.clear(); nodeClock.preparations.clear(); nodeClock.commits.clear();
    nodeClock.servo.Init(nodeClock.steadyStart,nodeClock.wallStart+nodeClock.offsetUs,nodeClock.driftPpm,nodeClock.maxSlewPpm);
    nodeClock.featureAt=0; nodeClock.predictionAt=0; nodeClock.lastAdjust=0; nodeClock.featureVersion=0;
    nodeClock.modelRuns=0; nodeClock.modelFallbacks=0; nodeClock.lastSamples=0; nodeClock.driftEstimatePpm=0;
    nodeClock.learnedPrepareUses=0; nodeClock.learnedPrepareReductions=0;
    if (!nodeClock.tracePath.empty()) {
        nodeClock.trace.open(nodeClock.tracePath,ios::out|ios::trunc);
        if (!nodeClock.trace) throw runtime_error("Cannot open clock trace");
    }
}
// 四时间戳去除对端处理时间；按邻居分别保留最新偏差，避免快节点重复加权。
bool RecordClockSample(uint64_t peer,int64_t t1,int64_t t2,int64_t t3,int64_t t4,int64_t elapsedUs) {
    lock_guard lock(nodeClock.lock);
    if (abs(double(t2)-double(t1))>20000000||abs(double(t3)-double(t1))>20000000) {
        nodeClock.rejected++; return false;
    }
    // Convert before subtracting untrusted int64 timestamps to avoid signed overflow.
    double processing=double(t3)-double(t2),delay=elapsedUs-processing;
    if (elapsedUs<0||processing<0||delay<0||delay>1000000||abs(double(t4)-double(t1)-elapsedUs)>10000) { nodeClock.rejected++; return false; }
    auto offset=int64_t(((double(t2)-double(t1))+(double(t3)-double(t4)))/2);
    if (abs(offset)>10000000) { nodeClock.rejected++; return false; }
    if (nodeClock.control!="legacy") {
        if (!nodeClock.members.count(peer)) { nodeClock.unboundSamples++; nodeClock.rejected++; return false; }
        auto now=int64_t(GetSteadyTime()/1000);
        auto raw=GetRawClockUsLocked(); auto logical=nodeClock.servo.Advance(now);
        auto target=offset+double(logical-raw);
        auto found=nodeClock.sources.find(peer);
        double jitter=found==nodeClock.sources.end()?0:0.75*found->second.jitter+0.25*abs(target-found->second.target);
        // Configured endpoint identity gives one contribution despite duplicate TCP sessions.
        nodeClock.sources[peer]={target,delay,jitter,now}; nodeClock.samples++;
        return true;
    }
    nodeClock.offsets[peer]={offset+nodeClock.correctionUs,int64_t(GetSteadyTime()/1000)};
    nodeClock.samples++;
    return true;
}
double ClockPercentile(vector<double> values,double quantile) {
    if (values.empty()) return 0;
    sort(values.begin(),values.end());
    return values[size_t(ceil(quantile*(values.size()-1)))];
}
void RecordClockWork(bool prepare,int64_t durationUs,bool late=false) {
    if (nodeClock.control=="legacy") return;
    lock_guard lock(nodeClock.lock);
    auto& work=prepare?nodeClock.preparations:nodeClock.commits;
    work.push_back({int64_t(GetSteadyTime()/1000),double(max(int64_t(0),durationUs)),late});
    if (work.size()>256) work.pop_front();
    ++nodeClock.workVersion;
}
ClockFeatures BuildClockFeaturesLocked(int64_t now,double cpu,size_t pendingTx,size_t pendingBlock,size_t ready) {
    vector<double> targets,rtts,jitters,ages;
    for (auto it=nodeClock.sources.begin();it!=nodeClock.sources.end();) {
        if (now-it->second.at>int64_t(nodeClock.periodMs)*4000) it=nodeClock.sources.erase(it);
        else {
            targets.push_back(it->second.target); rtts.push_back(it->second.rtt); jitters.push_back(it->second.jitter);
            ages.push_back(double(now-it->second.at)); ++it;
        }
    }
    auto correction=double(nodeClock.servo.Advance(now)-GetRawClockUsLocked());
    auto center=ClockPercentile(targets,0.5);
    vector<double> deviations; for (auto value:targets) deviations.push_back(abs(value-center));
    auto mad=ClockPercentile(deviations,0.5),rtt=ClockPercentile(rtts,0.95);
    auto WorkStats=[&](deque<ClockState::Work>& work) {
        while (!work.empty()&&now-work.front().at>30000000) work.pop_front();
        vector<double> durations; double late=0;
        for (const auto& item:work) { durations.push_back(item.duration); late+=item.late; }
        return pair{ClockPercentile(durations,0.95),work.empty()?0:late/work.size()};
    };
    auto [prepare,late]=WorkStats(nodeClock.preparations); auto [commit,unused]=WorkStats(nodeClock.commits); (void)unused;
    nodeClock.baselineTimeUs=targets.size()<size_t(nodeClock.minPeers)?1000000:max(250.0,abs(center-correction)+3*mad+rtt/2);
    nodeClock.baselinePrepareUs=max(1000.0,prepare);
    nodeClock.center=center; nodeClock.mad=mad;
    auto dt=nodeClock.featureAt?now-nodeClock.featureAt:0;
    auto delta=dt?center-nodeClock.lastCenter:0;
    if (dt>0&&targets.size()>=size_t(nodeClock.minPeers)) {
        auto drift=clamp(delta*1e6/dt,-5000.0,5000.0);
        nodeClock.driftEstimatePpm=0.9*nodeClock.driftEstimatePpm+0.1*drift;
    }
    double sampleRate=dt?double(nodeClock.samples-nodeClock.lastSamples)*1e6/dt:0;
    ClockFeatures features{double(targets.size()),abs(center-correction)/1000,mad/1000,
        targets.empty()?0:(*max_element(targets.begin(),targets.end())-*min_element(targets.begin(),targets.end()))/1000,
        ClockPercentile(rtts,0.5)/1000,rtt/1000,ClockPercentile(jitters,0.5)/1000,
        ClockPercentile(ages,1)/1000,abs(delta)/1000,
        double(nodeClock.rejected)/max(1.0,double(nodeClock.samples+nodeClock.rejected)),max(0.0,cpu),
        double(pendingTx),double(pendingBlock),double(ready),prepare/1000,commit/1000};
    nodeClock.history.push_back({now,mad/1000,rtt/1000,prepare/1000,commit/1000,late});
    while (!nodeClock.history.empty()&&(now-nodeClock.history.front().at>30000000||nodeClock.history.size()>301)) nodeClock.history.pop_front();
    size_t index=16;
    for (auto seconds:{1,5,30}) {
        vector<double> m,r,p,c; double lateSum=0,count=0;
        for (const auto& frame:nodeClock.history) if (now-frame.at<=int64_t(seconds)*1000000) {
            m.push_back(frame.mad); r.push_back(frame.rtt); p.push_back(frame.prepare); c.push_back(frame.commit); lateSum+=frame.late; count++;
        }
        features[index++]=ClockPercentile(m,0.95); features[index++]=ClockPercentile(r,0.95);
        features[index++]=ClockPercentile(p,0.95); features[index++]=ClockPercentile(c,0.95); features[index++]=count?lateSum/count:0;
    }
    features[31]=sampleRate; nodeClock.lastCenter=center; nodeClock.lastSamples=nodeClock.samples;
    return features;
}
// Called by the existing low-frequency clock coroutine, never by validation or commit locks.
void UpdateClockModel(double cpu,size_t pendingTx,size_t pendingBlock,size_t ready) {
    if (nodeClock.control=="legacy") return;
    ClockFeatures features; uint64_t version; int64_t now=int64_t(GetSteadyTime()/1000),logical=0;
    {
        lock_guard lock(nodeClock.lock);
        now=int64_t(GetSteadyTime()/1000);
        if (nodeClock.featureAt&&now-nodeClock.featureAt<int64_t(nodeClock.modelPeriodMs)*1000) return;
        features=BuildClockFeaturesLocked(now,cpu,pendingTx,pendingBlock,ready);
        nodeClock.features=features; nodeClock.featureAt=now; version=++nodeClock.featureVersion;
        logical=nodeClock.servo.Advance(now);
    }
    auto start=GetSteadyTime(); auto prediction=nodeClock.model.Predict(features); auto elapsed=GetSteadyTime()-start;
    {
        lock_guard lock(nodeClock.lock);
        if (version!=nodeClock.featureVersion) return;
        nodeClock.prediction=prediction; nodeClock.predictionAt=now; nodeClock.modelRuns++;
        nodeClock.inferenceNs+=elapsed; nodeClock.maxInferenceNs=max(nodeClock.maxInferenceNs,elapsed);
        if (!prediction.valid) nodeClock.modelFallbacks++;
    }
    WriteClockTrace({{"kind","observation"},{"steady_us",now},{"feature_version",version},{"features",features},
        {"prediction_valid",prediction.valid},{"time_valid",prediction.HasTime()},{"prepare_valid",prediction.HasPrepare()},
        {"reason",prediction.reason},{"time_us",prediction.timeUs},{"prepare_us",prediction.prepareUs},
        {"model_id",nodeClock.model.id},{"model_sha256",nodeClock.model.sha256},{"logical_us",logical},{"label_source","unlabeled"}});
}
void AdjustClock() {
    lock_guard lock(nodeClock.lock);
    if (nodeClock.control!="legacy") {
        auto now=int64_t(GetSteadyTime()/1000); nodeClock.servo.Advance(now);
        vector<double> targets; int64_t expiry=INT64_MAX;
        for (const auto& [key,source]:nodeClock.sources) if (now-source.at<=int64_t(nodeClock.periodMs)*4000) {
            targets.push_back(source.target); expiry=min(expiry,source.at+int64_t(nodeClock.periodMs)*4000);
        }
        nodeClock.fallback="ok";
        if (!nodeClock.enabled||targets.size()<size_t(nodeClock.minPeers)) {
            nodeClock.fallback=nodeClock.enabled?"insufficient_fresh_sources":"sync_disabled";
            if (targets.size()<size_t(nodeClock.minPeers)) nodeClock.baselineTimeUs=1000000;
            nodeClock.servo.SetRate(now,0,now); return;
        }
        auto center=ClockPercentile(targets,0.5);
        auto correction=double(nodeClock.servo.Advance(now)-GetRawClockUsLocked());
        vector<double> deviations,rtts;
        for (auto target:targets) deviations.push_back(abs(target-center));
        for (const auto& [key,source]:nodeClock.sources) if (now-source.at<=int64_t(nodeClock.periodMs)*4000) rtts.push_back(source.rtt);
        nodeClock.baselineTimeUs=max(250.0,abs(center-correction)+3*ClockPercentile(deviations,0.5)+ClockPercentile(rtts,0.95)/2);
        double uncertainty=nodeClock.baselineTimeUs;
        if (nodeClock.control=="learned") {
            if (!nodeClock.prediction.HasTime()) nodeClock.fallback=nodeClock.prediction.reason;
            else if (now-nodeClock.predictionAt>int64_t(nodeClock.modelTtlMs)*1000) nodeClock.fallback="prediction_stale";
            else uncertainty=max(uncertainty,nodeClock.prediction.timeUs);
        }
        // The model only attenuates a robust target. Slew and freshness bounds apply in every mode.
        auto requested=(center-correction)*nodeClock.gain/(1+uncertainty/10000)+nodeClock.driftEstimatePpm;
        nodeClock.servo.SetRate(now,requested,expiry); nodeClock.lastAdjust=now;
        return;
    }
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
    auto now=int64_t(GetSteadyTime()/1000);
    auto logical=nodeClock.control=="legacy"?raw+nodeClock.correctionUs:nodeClock.servo.Advance(now);
    size_t freshSources=0;
    for (const auto& [key,source]:nodeClock.sources) if (now-source.at<=int64_t(nodeClock.periodMs)*4000) freshSources++;
    bool predictionFresh=nodeClock.prediction.valid&&now-nodeClock.predictionAt<=int64_t(nodeClock.modelTtlMs)*1000;
    auto fallback=nodeClock.fallback;
    if (nodeClock.control!="legacy"&&freshSources<size_t(nodeClock.minPeers)) fallback="insufficient_fresh_sources";
    else if (nodeClock.control=="learned"&&nodeClock.prediction.valid&&!predictionFresh) fallback="prediction_stale";
    nlohmann::json result={{"logical_us",logical},{"raw_us",raw},{"correction_us",logical-raw},
        {"injected_offset_us",nodeClock.offsetUs},{"drift_ppm",nodeClock.driftPpm},{"enabled",nodeClock.enabled},
        {"samples",nodeClock.samples},{"rejected_samples",nodeClock.rejected},{"round_ms",nodeClock.roundMs},
        {"candidate_ms",nodeClock.candidateMs},{"algorithm",nodeClock.control=="legacy"?"four_timestamp_median_coupling":"bounded_robust_slew"},
        {"control",nodeClock.control},{"scheduler",nodeClock.scheduler},{"join_current_round",nodeClock.joinCurrentRound},{"slew_ppm",nodeClock.servo.slewPpm},
        {"max_slew_ppm",nodeClock.maxSlewPpm},{"mapping_version",nodeClock.servo.version},{"min_peers",nodeClock.minPeers},
        {"member_count",nodeClock.members.size()},{"fresh_sources",freshSources},{"unbound_samples",nodeClock.unboundSamples},{"fallback",fallback},
        {"empirical_uncertainty_us",nodeClock.baselineTimeUs},{"prepare_budget_us",nodeClock.baselinePrepareUs},
        {"model_loaded",nodeClock.model.loaded},{"model_id",nodeClock.model.id},{"model_sha256",nodeClock.model.sha256},
        {"model_training_kind",nodeClock.model.trainingKind},{"prediction_valid",nodeClock.prediction.valid},{"prediction_fresh",predictionFresh},
        {"prediction_reason",nodeClock.prediction.reason},{"predicted_time_us",nodeClock.prediction.timeUs},
        {"time_prediction_valid",nodeClock.prediction.HasTime()},{"prepare_prediction_valid",nodeClock.prediction.HasPrepare()},
        {"prepare_model_loaded",nodeClock.model.preparation.loaded},{"prepare_quantile",nodeClock.prepareQuantile},
        {"candidate_timing",nodeClock.candidateTiming},{"candidate_risk",nodeClock.candidateRisk},
        {"candidate_adaptive",nodeClock.candidateAdaptive},{"candidate_value",nodeClock.candidateValue},
        {"candidate_marginal",nodeClock.candidateMarginal},{"marginal_predictions",nodeClock.marginalPredictions},
        {"marginal_suggestions",nodeClock.marginalSuggestions},{"marginal_deferrals",nodeClock.marginalDeferrals},
        {"marginal_rule_suggestions",nodeClock.marginalRuleSuggestions},{"marginal_labels",nodeClock.marginalLabels},
        {"marginal_censored",nodeClock.marginalCensored},{"marginal_absolute_error",nodeClock.marginalAbsoluteError},
        {"decision_cache_hits",nodeClock.decisionCacheHits},{"decision_inference_ms",nodeClock.decisionInferenceNs/1e6},
        {"decision_budget_ms",nodeClock.decisionBudgetNs/1e6},{"value_predictions",nodeClock.valuePredictions},
        {"value_suggestions",nodeClock.valueSuggestions},{"value_deferrals",nodeClock.valueDeferrals},
        {"value_survival_labels",nodeClock.valueSurvivalLabels},{"value_retention_labels",nodeClock.valueRetentionLabels},
        {"value_censored",nodeClock.valueCensored},{"value_survival_brier_sum",nodeClock.valueSurvivalBrier},
        {"value_retention_brier_sum",nodeClock.valueRetentionBrier},
        {"decision_predictions",nodeClock.decisionPredictions},{"decision_fallbacks",nodeClock.decisionFallbacks},{"risk_deferrals",nodeClock.riskDeferrals},
        {"learned_prepare_uses",nodeClock.learnedPrepareUses},{"learned_prepare_reductions",nodeClock.learnedPrepareReductions},
        {"selected_prepare_us",nodeClock.selectedPrepareUs},{"selected_time_us",nodeClock.selectedTimeUs},
        {"predicted_prepare_us",nodeClock.prediction.prepareUs},{"model_runs",nodeClock.modelRuns},
        {"model_fallbacks",nodeClock.modelFallbacks},{"inference_ms",nodeClock.inferenceNs/1e6},{"max_inference_us",nodeClock.maxInferenceNs/1000.0},
        {"feature_version",nodeClock.featureVersion},{"rounds",nodeClock.rounds},{"joined_current_rounds",nodeClock.joinedRounds},
        {"skipped_rounds",nodeClock.skippedRounds},{"budget_skips",nodeClock.budgetSkips},{"late_candidates",nodeClock.lateCandidates},
        {"stale_candidates",nodeClock.staleCandidates},{"cancelled_candidates",nodeClock.cancelledCandidates},
        {"trace_dropped",nodeClock.traceDropped.load()}};
    return result;
}
pair<int64_t,int64_t> GetCandidateBudgets(bool reuseWork=false) {
    lock_guard lock(nodeClock.lock);
    auto now=int64_t(GetSteadyTime()/1000);
    vector<double> targets,rtts,deviations;
    for (const auto& [key,source]:nodeClock.sources) if (now-source.at<=int64_t(nodeClock.periodMs)*4000) {
        targets.push_back(source.target); rtts.push_back(source.rtt);
    }
    auto center=ClockPercentile(targets,0.5);
    for (auto value:targets) deviations.push_back(abs(value-center));
    auto correction=double(nodeClock.servo.Advance(now)-GetRawClockUsLocked());
    nodeClock.baselineTimeUs=targets.size()<size_t(nodeClock.minPeers)?1000000:
        max(250.0,abs(center-correction)+3*ClockPercentile(deviations,0.5)+ClockPercentile(rtts,0.95)/2);
    while (!nodeClock.preparations.empty()&&now-nodeClock.preparations.front().at>30000000) nodeClock.preparations.pop_front();
    if (!reuseWork) {
        vector<double> work; for (const auto& value:nodeClock.preparations) work.push_back(value.duration);
        nodeClock.baselinePrepareUs=max(1000.0,ClockPercentile(work,nodeClock.prepareQuantile));
    }
    auto uncertainty=nodeClock.baselineTimeUs,prepare=nodeClock.baselinePrepareUs;
    if (nodeClock.control=="learned"&&nodeClock.prediction.valid&&now-nodeClock.predictionAt<=int64_t(nodeClock.modelTtlMs)*1000) {
        if (nodeClock.prediction.HasTime()) uncertainty=max(uncertainty,nodeClock.prediction.timeUs);
        if (nodeClock.prediction.HasPrepare()) {
            // Preparation lead is a performance prediction, not a validity bound.
            // Frozen deadlines and parent/version validation still reject late work.
            prepare=nodeClock.model.preparation.loaded?nodeClock.prediction.prepareUs:max(prepare,nodeClock.prediction.prepareUs);
            nodeClock.learnedPrepareUses++;
            if (prepare<nodeClock.baselinePrepareUs) nodeClock.learnedPrepareReductions++;
        }
    }
    nodeClock.selectedPrepareUs=prepare; nodeClock.selectedTimeUs=uncertainty;
    return {int64_t(ceil(uncertainty)),int64_t(ceil(prepare))};
}
pair<int64_t,int64_t> GetDecisionBudgets(size_t ready,size_t pendingTx,size_t pendingBlock,ClockFeatures& features,bool recordBudget=true) {
    auto began=GetSteadyTime();
    auto budgets=GetCandidateBudgets();
    double fallback=1000; bool fresh=false;
    {
        lock_guard lock(nodeClock.lock);
        features=nodeClock.features;
        if (!nodeClock.candidateTiming&&!nodeClock.candidateAdaptive) {
            if (recordBudget) nodeClock.decisionBudgetNs+=GetSteadyTime()-began;
            return budgets;
        }
        auto now=int64_t(GetSteadyTime()/1000);
        fresh=nodeClock.featureAt&&now-nodeClock.featureAt<=int64_t(nodeClock.modelTtlMs)*1000;
        features[11]=double(pendingTx); features[12]=double(pendingBlock); features[13]=double(ready);
        vector<double> work,commits;
        for (const auto& value:nodeClock.preparations) if (now-value.at<=30000000) work.push_back(value.duration);
        for (const auto& value:nodeClock.commits) if (now-value.at<=30000000) commits.push_back(value.duration);
        features[14]=max(1000.0,ClockPercentile(work,.95))/1000;
        features[15]=ClockPercentile(commits,.95)/1000;
        fallback=nodeClock.baselinePrepareUs;
    }
    // Model is immutable after startup. Never run inference holding pool/DB locks.
    auto inferStart=GetSteadyTime();
    auto predicted=fresh?nodeClock.model.Predict(features):ClockPrediction{};
    auto inference=GetSteadyTime()-inferStart;
    bool used=fresh&&nodeClock.control=="learned"&&predicted.HasPrepare();
    budgets.second=int64_t(ceil(used?predicted.prepareUs:fallback));
    {
        lock_guard lock(nodeClock.lock);
        if (used) nodeClock.decisionPredictions++; else nodeClock.decisionFallbacks++;
        nodeClock.selectedPrepareUs=double(budgets.second);
        nodeClock.decisionInferenceNs+=inference;
        if (recordBudget) nodeClock.decisionBudgetNs+=GetSteadyTime()-began;
    }
    return budgets;
}
pair<int64_t,int64_t> GetAdaptiveBudgets(AdaptivePreparation& policy,uint64_t state,int64_t round,
        size_t ready,size_t pendingTx,size_t pendingBlock,ClockFeatures& features) {
    auto began=GetSteadyTime();
    auto now=int64_t(GetSteadyTime()/1000); AdaptivePreparation::Key key;
    {
        lock_guard lock(nodeClock.lock);
        key={state,nodeClock.featureVersion,nodeClock.workVersion,round,
            ready/512,AdaptivePreparation::QueueBucket(pendingTx),AdaptivePreparation::QueueBucket(pendingBlock)};
        // A stale feature frame invalidates even a recent cached decision.
        if (!nodeClock.featureAt||now-nodeClock.featureAt>int64_t(nodeClock.modelTtlMs)*1000) policy.cached=false;
    }
    auto bucket=size_t(pendingBlock>0);
    if (policy.Hit(key,now)) {
        auto budgets=GetCandidateBudgets(true); features=policy.features;
        budgets.second=policy.Budget(bucket);
        { lock_guard lock(nodeClock.lock); ++nodeClock.decisionCacheHits; nodeClock.selectedPrepareUs=double(budgets.second); nodeClock.decisionBudgetNs+=GetSteadyTime()-began; }
        return budgets;
    }
    uint64_t predictions;
    { lock_guard lock(nodeClock.lock); predictions=nodeClock.decisionPredictions; }
    auto budgets=GetDecisionBudgets(ready,pendingTx,pendingBlock,features,false);
    { lock_guard lock(nodeClock.lock); policy.modelUsed=nodeClock.decisionPredictions>predictions; }
    policy.key=key; policy.cachedAt=now; policy.cached=true; policy.rawUs=budgets.second; policy.features=features;
    budgets.second=policy.Budget(bucket);
    { lock_guard lock(nodeClock.lock); nodeClock.selectedPrepareUs=double(budgets.second); nodeClock.decisionBudgetNs+=GetSteadyTime()-began; }
    return budgets;
}
vector<uint8_t> GetTime() {
    vector<uint8_t> data(8);
    WriteU64(data.data(),duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count());
    return data;
}
#endif
