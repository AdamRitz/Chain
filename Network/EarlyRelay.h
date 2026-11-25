#ifndef CHAIN_NETWORK_EARLY_RELAY_H
#define CHAIN_NETWORK_EARLY_RELAY_H
#include <array>
#include <cstdint>
#include <map>
#include <mutex>
#include <tuple>

// Bounds apply only to relay before account execution. Every block still goes
// through normal execution and can be relayed after successful validation.
class EarlyRelayBudget {
    struct Height {size_t blocks=0,bytes=0;std::map<std::pair<std::array<uint8_t,32>,uint64_t>,size_t> producers;};
    std::mutex mutex;std::map<uint64_t,Height> heights;
public:
    bool Reserve(uint64_t height,uint64_t slot,const std::array<uint8_t,32>& producer,size_t bytes) {
        std::lock_guard lock(mutex);
        if (!heights.count(height)&&heights.size()>=4) {
            if(height<=heights.begin()->first)return false;
            heights.erase(heights.begin());
        }
        auto& entry=heights[height];auto key=std::make_pair(producer,slot);
        auto found=entry.producers.find(key);
        if(entry.blocks>=64||bytes>32*1024*1024-entry.bytes||(found!=entry.producers.end()&&found->second>=8))return false;
        entry.producers[key]++;entry.blocks++;entry.bytes+=bytes;return true;
    }
};
#endif
