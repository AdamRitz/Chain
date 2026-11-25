#ifndef CHAIN_NETWORK_SEND_PRIORITY_H
#define CHAIN_NETWORK_SEND_PRIORITY_H
#include <array>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <utility>

enum class SendPriority:size_t { Urgent=0,Transaction=1,Bulk=2 };
inline SendPriority MessageSendPriority(uint8_t type) {
    switch (type) {
    case 1: case 10: case 17: case 18: case 44: case 53: return SendPriority::Transaction;
    case 2: case 7: case 22: case 24: case 26: case 41: case 43: return SendPriority::Bulk;
    default: return SendPriority::Urgent;
    }
}

// Called only on the connection strand. A selected frame leaves this queue
// before its write starts, so a higher priority frame never interrupts bytes
// from the active frame. Each class keeps FIFO order.
template<class Message> class PrioritySendQueue {
    std::array<std::deque<Message>,3> queues;
    size_t urgentRun=0,nonBulkRun=0;
public:
    static constexpr size_t maxUrgentRun=8,maxNonBulkRun=12;
    bool Empty() const { return queues[0].empty()&&queues[1].empty()&&queues[2].empty(); }
    void Push(SendPriority priority,Message message) { queues[size_t(priority)].push_back(std::move(message)); }
    void PushFront(SendPriority priority,Message message) { queues[size_t(priority)].push_front(std::move(message)); }
    std::pair<SendPriority,Message> Pop() {
        size_t index;
        if (!queues[2].empty()&&nonBulkRun>=maxNonBulkRun) index=2;
        else if (!queues[1].empty()&&urgentRun>=maxUrgentRun) index=1;
        else if (!queues[0].empty()) index=0;
        else if (!queues[1].empty()) index=1;
        else index=2;
        auto message=std::move(queues[index].front()); queues[index].pop_front();
        if (index==0) { if (urgentRun<maxUrgentRun) ++urgentRun; }
        else if (index==1) urgentRun=0;
        if (index==2) nonBulkRun=0;
        else if (nonBulkRun<maxNonBulkRun) ++nonBulkRun;
        return {SendPriority(index),std::move(message)};
    }
    void Clear() { for (auto& queue:queues) queue.clear(); urgentRun=nonBulkRun=0; }
};
#endif
