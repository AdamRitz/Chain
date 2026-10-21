#ifndef CHAIN_ROUND_H
#define CHAIN_ROUND_H
#include <array>
#include <cstdint>
#include <optional>
#include "ClockControl.h"

struct CandidateRound {
    int64_t id=0,startUs=0,proposalDeadlineUs=0,collectionDeadlineUs=0;
    uint64_t clockVersion=0,height=0,stateVersion=0,featureVersion=0;
    std::array<uint8_t,32> parent{};
    bool joinedCurrent=false;
    int64_t offeredUs=0;
};
// All deadlines are frozen on the steady clock. Predictions only reserve local work time.
std::optional<CandidateRound> PlanCandidateRound(const ClockMapping& mapping,int64_t lastRound,
    int64_t periodUs,int64_t windowUs,int64_t prepareUs,int64_t uncertaintyUs,int64_t guardUs,bool joinCurrent) {
    if (periodUs<=0||windowUs<=0||windowUs>=periodUs||prepareUs<0||uncertaintyUs<0||guardUs<0) return {};
    auto reserve=uncertaintyUs+guardUs;
    if (reserve>=windowUs) return {};
    auto current=mapping.logicalUs/periodUs;
    auto round=std::max(lastRound+1,current+1);
    if (joinCurrent&&current>lastRound&&mapping.logicalUs+prepareUs+reserve<current*periodUs+windowUs) round=current;
    auto start=mapping.ToSteady(round*periodUs);
    auto close=mapping.ToSteady(round*periodUs+windowUs);
    return CandidateRound{round,start,close-reserve,close,mapping.version,0,0,0,{},round==current};
}
bool CandidateRoundFits(const CandidateRound& round,int64_t now,uint64_t height,uint64_t version,const std::array<uint8_t,32>& parent) {
    return now>=round.startUs&&now<round.proposalDeadlineUs&&round.height==height&&round.stateVersion==version&&round.parent==parent;
}
#endif
