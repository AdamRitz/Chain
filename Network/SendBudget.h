#ifndef CHAIN_NETWORK_SEND_BUDGET_H
#define CHAIN_NETWORK_SEND_BUDGET_H
#include <cstddef>
#include <memory>
#include <mutex>
#include <limits>
#include <utility>

// Counts logical wire bytes per recipient, including handlers not yet run by a
// strand. Shared broadcast buffers are counted once for each queued recipient;
// these counters describe outstanding work, not resident memory usage.
struct SendPeerBudget { size_t bytes=0,tasks=0; };
struct SendBudgetSnapshot {
    size_t bytes=0,tasks=0,peakBytes=0,peakTasks=0;
    size_t byteLimit=0,taskLimit=0,peerTaskLimit=0,rejected=0;
    size_t controlByteReserve=0,controlTaskReserve=0;
    size_t peerControlByteReserve=0,peerControlTaskReserve=0;
};
struct SendBudgetState {
    mutable std::mutex lock;
    SendBudgetSnapshot counters;
};
class SendQueueBudget;
class SendReservation {
    friend class SendQueueBudget;
    std::shared_ptr<SendBudgetState> state;
    std::shared_ptr<SendPeerBudget> peer;
    size_t bytes;
    bool active=false;
    SendReservation(std::shared_ptr<SendBudgetState> queue,std::shared_ptr<SendPeerBudget> destination,size_t size):
        state(std::move(queue)),peer(std::move(destination)),bytes(size) {}
public:
    SendReservation(const SendReservation&)=delete;
    SendReservation& operator=(const SendReservation&)=delete;
    ~SendReservation();
};
class SendQueueBudget {
    std::shared_ptr<SendBudgetState> state=std::make_shared<SendBudgetState>();
    static size_t ExtendLimit(size_t limit,size_t extra) {
        const auto maximum=std::numeric_limits<size_t>::max();
        return extra>maximum-limit?maximum:limit+extra;
    }
public:
    // Outstanding handlers retain the state even when their queue owner has
    // already been destroyed, including during exceptional shutdown.
    SendQueueBudget(size_t bytes,size_t tasks,size_t peerTasks,size_t controlBytes=0,size_t controlTasks=0,
            size_t peerControlBytes=0,size_t peerControlTasks=0) {
        state->counters.byteLimit=bytes; state->counters.taskLimit=tasks; state->counters.peerTaskLimit=peerTasks;
        state->counters.controlByteReserve=controlBytes<bytes?controlBytes:bytes;
        state->counters.controlTaskReserve=controlTasks<tasks?controlTasks:tasks;
        state->counters.peerControlByteReserve=peerControlBytes;
        state->counters.peerControlTaskReserve=peerControlTasks;
    }
    SendQueueBudget(const SendQueueBudget&)=delete;
    SendQueueBudget& operator=(const SendQueueBudget&)=delete;
    std::shared_ptr<SendReservation> TryReserve(const std::shared_ptr<SendPeerBudget>& peer,
            size_t bytes,size_t peerByteLimit,bool countRejection=true,bool control=false) {
        if (!peer||!bytes) return {};
        // Allocate before changing counters, so allocation failure cannot leak
        // a reservation. Failed probes hold no counted work.
        auto reservation=std::shared_ptr<SendReservation>(new SendReservation(state,peer,bytes));
        std::lock_guard guard(state->lock);
        auto& counters=state->counters;
        auto byteLimit=counters.byteLimit-(control?0:counters.controlByteReserve);
        auto taskLimit=counters.taskLimit-(control?0:counters.controlTaskReserve);
        // Keep the ordinary per-peer allowance intact, so a maximum-sized
        // block reply can still be sent. Urgent frames may use this additional
        // bounded capacity; the node-wide limit always remains unchanged.
        auto peerTasks=counters.peerTaskLimit;
        if (control) {
            peerByteLimit=ExtendLimit(peerByteLimit,counters.peerControlByteReserve);
            peerTasks=ExtendLimit(peerTasks,counters.peerControlTaskReserve);
        }
        if (bytes>byteLimit||counters.bytes>byteLimit-bytes||counters.tasks>=taskLimit||
                bytes>peerByteLimit||peer->bytes>peerByteLimit-bytes||peer->tasks>=peerTasks) {
            if (countRejection) counters.rejected++;
            return {};
        }
        counters.bytes+=bytes; counters.tasks++; peer->bytes+=bytes; peer->tasks++;
        if (counters.bytes>counters.peakBytes) counters.peakBytes=counters.bytes;
        if (counters.tasks>counters.peakTasks) counters.peakTasks=counters.tasks;
        reservation->active=true;
        return reservation;
    }
    SendBudgetSnapshot Snapshot() const { std::lock_guard guard(state->lock); return state->counters; }
};
inline SendReservation::~SendReservation() {
    if (!active) return;
    std::lock_guard guard(state->lock);
    state->counters.bytes-=bytes; state->counters.tasks--; peer->bytes-=bytes; peer->tasks--;
}
#endif
