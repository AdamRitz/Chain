#pragma once

#include <array>
#include <condition_variable>
#include <cstddef>
#include <functional>
#include <mutex>
#include <thread>

namespace chain::work_election {

enum class ProofPriority { Normal, Block };

// Share one executor between network tickets and block verification. A caller
// submits one ticket's groups and waits for both workers to finish. Concurrent
// callers wait before submitting; waiting blocks run before ordinary tickets.
class ProofVerifier {
    struct Batch {
        const std::function<bool(std::size_t)>& verify;
        std::size_t count;
        std::size_t next = 0;
        std::size_t remaining;
        bool valid = true;
    };

    std::mutex callers_;
    std::condition_variable callersReady_;
    std::array<std::size_t, 2> waiting_{};
    bool active_ = false;
    std::mutex mutex_;
    std::condition_variable ready_;
    std::condition_variable done_;
    Batch* batch_ = nullptr;
    bool stopping_ = false;
    std::array<std::thread, 2> workers_;
    inline static thread_local ProofVerifier* current_ = nullptr;

    void FinishCall() noexcept {
        {
            std::lock_guard lock(callers_);
            active_ = false;
        }
        callersReady_.notify_all();
    }

    void Run() {
        current_ = this;
        std::unique_lock lock(mutex_);
        for (;;) {
            ready_.wait(lock, [&] { return stopping_ || (batch_ && batch_->next < batch_->count); });
            if (stopping_) break;
            auto& batch = *batch_;
            const auto group = batch.next++;
            lock.unlock();
            bool valid = false;
            try { valid = batch.verify(group); } catch (...) { valid = false; }
            lock.lock();
            batch.valid = batch.valid && valid;
            if (--batch.remaining == 0) done_.notify_one();
        }
        current_ = nullptr;
    }

    void Stop() noexcept {
        {
            std::lock_guard lock(mutex_);
            stopping_ = true;
        }
        ready_.notify_all();
        for (auto& worker : workers_) if (worker.joinable()) worker.join();
    }

public:
    ProofVerifier() {
        try { for (auto& worker : workers_) worker = std::thread([this] { Run(); }); }
        catch (...) { Stop(); throw; }
    }
    ProofVerifier(const ProofVerifier&) = delete;
    ProofVerifier& operator=(const ProofVerifier&) = delete;
    ~ProofVerifier() {
        // Owners must stop their callers before destroying the shared executor.
        std::unique_lock callLock(callers_);
        callersReady_.wait(callLock, [&] { return !active_ && waiting_[0] == 0 && waiting_[1] == 0; });
        Stop();
    }

    [[nodiscard]] std::size_t Pending(ProofPriority priority) {
        std::lock_guard lock(callers_);
        return waiting_[priority == ProofPriority::Block ? 1 : 0];
    }

    [[nodiscard]] bool VerifyGroups(std::size_t count, const std::function<bool(std::size_t)>& verify,
                                    ProofPriority priority = ProofPriority::Normal) {
        if (count == 0) return true;
        if (!verify) return false;
        // Keep nested calls safe without waiting for the worker executing them.
        if (current_ == this) {
            try {
                for (std::size_t i = 0; i < count; ++i) if (!verify(i)) return false;
                return true;
            } catch (...) { return false; }
        }
        {
            std::unique_lock callLock(callers_);
            const auto kind = priority == ProofPriority::Block ? 1 : 0;
            ++waiting_[kind];
            try {
                callersReady_.wait(callLock, [&] { return !active_ && (kind == 1 || waiting_[1] == 0); });
            } catch (...) {
                --waiting_[kind];
                callersReady_.notify_all();
                throw;
            }
            --waiting_[kind];
            active_ = true;
        }
        struct Finish { ProofVerifier& owner; ~Finish() { owner.FinishCall(); } } finish{*this};
        Batch batch{verify, count, 0, count};
        std::unique_lock lock(mutex_);
        if (stopping_) return false;
        batch_ = &batch;
        ready_.notify_all();
        done_.wait(lock, [&] { return batch.remaining == 0; });
        batch_ = nullptr;
        return batch.valid;
    }
};

} // namespace chain::work_election
