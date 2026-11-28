#include "../Network/WorkTicketQueue.h"

#include <iostream>

namespace {
namespace election = chain::work_election;
namespace queue = chain::work_queue;

void Require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

election::Ticket Ticket(std::uint8_t identity) {
    election::Ticket result;
    result.context.chainId[0] = 3;
    result.context.targetEpoch = 9;
    result.context.randomSeed[0] = 17;
    result.context.matureRounds = 32;
    result.publicKey[0] = identity;
    return result;
}

queue::PendingTicket Pending(std::uint8_t identity, std::uint8_t score,
                              std::size_t bytes = 1, std::uint8_t hash = 0) {
    election::Digest digest{}; digest[0] = hash;
    queue::PendingTicket result(Ticket(identity), std::vector<std::uint8_t>(bytes), digest, identity);
    Require(result.score == election::DeriveScore(result.record.context, result.record.publicKey),
            "Pending score is not derived from ticket");
    // Explicit scores exercise equal-score ordering without finding hash collisions.
    result.score.fill(0); result.score[0] = score;
    return result;
}

void TestOrdering() {
    queue::TicketQueue pending;
    Require(pending.Push(Pending(9, 7)), "First push failed");
    Require(pending.Push(Pending(4, 3, 2, 2)), "Second push failed");
    Require(pending.Push(Pending(2, 3, 3, 3)), "Third push failed");
    Require(pending.Push(Pending(2, 3, 4, 1)), "Fourth push failed");
    Require(pending.Size() == 4 && pending.Bytes() == 10, "Queue accounting mismatch");
    const auto best = pending.TakeBest();
    Require(best && best->record.publicKey[0] == 2 && best->hash[0] == 1 && best->source == 2,
            "Equal score/public key did not use message hash");
    Require(pending.Bytes() == 6, "Taking best did not release bytes");
    Require(pending.TakeBest()->hash[0] == 3, "Equal score did not use public key");
    Require(pending.TakeBest()->record.publicKey[0] == 4, "Score ordering failed");
    Require(pending.TakeBest()->record.publicKey[0] == 9, "Worst score was not last");
    Require(!pending.TakeBest() && pending.Size() == 0 && pending.Bytes() == 0, "Empty queue retained items");
}

void TestCapacityAndDrain() {
    queue::TicketQueue pending(2, 8);
    Require(pending.Push(Pending(1, 1, 4)) && pending.Push(Pending(2, 2, 4)), "Exact byte bound rejected");
    Require(!pending.Push(Pending(3, 0, 0)), "Item capacity exceeded");
    Require(pending.Size() == 2 && pending.Bytes() == 8, "Rejected item changed accounting");
    Require(pending.TakeBest().has_value(), "Capacity test could not take item");
    Require(!pending.Push(Pending(3, 0, 5)), "Byte capacity exceeded");
    Require(!pending.Push(Pending(3, 0, std::numeric_limits<std::uint16_t>::max())), "Oversized message admitted");
    Require(pending.Push(Pending(3, 0, 4)), "Released queue capacity not reusable");
    auto drained = pending.Drain();
    Require(drained.size() == 2 && drained[0].bytes.size() + drained[1].bytes.size() == 8,
            "Drain lost outstanding messages");
    Require(pending.Size() == 0 && pending.Bytes() == 0 && pending.Drain().empty(), "Drain did not reset accounting");
    Require(pending.Push(Pending(4, 1, 8)), "Drained queue cannot be reused");
    queue::TicketQueue disabled(0, 8);
    Require(!disabled.Push(Pending(1, 1)), "Zero item bound admitted message");
    queue::TicketQueue noBytes(2, 0);
    Require(!noBytes.Push(Pending(1, 1)), "Zero byte bound admitted message");
    queue::TicketQueue defaults;
    for (std::uint8_t i = 0; i < 32; ++i)
        Require(defaults.Push(Pending(i, i, 65536)), "Default queue rejected 32 messages totaling two MiB");
    Require(defaults.Size() == 32 && defaults.Bytes() == 2 * 1024 * 1024,
            "Default queue limits differ from 32 messages and two MiB");
    Require(!defaults.Push(Pending(33, 0)), "Default queue admitted a thirty-third message");
    defaults.Drain();
    Require(!defaults.Push(Pending(1, 0, 2 * 1024 * 1024 + 1)), "Default queue admitted more than two MiB");
}

void TestVerifiedThreshold() {
    std::vector<election::Ticket> candidates;
    for (std::uint8_t i = 1; i <= 12; ++i) candidates.push_back(Ticket(i));
    std::sort(candidates.begin(), candidates.end(), queue::TicketLess);
    std::vector<std::shared_ptr<const election::Ticket>> verified;
    for (std::size_t i = 1; i <= 9; ++i) verified.push_back(std::make_shared<const election::Ticket>(candidates[i]));
    Require(queue::IsCompetitive(candidates.back(), verified), "Fewer than ten valid tickets rejected a new identity");
    Require(!queue::IsCompetitive(candidates[2], verified), "Already verified identity admitted twice");
    verified.push_back(std::make_shared<const election::Ticket>(candidates[10]));
    std::reverse(verified.begin(), verified.end());
    Require(queue::IsCompetitive(candidates.front(), verified), "Better score rejected by full verified set");
    Require(!queue::IsCompetitive(candidates.back(), verified), "Worse score admitted against verified top ten");
    Require(!queue::IsCompetitive(candidates.front(), verified, 0), "Zero winner count admitted candidate");
    auto otherRound = candidates.front(); ++otherRound.context.targetEpoch;
    verified.push_back(std::make_shared<const election::Ticket>(otherRound));
    verified.push_back(nullptr);
    Require(queue::IsCompetitive(candidates.front(), verified), "Another round affected identity or score threshold");
    Require(queue::IsCompetitive(otherRound, {}), "Empty verified set rejected candidate");
    queue::TicketQueue pending;
    for (std::uint8_t i = 1; i <= 32; ++i) Require(pending.Push(Pending(i, 0)), "Pending fixture overflow");
    Require(queue::IsCompetitive(candidates.back(), {}), "Pending tickets changed verified threshold");
}
} // namespace

int main() {
    try {
        Require(sodium_init() >= 0, "libsodium initialization failed");
        TestOrdering(); TestCapacityAndDrain(); TestVerifiedThreshold();
        std::cout << "Work ticket queue checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Work ticket queue check failed: " << error.what() << '\n';
        return 1;
    }
}
