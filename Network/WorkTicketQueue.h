#pragma once

#include "../Crypto/WorkElection.h"
#include <memory>
#include <optional>

namespace chain::work_queue {

using work_election::Digest;
using work_election::Ticket;

inline bool TicketLess(const Ticket& left, const Ticket& right) {
    const auto a = work_election::DeriveScore(left.context, left.publicKey);
    const auto b = work_election::DeriveScore(right.context, right.publicKey);
    return a == b ? left.publicKey < right.publicKey : a < b;
}

// Only records that already passed full verification belong in this span.
// Pending tickets never change the threshold used to skip mathematical work.
inline bool IsCompetitive(const Ticket& ticket,
        std::span<const std::shared_ptr<const Ticket>> verified, std::size_t limit = 10) {
    if (limit == 0) return false;
    const Ticket* worst = nullptr;
    std::size_t count = 0;
    for (const auto& item : verified) {
        if (!item || item->context != ticket.context) continue;
        if (item->publicKey == ticket.publicKey) return false;
        ++count;
        if (!worst || TicketLess(*worst, *item)) worst = item.get();
    }
    return count < limit || (worst && TicketLess(ticket, *worst));
}

struct PendingTicket {
    Ticket record;
    std::vector<std::uint8_t> bytes;
    Digest hash{}, score{};
    std::uint64_t source = 0;

    PendingTicket(Ticket ticket, std::vector<std::uint8_t> message,
                  Digest messageHash, std::uint64_t peer)
        : record(std::move(ticket)), bytes(std::move(message)), hash(messageHash),
          score(work_election::DeriveScore(record.context, record.publicKey)), source(peer) {}
};

// The node holds workQueueMutex for all accesses. Small bounded vectors also
// make shutdown accounting explicit: Drain transfers every outstanding message.
class TicketQueue {
    std::vector<PendingTicket> pending_;
    std::size_t capacity_, byteLimit_, bytes_ = 0;
public:
    explicit TicketQueue(std::size_t capacity = 32, std::size_t byteLimit = 2 * 1024 * 1024)
        : capacity_(capacity), byteLimit_(byteLimit) {}

    bool Push(PendingTicket ticket) {
        if (pending_.size() >= capacity_ || ticket.bytes.size() > byteLimit_ - bytes_) return false;
        const auto size = ticket.bytes.size();
        pending_.push_back(std::move(ticket));
        bytes_ += size;
        return true;
    }
    std::optional<PendingTicket> TakeBest() {
        if (pending_.empty()) return std::nullopt;
        const auto best = std::min_element(pending_.begin(), pending_.end(),
            [](const auto& a, const auto& b) {
                if (a.score != b.score) return a.score < b.score;
                if (a.record.publicKey != b.record.publicKey) return a.record.publicKey < b.record.publicKey;
                return a.hash < b.hash;
            });
        PendingTicket result = std::move(*best);
        pending_.erase(best);
        bytes_ -= result.bytes.size();
        return result;
    }
    std::size_t Size() const { return pending_.size(); }
    std::size_t Bytes() const { return bytes_; }
    std::vector<PendingTicket> Drain() {
        auto result = std::move(pending_);
        pending_.clear();
        bytes_ = 0;
        return result;
    }
};

} // namespace chain::work_queue
