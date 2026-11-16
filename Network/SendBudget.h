#ifndef CHAIN_NETWORK_SEND_BUDGET_H
#define CHAIN_NETWORK_SEND_BUDGET_H
#include <cstddef>
#include <memory>
#include <mutex>
#include <utility>

// Counts logical wire bytes per recipient, including handlers not yet run by a
// strand. Shared broadcast buffers are counted once for each queued recipient;
// these counters describe outstanding work, not resident memory usage.
struct SendPeerBudget { size_t bytes=0,tasks=0; };
struct SendBudgetSnapshot {
    size_t bytes=0,tasks=0,peakBytes=0,peakTasks=0;
    size_t byteLimit=0,taskLimit=0,peerTaskLimit=0,rejected=0;
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
public:
    // Outstanding handlers retain the state even when their queue owner has
    // already been destroyed, including during exceptional shutdown.
    SendQueueBudget(size_t bytes,size_t tasks,size_t peerTasks) {
        state->counters.byteLimit=bytes; state->counters.taskLimit=tasks; state->counters.peerTaskLimit=peerTasks;
    }
    SendQueueBudget(const SendQueueBudget&)=delete;
    SendQueueBudget& operator=(const SendQueueBudget&)=delete;
    std::shared_ptr<SendReservation> TryReserve(const std::shared_ptr<SendPeerBudget>& peer,
            size_t bytes,size_t peerByteLimit,bool countRejection=true) {
        if (!peer||!bytes) return {};
        // Allocate before changing counters, so allocation failure cannot leak
        // a reservation. Failed probes hold no counted work.
        auto reservation=std::shared_ptr<SendReservation>(new SendReservation(state,peer,bytes));
        std::lock_guard guard(state->lock);
        auto& counters=state->counters;
        if (bytes>counters.byteLimit||counters.bytes>counters.byteLimit-bytes||counters.tasks>=counters.taskLimit||
                bytes>peerByteLimit||peer->bytes>peerByteLimit-bytes||peer->tasks>=counters.peerTaskLimit) {
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
