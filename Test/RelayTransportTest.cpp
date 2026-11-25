#include "../Network/Fragment.h"
#include "../Network/SendPriority.h"
#include "../Network/EarlyRelay.h"
#include "../Network/PeerMaintenance.h"
#include <iostream>
#include <stdexcept>
using namespace chain::fragment;
void Require(bool value) {if(!value)throw std::runtime_error("Relay transport assertion failed");}
int main() {
    auto valid=[](uint8_t t,uint32_t size){return Allowed(t)&&size<=kMaxMessageBytes;};
    std::vector<uint8_t> full(5+200000);full[0]=43;Write(full.data()+1,200000,4);
    for(size_t i=5;i<full.size();i++)full[i]=uint8_t(i);
    std::vector<uint8_t> out;uint8_t type=0;
    {
        Receiver receiver;
        for(size_t offset=0;offset<200000;offset+=kChunkBytes) {
            auto chunk=Encode(full,1,offset);
            const auto result=receiver.Accept(std::span(chunk).subspan(5),100,type,out,valid);
            Require(result==(offset+kChunkBytes>=200000?Result::Complete:Result::Pending));
        }
        Require(type==43&&out==std::vector<uint8_t>(full.begin()+5,full.end())&&receiver.Bytes()==0&&bufferedBytes==0);
        auto first=Encode(full,2,0);
        Require(receiver.Accept(std::span(first).subspan(5),100,type,out,valid)==Result::Pending);
        Require(bufferedBytes==200000);
        Require(receiver.Accept(std::span(first).subspan(5),100,type,out,valid)==Result::Invalid);
        auto skipped=Encode(full,2,2*kChunkBytes);
        Require(receiver.Accept(std::span(skipped).subspan(5),100,type,out,valid)==Result::Invalid);
        Require(receiver.Expired(101+kTimeoutNs));
        receiver.Clear();Require(bufferedBytes==0);
        first[13]=55;Require(receiver.Accept(std::span(first).subspan(5),100,type,out,valid)==Result::Invalid);
        first[13]=43;Write(first.data()+14,kMaxMessageBytes+1,4);
        Require(receiver.Accept(std::span(first).subspan(5),100,type,out,valid)==Result::Invalid);
    }
    {
        std::vector<Receiver> receivers(40);
        full.resize(kMaxMessageBytes+5);full[0]=43;
        auto first=Encode(full,1,0);
        for(size_t i=0;i<32;i++)Require(receivers[i].Accept(std::span(first).subspan(5),100,type,out,valid)==Result::Pending);
        Require(bufferedBytes==kGlobalBytes);
        Require(receivers[32].Accept(std::span(first).subspan(5),100,type,out,valid)==Result::Invalid);
    }
    Require(bufferedBytes==0);
    PrioritySendQueue<int> queue;
    queue.Push(SendPriority::Bulk,1);queue.Push(SendPriority::Bulk,2);
    Require(queue.Pop().second==1);queue.PushFront(SendPriority::Bulk,1);queue.Push(SendPriority::Urgent,3);
    Require(queue.Pop().second==3&&queue.Pop().second==1&&queue.Pop().second==2);
    EarlyRelayBudget budget;std::array<uint8_t,32> producer{};
    for(unsigned i=0;i<8;i++)Require(budget.Reserve(2,1,producer,1024));
    Require(!budget.Reserve(2,1,producer,1024));
    for(unsigned i=1;i<64;i++){producer[0]=uint8_t(i);budget.Reserve(2,1,producer,1024);}
    Require(!budget.Reserve(2,2,producer,1024));
    for(uint64_t h=3;h<7;h++)Require(budget.Reserve(h,1,producer,1024));
    Require(!budget.Reserve(2,1,producer,1024));
    PeerRetrySchedule retries;auto endpoint=std::make_pair(std::string("127.0.0.1"),uint16_t(1));
    Require(retries.Ready(endpoint,10));retries.Record(endpoint,false,10);
    Require(!retries.Ready(endpoint,11)&&retries.Ready(endpoint,1'000'000'010ULL));
    retries.Record(endpoint,false,1'000'000'010ULL);Require(!retries.Ready(endpoint,2'000'000'010ULL));
    retries.Record(endpoint,true,2'000'000'010ULL);Require(retries.Ready(endpoint,2'000'000'010ULL));
    std::cout<<"Relay transport, bounds, scheduling and retry tests passed\n";
}
