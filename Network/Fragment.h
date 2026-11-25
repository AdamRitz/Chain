#ifndef CHAIN_NETWORK_FRAGMENT_H
#define CHAIN_NETWORK_FRAGMENT_H
#include <atomic>
#include <cstdint>
#include <cstring>
#include <map>
#include <span>
#include <vector>

namespace chain::fragment {
constexpr size_t kChunkBytes=64*1024,kHeaderBytes=17,kMaxMessageBytes=2*1024*1024;
constexpr size_t kPeerBytes=8*1024*1024,kGlobalBytes=64*1024*1024;
constexpr uint64_t kTimeoutNs=30'000'000'000ULL;
inline std::atomic<size_t> bufferedBytes{0};
inline std::atomic<uint64_t> sentFrames{0},receivedFrames{0},completedMessages{0};
inline bool Allowed(uint8_t type) {
    switch (type) {
    case 2:case 7:case 10:case 18:case 22:case 24:case 26:case 41:case 43:return true;
    default:return false;
    }
}
inline uint64_t Read(const uint8_t* p,size_t n) { uint64_t v=0;for(size_t i=0;i<n;i++)v|=uint64_t(p[i])<<(8*i);return v; }
inline void Write(uint8_t* p,uint64_t v,size_t n) { for(size_t i=0;i<n;i++)p[i]=uint8_t(v>>(8*i)); }
inline std::vector<uint8_t> Encode(std::span<const uint8_t> frame,uint64_t id,size_t offset) {
    const auto total=frame.size()-5,count=std::min(kChunkBytes,total-offset);
    std::vector<uint8_t> out(5+kHeaderBytes+count);
    out[0]=55;Write(out.data()+1,kHeaderBytes+count,4);
    Write(out.data()+5,id,8);out[13]=frame[0];Write(out.data()+14,total,4);Write(out.data()+18,offset,4);
    std::memcpy(out.data()+22,frame.data()+5+offset,count);return out;
}
enum class Result { Invalid,Pending,Complete };
class Receiver {
    struct Partial { uint8_t type=0;size_t next=0;uint64_t started=0;std::vector<uint8_t> data; };
    std::map<uint64_t,Partial> pending;
    size_t bytes=0;uint64_t lastStarted=0;
public:
    Receiver()=default;Receiver(const Receiver&)=delete;Receiver& operator=(const Receiver&)=delete;
    ~Receiver() { Clear(); }
    void Clear() { bufferedBytes.fetch_sub(bytes);bytes=0;pending.clear(); }
    size_t Bytes() const { return bytes; }
    bool Expired(uint64_t now) const { for(const auto& [id,p]:pending)if(now-p.started>kTimeoutNs)return true;return false; }
    template<class Validator> Result Accept(std::span<const uint8_t> input,uint64_t now,uint8_t& type,std::vector<uint8_t>& out,Validator valid) {
        if(input.size()<=kHeaderBytes||input.size()>kHeaderBytes+kChunkBytes||Expired(now))return Result::Invalid;
        const auto id=Read(input.data(),8),total=Read(input.data()+9,4),offset=Read(input.data()+13,4);
        const auto original=input[8];const auto count=input.size()-kHeaderBytes;
        if(!id||!Allowed(original)||total<=kChunkBytes||total>kMaxMessageBytes||!valid(original,uint32_t(total))||
            offset>=total||count>total-offset||(count!=kChunkBytes&&offset+count!=total))return Result::Invalid;
        auto found=pending.find(id);
        if(found==pending.end()) {
            if(offset||id<=lastStarted||pending.size()>=4||total>kPeerBytes-bytes)return Result::Invalid;
            auto used=bufferedBytes.load();
            do { if(total>kGlobalBytes-used)return Result::Invalid; } while(!bufferedBytes.compare_exchange_weak(used,used+total));
            try { found=pending.emplace(id,Partial{original,0,now,std::vector<uint8_t>(total)}).first; }
            catch(...) { bufferedBytes.fetch_sub(total);throw; }
            bytes+=total;lastStarted=id;
        }
        auto& part=found->second;
        if(part.type!=original||part.data.size()!=total||part.next!=offset)return Result::Invalid;
        std::memcpy(part.data.data()+offset,input.data()+kHeaderBytes,count);part.next+=count;receivedFrames++;
        if(part.next!=total)return Result::Pending;
        type=original;out=std::move(part.data);bytes-=total;bufferedBytes.fetch_sub(total);pending.erase(found);completedMessages++;
        return Result::Complete;
    }
};
}
#endif
