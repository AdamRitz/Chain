#include <atomic>
#include <iostream>
#include <limits>
#include <thread>
#include <vector>
#include "../Network/SendBudget.h"
#include "../Network/SendPriority.h"

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
        {
            SendQueueBudget control(100,4,4,20,1);
            auto destination=std::make_shared<SendPeerBudget>();
            auto transaction=control.TryReserve(destination,80,100);
            Require(bool(transaction)&&!control.TryReserve(destination,1,100),"transactions cannot fill reserved control bytes");
            auto urgent=control.TryReserve(destination,20,100,true,true);
            Require(bool(urgent)&&control.Snapshot().bytes==100,"control messages use reserved global bytes");
            transaction.reset();urgent.reset();
            std::vector<std::shared_ptr<SendReservation>> tasks;
            for (size_t i=0;i<3;i++) tasks.push_back(control.TryReserve(destination,1,100));
            Require(tasks[0]&&tasks[1]&&tasks[2]&&!control.TryReserve(destination,1,100),"transactions cannot fill reserved control tasks");
            urgent=control.TryReserve(destination,1,100,true,true);
            Require(bool(urgent),"control tasks proceed when ordinary task capacity is full");
        }
        {
            SendQueueBudget peerControl(256,8,2,0,0,20,1);
            auto destination=std::make_shared<SendPeerBudget>();
            auto ordinary=peerControl.TryReserve(destination,100,100);
            Require(bool(ordinary)&&!peerControl.TryReserve(destination,1,100),
                "ordinary traffic can use its full original peer byte allowance");
            auto urgent=peerControl.TryReserve(destination,20,100,true,true);
            Require(bool(urgent)&&!peerControl.TryReserve(destination,1,100,true,true),
                "urgent traffic has bounded additional peer bytes when ordinary traffic is full");
            ordinary.reset();urgent.reset();
            auto firstTask=peerControl.TryReserve(destination,1,100);
            auto secondTask=peerControl.TryReserve(destination,1,100);
            Require(firstTask&&secondTask&&!peerControl.TryReserve(destination,1,100),
                "ordinary peer task allowance remains bounded");
            urgent=peerControl.TryReserve(destination,1,100,true,true);
            Require(bool(urgent)&&!peerControl.TryReserve(destination,1,100,true,true),
                "urgent peer tasks use only the configured additional capacity");
            firstTask.reset();secondTask.reset();urgent.reset();
            Require(peerControl.Snapshot().bytes==0&&peerControl.Snapshot().tasks==0&&
                destination->bytes==0&&destination->tasks==0,"draining releases ordinary and additional control capacity");
            SendQueueBudget overflow(std::numeric_limits<size_t>::max(),4,2,0,0,
                std::numeric_limits<size_t>::max(),std::numeric_limits<size_t>::max());
            auto maximal=overflow.TryReserve(destination,std::numeric_limits<size_t>::max(),1,true,true);
            Require(bool(maximal)&&!overflow.TryReserve(destination,1,1,true,true),
                "additional peer capacity saturates without integer overflow and respects the global cap");
            maximal.reset();Require(overflow.Snapshot().bytes==0,"maximal accounting releases without overflow");
        }
        {
            PrioritySendQueue<int> queue;
            queue.Push(SendPriority::Bulk,30);queue.Push(SendPriority::Transaction,20);
            queue.Push(SendPriority::Urgent,10);queue.Push(SendPriority::Urgent,11);
            Require(queue.Pop().second==10&&queue.Pop().second==11,"urgent frames pass queued transactions and preserve FIFO");
            Require(queue.Pop().second==20&&queue.Pop().second==30&&queue.Empty(),"transactions precede bulk");
            for (int i=0;i<40;i++) queue.Push(SendPriority::Urgent,i);
            for (int i=0;i<10;i++) queue.Push(SendPriority::Transaction,i);
            for (int i=0;i<4;i++) queue.Push(SendPriority::Bulk,i);
            size_t urgentRun=0,nonBulkRun=0,transactionCount=0,bulkCount=0;
            std::array<int,3> last{-1,-1,-1};
            while (!queue.Empty()) {
                auto [priority,number]=queue.Pop();auto index=size_t(priority);
                Require(number>last[index],"FIFO remains ordered in each class under sustained load");last[index]=number;
                if (priority==SendPriority::Urgent) ++urgentRun;else {urgentRun=0;transactionCount+=priority==SendPriority::Transaction;}
                if (priority==SendPriority::Bulk) {nonBulkRun=0;++bulkCount;}else ++nonBulkRun;
                if (transactionCount<10) Require(urgentRun<=8,"continuous urgent messages cannot starve transactions");
                if (bulkCount<4) Require(nonBulkRun<=12,"continuous higher priority messages cannot starve bulk frames");
            }
            Require(MessageSendPriority(39)==SendPriority::Urgent&&MessageSendPriority(51)==SendPriority::Urgent&&
                MessageSendPriority(40)==SendPriority::Urgent&&MessageSendPriority(45)==SendPriority::Urgent&&
                MessageSendPriority(10)==SendPriority::Transaction&&MessageSendPriority(54)==SendPriority::Transaction&&
                MessageSendPriority(41)==SendPriority::Urgent,
                "compact announcements, requests and admission replies have correct priorities");
            SendQueueBudget leases(100,4,4);
            auto destination=std::make_shared<SendPeerBudget>();
            PrioritySendQueue<std::shared_ptr<SendReservation>> pending;
            pending.Push(SendPriority::Urgent,leases.TryReserve(destination,20,100));
            pending.Push(SendPriority::Bulk,leases.TryReserve(destination,30,100));
            auto active=pending.Pop();pending.Clear();
            Require(leases.Snapshot().bytes==20,"clearing queued frames retains the active frame reservation");
            active.second.reset();Require(leases.Snapshot().bytes==0,"active frame completion releases its reservation");
        }
        std::cout<<"Send budget limits, cancellation and concurrent ownership checks passed\n";
        return 0;
    } catch (const std::exception& error) { std::cerr<<error.what()<<'\n'; return 1; }
}
