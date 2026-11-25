#ifndef CHAIN_NETWORK_PEER_MAINTENANCE_H
#define CHAIN_NETWORK_PEER_MAINTENANCE_H
#include <algorithm>
#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <utility>

class PeerRetrySchedule {
    struct Entry {unsigned failures=0;uint64_t retry=0,touched=0;};
    std::mutex mutex;std::map<std::pair<std::string,uint16_t>,Entry> entries;
public:
    bool Ready(const std::pair<std::string,uint16_t>& endpoint,uint64_t now) {
        std::lock_guard lock(mutex);auto found=entries.find(endpoint);
        return found==entries.end()||now>=found->second.retry;
    }
    void Record(const std::pair<std::string,uint16_t>& endpoint,bool success,uint64_t now) {
        std::lock_guard lock(mutex);
        if(!entries.count(endpoint)&&entries.size()>=1024) {
            auto oldest=std::min_element(entries.begin(),entries.end(),[](const auto& a,const auto& b){return a.second.touched<b.second.touched;});
            entries.erase(oldest);
        }
        auto& entry=entries[endpoint];entry.touched=now;
        if(success) {entry.failures=0;entry.retry=0;}
        else {entry.failures=std::min(entry.failures+1,7u);entry.retry=now+std::min(uint64_t(60),uint64_t(1)<<(entry.failures-1))*1'000'000'000ULL;}
    }
};
#endif
