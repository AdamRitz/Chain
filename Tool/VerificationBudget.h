#ifndef CHAIN_VERIFICATION_BUDGET_H
#define CHAIN_VERIFICATION_BUDGET_H
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>

// Startup configuration. Permits cover cold cryptographic work only, never
// cache waits, pool/DB locks, child-task joins, or network I/O.
struct VerificationBudget {
    size_t limit=0;
    struct Stats { size_t active=0,peak=0,normalWaiting=0,priorityWaiting=0; uint64_t waits=0,waitNs=0; } stats;
    std::mutex lock;
    std::condition_variable condition;
    unsigned priorityStreak=0;
    void Enter(bool priority) {
        auto start=std::chrono::steady_clock::now();
        std::unique_lock guard(lock);
        auto& waiting=priority?stats.priorityWaiting:stats.normalWaiting;
        ++waiting;
        auto allowed=[&] {
            if (stats.active>=limit) return false;
            // Four priority grants at most while ordinary traffic is waiting.
            return priority?(priorityStreak<4||!stats.normalWaiting):(!stats.priorityWaiting||priorityStreak>=4);
        };
        if (!allowed()) { ++stats.waits; condition.wait(guard,allowed); }
        --waiting; ++stats.active; stats.peak=std::max(stats.peak,stats.active);
        priorityStreak=priority?std::min(4u,priorityStreak+1):0;
        stats.waitNs+=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count());
        condition.notify_all();
    }
    void Leave() { std::lock_guard guard(lock); --stats.active; condition.notify_all(); }
    Stats Snapshot() { std::lock_guard guard(lock); return stats; }
    struct Lease {
        VerificationBudget* owner;
        Lease(VerificationBudget& budget,bool priority):owner(budget.limit?&budget:nullptr) { if (owner) owner->Enter(priority); }
        ~Lease() { if (owner) owner->Leave(); }
        Lease(const Lease&)=delete;
        Lease& operator=(const Lease&)=delete;
    };
};
inline VerificationBudget verificationBudget;
#endif
