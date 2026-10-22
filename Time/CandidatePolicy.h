#ifndef CHAIN_CANDIDATE_POLICY_H
#define CHAIN_CANDIDATE_POLICY_H
#include <array>
#include <algorithm>
#include <cstdint>

// Small online outcome model. Only completed local attempts train it; abstentions
// have no invented counterfactual label. State belongs to the main loop.
struct CandidateRiskModel {
    struct Cell { double failure=.5; unsigned samples=0; };
    std::array<Cell,4> cells{};
    unsigned consecutiveSkips=0;
    static size_t Bucket(bool busy,bool large) { return size_t(busy)*2+size_t(large); }
    void Observe(size_t bucket,bool failed) {
        auto& cell=cells.at(bucket);
        cell.failure=.875*cell.failure+.125*double(failed);
        cell.samples=std::min(1000000u,cell.samples+1);
        consecutiveSkips=0;
    }
    bool ShouldDefer(size_t bucket,bool ready,int64_t prepareUs,int64_t remainingUs) const {
        const auto& cell=cells.at(bucket);
        // A verified buffered alternative is mandatory. After two deferrals an
        // ordinary attempt is forced, even if the empirical prediction is bad.
        return ready&&consecutiveSkips<2&&cell.samples>=8&&prepareUs>=5000&&
            cell.failure>=.65&&remainingUs<prepareUs;
    }
};
#endif
