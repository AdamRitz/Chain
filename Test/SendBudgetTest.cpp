#include <atomic>
#include <iostream>
#include <limits>
#include <thread>
#include <vector>
#include "../Network/SendBudget.h"

void Require(bool condition,const char* message) { if (!condition) throw std::runtime_error(message); }
int main() {
    try {
        SendQueueBudget budget(100,3,2);
        auto first=std::make_shared<SendPeerBudget>(),second=std::make_shared<SendPeerBudget>();
        auto a=budget.TryReserve(first,40,60);
        Require(bool(a),"first reservation");
        Require(!budget.TryReserve(first,21,60),"peer bytes checked before work is queued");
        auto b=budget.TryReserve(first,20,60);
        Require(bool(b)&&!budget.TryReserve(first,1,60),"peer task limit is independent of global bytes");
        auto c=budget.TryReserve(second,40,60);
        Require(bool(c)&&!budget.TryReserve(second,1,60),"node budget covers multiple peers");
        auto snapshot=budget.Snapshot();
        Require(snapshot.bytes==100&&snapshot.tasks==3&&snapshot.peakBytes==100&&snapshot.peakTasks==3,
            "current and peak reservations reflect pending work");
        auto inFlight=a; a.reset();
        Require(budget.Snapshot().bytes==100,"active write may outlive queue removal");
        inFlight.reset(); b.reset(); c.reset();
        Require(budget.Snapshot().bytes==0&&budget.Snapshot().tasks==0,"last owner releases both counters");
        Require(!budget.TryReserve(first,std::numeric_limits<size_t>::max(),60),"oversized request cannot overflow counters");
        {
            // Destroying an unexecuted post or failed admission reply drops the
            // same lease as a successfully completed queued write.
            auto cancelled=budget.TryReserve(first,60,60);
            Require(bool(cancelled),"released capacity is reusable");
        }
        Require(budget.Snapshot().tasks==0&&first->bytes==0&&first->tasks==0,"cancelled owner releases peer counters");
        auto rejected=budget.Snapshot().rejected;
        auto held=budget.TryReserve(first,60,60);
        Require(!budget.TryReserve(first,1,60,false)&&budget.Snapshot().rejected==rejected,
            "backpressure probes do not count as rejected messages");
        held.reset();
        {
            auto latePeer=std::make_shared<SendPeerBudget>();
            std::shared_ptr<SendReservation> lateHandler;
            {
                SendQueueBudget owner(64,1,1);
                lateHandler=owner.TryReserve(latePeer,64,64);
                Require(bool(lateHandler),"handler reserves work before owner destruction");
            }
            Require(latePeer->bytes==64&&latePeer->tasks==1,"handler retains accounting state after owner destruction");
            lateHandler.reset();
            Require(latePeer->bytes==0&&latePeer->tasks==0,"late cancellation releases accounting after owner destruction");
        }

        SendQueueBudget concurrent(512,8,2);
        std::atomic<size_t> accepted{0}; std::vector<std::thread> workers;
        for (size_t i=0;i<16;i++) workers.emplace_back([&] {
            auto peer=std::make_shared<SendPeerBudget>();
            for (size_t attempt=0;attempt<2000;attempt++) {
                auto reservation=concurrent.TryReserve(peer,64,128);
                if (reservation) { accepted++; std::this_thread::yield(); }
            }
        });
        for (auto& worker:workers) worker.join();
        snapshot=concurrent.Snapshot();
        Require(accepted>0&&snapshot.bytes==0&&snapshot.tasks==0&&snapshot.peakBytes<=512&&snapshot.peakTasks<=8,
            "concurrent reservations preserve limits and release exactly once");
        std::cout<<"Send budget limits, cancellation and concurrent ownership checks passed\n";
        return 0;
    } catch (const std::exception& error) { std::cerr<<error.what()<<'\n'; return 1; }
}
