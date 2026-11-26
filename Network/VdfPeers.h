#pragma once
#include "VdfPeerProtocol.h"

// All directory, roster and authenticated-connection fields use peerMutex.
// Read chain state before taking that mutex; never hold it during socket work.
constexpr size_t vdfPeerReserveLimit=9,vdfPeerPendingLimit=4;
struct VdfPeerEndpoint {
    chain::vdf_peer::EndpointHint hint;
    uint64_t seen=0;
    bool direct=false;
};
struct VdfPeerTarget {array<uint8_t,32> publicKey;ip::address address;uint16_t port;bool blockOnly=false;};
vector<array<uint8_t,32>> vdfCurrentProducers,vdfNextProducers;
map<array<uint8_t,32>,vector<VdfPeerEndpoint>> vdfPeerEndpoints;
set<array<uint8_t,32>> vdfBlockEndpoints,vdfLegacyEndpoints;
bool vdfLocalParticipant=false,vdfHintsDirty=false;
uint64_t vdfHintsPublishedAt=0;
atomic<uint64_t> vdfPeerAuthenticationFailures{0};
array<uint8_t,32> vdfLastBlockHash{};
uint64_t vdfLastBlockSlot=0,vdfLastBlockEpoch=0,vdfLastBlockHeight=0;
vector<string> vdfLastBlockRecipients;

bool VdfProducerMeshEnabled() {
    return VdfConsensus::config.enabled&&VdfConsensus::config.producerRotation;
}
bool ContainsVdfKey(const vector<array<uint8_t,32>>& keys,const array<uint8_t,32>& key) {
    return find(keys.begin(),keys.end(),key)!=keys.end();
}
bool WantedVdfKeyLocked(const array<uint8_t,32>& key) {
    return ContainsVdfKey(vdfCurrentProducers,key)||ContainsVdfKey(vdfNextProducers,key);
}
size_t VdfConnectionLimit() {
    return maxConnections+(VdfProducerMeshEnabled()?vdfPeerReserveLimit+vdfPeerPendingLimit:0);
}
bool AdmitVdfPeerLocked(const shared_ptr<Peer>& peer) {
    size_t ordinary=0,pending=0;
    for (const auto& [id,entry]:peerPool) if (!entry->closePosted) {
        if (entry->vdfPending) ++pending;
        if (entry->ordinarySlot) ++ordinary;
    }
    if (peerPool.size()>=maxPeers+(VdfProducerMeshEnabled()?vdfPeerReserveLimit+vdfPeerPendingLimit:0)) return false;
    // A producer dial uses the extra producer capacity, never a vacant place
    // intended for the configured/discovered ordinary network.
    if (!peer->blockOnly&&!peer->expectedVdfKey&&ordinary<maxPeers) {
        peer->ordinarySlot=true;return true;
    }
    if (!VdfProducerMeshEnabled()||!vdfLocalParticipant||pending>=vdfPeerPendingLimit) return false;
    if (peerPool.size()>=maxPeers+vdfPeerReserveLimit+vdfPeerPendingLimit) return false;
    peer->vdfPending=true;peer->vdfPendingAt=GetSteadyTime();
    return true;
}
bool IsPendingVdfPeer(const shared_ptr<Peer>& peer) {
    lock_guard lock(peerMutex);return peer->vdfPending;
}
void RememberVdfPeerProtocolLocked(const shared_ptr<Peer>& peer) {
    if (!peer->vdfAuthenticated||!WantedVdfKeyLocked(peer->vdfPeerKey)) return;
    if (peer->blockOnly||peer->blockRelayProtocol) {
        vdfBlockEndpoints.insert(peer->vdfPeerKey);vdfLegacyEndpoints.erase(peer->vdfPeerKey);
    } else if (peer->capabilitiesReceived&&!vdfBlockEndpoints.count(peer->vdfPeerKey)) {
        vdfLegacyEndpoints.insert(peer->vdfPeerKey);
    }
}
chain::vdf_peer::EndpointHint VdfEndpointHint(const array<uint8_t,32>& key,ip::address address,uint16_t port) {
    chain::vdf_peer::EndpointHint hint;
    hint.publicKey=key;hint.port=port;address=CanonicalAddress(address);
    hint.family=address.is_v4()?4:6;
    if (address.is_v4()) {auto bytes=address.to_v4().to_bytes();copy(bytes.begin(),bytes.end(),hint.address.begin());}
    else {auto bytes=address.to_v6().to_bytes();copy(bytes.begin(),bytes.end(),hint.address.begin());}
    return hint;
}
ip::address VdfHintAddress(const chain::vdf_peer::EndpointHint& hint) {
    if (hint.family==4) {ip::address_v4::bytes_type bytes;copy_n(hint.address.begin(),4,bytes.begin());return ip::address_v4(bytes);}
    ip::address_v6::bytes_type bytes;copy(hint.address.begin(),hint.address.end(),bytes.begin());
    return CanonicalAddress(ip::address_v6(bytes));
}
bool RememberVdfEndpointLocked(const chain::vdf_peer::EndpointHint& hint,bool direct,uint64_t now) {
    if (!WantedVdfKeyLocked(hint.publicKey)||hint.publicKey==vdfPublicKey) return false;
    auto& entries=vdfPeerEndpoints[hint.publicKey];
    auto found=find_if(entries.begin(),entries.end(),[&](const auto& entry){return entry.hint==hint;});
    if (found!=entries.end()) {
        found->seen=now;
        if (direct&&!found->direct) {found->direct=true;return true;}
        return false;
    }
    if (entries.size()==4) {
        auto oldest=min_element(entries.begin(),entries.end(),[](const auto& a,const auto& b) {
            return a.direct!=b.direct?!a.direct:a.seen<b.seen;
        });
        if (!direct&&oldest->direct) return false;
        entries.erase(oldest);
    }
    entries.push_back({hint,now,direct});return true;
}
void SendVdfPeerHints(const shared_ptr<Peer>& peer) {
    if (!peer->vdfPeerProtocol||!peer->vdfChainMatched||peer->closePosted) return;
    vector<chain::vdf_peer::EndpointHint> hints;
    {
        lock_guard lock(peerMutex);
        for (const auto& [key,entries]:vdfPeerEndpoints) {
            if (entries.empty()||!WantedVdfKeyLocked(key)) continue;
            auto best=max_element(entries.begin(),entries.end(),[](const auto& a,const auto& b) {
                return a.direct!=b.direct?!a.direct:a.seen<b.seen;
            });
            hints.push_back(best->hint);
            if (hints.size()==chain::vdf_peer::kMaxHints) break;
        }
    }
    if (!hints.empty()) SendData(peer,GenerateMessage(chain::vdf_peer::kHintsType,chain::vdf_peer::EncodeHints(hints)));
}
void BeginVdfPeerAuthentication(const shared_ptr<Peer>& peer) {
    if (!VdfProducerMeshEnabled()||!peer->vdfPeerProtocol||peer->vdfChallengeSent) return;
    peer->vdfChallenge=chain::vdf_peer::MakeChallenge();
    peer->vdfChallengeAt=GetSteadyTime();peer->vdfChallengeSent=true;
    SendData(peer,GenerateMessage(chain::vdf_peer::kChallengeType,peer->vdfChallenge));
    SendVdfPeerHints(peer);
}
void ReceiveVdfPeerChallenge(const shared_ptr<Peer>& peer,span<const uint8_t> data) {
    if (!VdfProducerMeshEnabled()) return;
    // Stats replies run on a worker and can arrive after the challenge.
    // Receiving this negotiated protocol message also establishes support.
    peer->vdfPeerProtocol=true;
    BeginVdfPeerAuthentication(peer);
    if (!vdfIdentityReady||peer->vdfChallengeAnswered) return;
    chain::vdf_peer::Challenge challenge;copy(data.begin(),data.end(),challenge.begin());
    const auto identity=chain::vdf_peer::CreateIdentity(genesisRoot,challenge,listenPort,vdfSecretKey);
    peer->vdfChallengeAnswered=true;
    SendData(peer,GenerateMessage(chain::vdf_peer::kIdentityType,chain::vdf_peer::EncodeIdentity(identity)));
}
void ReceiveVdfPeerIdentity(const shared_ptr<Peer>& peer,span<const uint8_t> data) {
    chain::vdf_peer::Identity identity;
    if (!peer->vdfPeerProtocol||!peer->vdfChallengeSent||
            GetSteadyTime()-peer->vdfChallengeAt>5'000'000'000ULL||
            !chain::vdf_peer::DecodeIdentity(data,identity)||
            !chain::vdf_peer::VerifyIdentity(identity,genesisRoot,peer->vdfChallenge,peer->port)||
            (peer->expectedVdfKey&&identity.publicKey!=*peer->expectedVdfKey)||identity.publicKey==vdfPublicKey) {
        if (peer->expectedVdfKey) peerRetrySchedule.Record({peer->address.to_string(),peer->port},false,GetSteadyTime());
        vdfPeerAuthenticationFailures++;RejectSend(peer);return;
    }
    vector<shared_ptr<Peer>> close;
    bool first=false;
    {
        lock_guard lock(peerMutex);
        if (peer->vdfAuthenticated) {
            if (peer->vdfPeerKey!=identity.publicKey) close.push_back(peer);
        } else {
            first=true;
            peer->vdfAuthenticated=true;peer->vdfPeerKey=identity.publicKey;
            peer->vdfReserved=!peer->ordinarySlot&&vdfLocalParticipant&&WantedVdfKeyLocked(identity.publicKey);
            RememberVdfPeerProtocolLocked(peer);
            if (peer->vdfPending&&!peer->vdfReserved) close.push_back(peer);
            else {
                peer->vdfPending=false;
                vdfHintsDirty|=RememberVdfEndpointLocked(VdfEndpointHint(identity.publicKey,peer->address,peer->port),true,GetSteadyTime());
                // Both ends choose the same physical connection, including when
                // inbound and outbound handshakes finish in different orders.
                const bool preferOutbound=vdfIdentityReady?vdfPublicKey<identity.publicKey:NodeRelayInstance()<peer->remoteInstance;
                auto keep=peer;
                for (const auto& [id,other]:peerPool) {
                    if (!vdfIdentityReady&&peer->remoteInstance.empty()) break;
                    if (other==peer||other->closePosted||other->blockOnly!=peer->blockOnly||!other->vdfAuthenticated||other->vdfPeerKey!=identity.publicKey) continue;
                    if ((other->outbound==preferOutbound&&keep->outbound!=preferOutbound)||
                            (other->outbound==keep->outbound&&other->key<keep->key)) {
                        TransferOrdinarySlotLocked(other,keep);close.push_back(keep);keep=other;
                    } else {TransferOrdinarySlotLocked(keep,other);close.push_back(other);}
                }
            }
        }
    }
    for (const auto& entry:close) RejectSend(entry);
    SendVdfPeerHints(peer);
    if (first&&!peer->closePosted) {
        SendData(peer,GenerateMessage(19,{}));
        if (!peer->blockOnly) SendData(peer,GenerateMessage(50,{}));
    }
}
void ReceiveVdfPeerHints(const shared_ptr<Peer>& peer,span<const uint8_t> data) {
    if (!VdfProducerMeshEnabled()||!peer->vdfPeerProtocol||!peer->vdfChainMatched) return;
    auto now=GetSteadyTime();
    if (now-peer->vdfHintsAt<100'000'000ULL) return;
    peer->vdfHintsAt=now;
    vector<chain::vdf_peer::EndpointHint> hints;
    if (!chain::vdf_peer::DecodeHints(data,hints)) {RejectSend(peer);return;}
    lock_guard lock(peerMutex);
    for (auto hint:hints) {
        auto address=VdfHintAddress(hint);
        if (address.is_v6()&&(address.to_v6().is_link_local()||address.to_v6().scope_id())) continue;
        if (address.is_loopback()&&hint.port==listenPort) continue;
        bool conflict=false;
        for (const auto& [id,entry]:peerPool) if (!entry->closePosted&&entry->vdfAuthenticated&&
                entry->address==address&&entry->port==hint.port&&entry->vdfPeerKey!=hint.publicKey) conflict=true;
        if (!conflict) vdfHintsDirty|=RememberVdfEndpointLocked(VdfEndpointHint(hint.publicKey,address,hint.port),false,now);
    }
}
vector<VdfPeerTarget> RefreshVdfProducerPeers() {
    vector<array<uint8_t,32>> current,next;
    {
        shared_lock lock(dbCommitMutex);
        auto height=DBReadBlockHeight()+1;
        for (const auto& record:DBVdfActiveCandidates(height)) current.push_back(record.publicKey);
        auto candidates=DBVdfCandidatesForNextBlock();
        auto runtime=ReadVdfCandidates(DBVdfContext(VdfConsensus::Epoch(height)+1));
        candidates.insert(candidates.end(),runtime.begin(),runtime.end());
        for (const auto& record:MergeVdfCandidates(std::move(candidates))) next.push_back(record.publicKey);
    }
    vector<shared_ptr<Peer>> close,broadcast;
    vector<VdfPeerTarget> targets;
    auto now=GetSteadyTime();
    {
        lock_guard lock(peerMutex);
        if (current!=vdfCurrentProducers||next!=vdfNextProducers) vdfHintsDirty=true;
        vdfCurrentProducers=std::move(current);vdfNextProducers=std::move(next);
        vdfLocalParticipant=vdfIdentityReady&&WantedVdfKeyLocked(vdfPublicKey);
        erase_if(vdfBlockEndpoints,[](const auto& key){return !WantedVdfKeyLocked(key);});
        erase_if(vdfLegacyEndpoints,[](const auto& key){return !WantedVdfKeyLocked(key);});
        set<array<uint8_t,32>> blockConnected;
        for (const auto& [id,peer]:peerPool) if (!peer->closePosted&&peer->vdfAuthenticated) {
            if (peer->blockOnly) blockConnected.insert(peer->vdfPeerKey);
            RememberVdfPeerProtocolLocked(peer);
        }
        for (auto it=vdfPeerEndpoints.begin();it!=vdfPeerEndpoints.end();) {
            erase_if(it->second,[&](const auto& entry){return now-entry.seen>30'000'000'000ULL;});
            if (!WantedVdfKeyLocked(it->first)||it->second.empty()) it=vdfPeerEndpoints.erase(it);else ++it;
        }
        for (const auto& [id,peer]:peerPool) {
            if (peer->closePosted) continue;
            const auto wanted=vdfLocalParticipant&&peer->vdfAuthenticated&&WantedVdfKeyLocked(peer->vdfPeerKey);
            auto reserved=!peer->ordinarySlot&&wanted&&(peer->blockOnly||!blockConnected.count(peer->vdfPeerKey));
            peer->vdfReserved=reserved;
            if (peer->vdfPending&&now-peer->vdfPendingAt>3'000'000'000ULL) {close.push_back(peer);continue;}
            // Ordinary places survive elections. Extra connections expire
            // with their producer role or the replacement block connection.
            if (!peer->ordinarySlot&&!reserved&&!peer->vdfPending) {close.push_back(peer);continue;}
            if (peer->vdfAuthenticated)
                vdfHintsDirty|=RememberVdfEndpointLocked(VdfEndpointHint(peer->vdfPeerKey,peer->address,peer->port),true,now);
        }
        if (vdfLocalParticipant) for (const auto& [key,entries]:vdfPeerEndpoints) {
            if (key==vdfPublicKey||!WantedVdfKeyLocked(key)) continue;
            // Hints do not carry capabilities. Try the block handshake first;
            // only an authenticated capability reply establishes a legacy peer.
            const bool blockOnly=!vdfLegacyEndpoints.count(key);
            bool connected=false;
            for (const auto& [id,peer]:peerPool) if (!peer->closePosted&&peer->blockOnly==blockOnly&&peer->vdfAuthenticated&&peer->vdfPeerKey==key) connected=true;
            if (connected) continue;
            vector<VdfPeerEndpoint> ranked=entries;
            stable_sort(ranked.begin(),ranked.end(),[](const auto& a,const auto& b){return a.direct!=b.direct?a.direct:a.seen>b.seen;});
            for (const auto& entry:ranked) {
                auto address=VdfHintAddress(entry.hint);bool occupied=false;
                for (const auto& [id,peer]:peerPool) if (!peer->closePosted&&peer->blockOnly==blockOnly&&peer->address==address&&peer->port==entry.hint.port) occupied=true;
                if (occupied||(blockOnly?blockAddressAttempts:addressAttempts).count({address.to_string(),entry.hint.port})||
                        !peerRetrySchedule.Ready({address.to_string(),entry.hint.port},now)) continue;
                targets.push_back({key,address,entry.hint.port,blockOnly});break;
            }
        }
        if (vdfHintsDirty||now-vdfHintsPublishedAt>=2'000'000'000ULL) {
            for (const auto& [id,peer]:peerPool) if (!peer->closePosted) broadcast.push_back(peer);
            vdfHintsPublishedAt=now;vdfHintsDirty=false;
        }
    }
    for (const auto& peer:close) RejectSend(peer);
    for (const auto& peer:broadcast) post(peer->strand,[peer]{SendVdfPeerHints(peer);});
    return targets;
}
vector<shared_ptr<Peer>> PrioritizeVdfBlockPeers(const vector<uint8_t>& data,vector<shared_ptr<Peer>> ordinary,uint64_t source) {
    if (!VdfProducerMeshEnabled()) return ordinary;
    optional<VdfConsensus::Extension> extension;
    if (!ReadVdfExtension(data,extension)||!extension) return ordinary;
    const auto height=ReadU64(data.data()+64),epoch=VdfConsensus::Epoch(height);
    vector<array<uint8_t,32>> order;
    {
        shared_lock lock(dbCommitMutex);
        for (const auto& candidate:DBVdfActiveCandidates(height)) order.push_back(candidate.publicKey);
    }
    vector<shared_ptr<Peer>> result;
    lock_guard lock(peerMutex);
    auto next=order;
    if (VdfConsensus::Epoch(height+1)!=epoch) {
        next.clear();for (const auto& candidate:extension->candidates) next.push_back(candidate.publicKey);
    }
    if (!next.empty()) {
        auto key=next.front();
        if (VdfConsensus::config.consecutiveBlocks&&extension->turn) {
            if (VdfConsensus::Epoch(height+1)==epoch) {
                const auto index=extension->turn->number%next.size();
                key=next[(index+(extension->turn->blocks?1:0))%next.size()];
            }
        } else key=next[(extension->slot+1-VdfConsensus::StartSlot())%next.size()];
        erase(order,key);order.insert(order.begin(),key);
    }
    set<pair<ip::address,uint16_t>> sent;
    optional<pair<ip::address,uint16_t>> from;
    if (auto entry=peerPool.find(source);entry!=peerPool.end()) from=make_pair(entry->second->address,entry->second->port);
    auto append=[&](const shared_ptr<Peer>& peer) {
        auto endpoint=make_pair(peer->address,peer->port);
        if (peer->closePosted||!peer->port||peer->key==source||(from&&endpoint==*from)||!sent.insert(endpoint).second) return;
        result.push_back(peer);
    };
    for (const auto& key:order) {
        shared_ptr<Peer> chosen;
        for (const auto& [id,peer]:peerPool)
            if (!peer->closePosted&&peer->vdfAuthenticated&&peer->vdfPeerKey==key&&(!chosen||peer->blockOnly)) chosen=peer;
        if (chosen) append(chosen);
    }
    for (const auto& peer:ordinary) append(peer);
    if (extension->producer==vdfPublicKey) {
        memcpy(vdfLastBlockHash.data(),data.data()+80,32);
        vdfLastBlockSlot=extension->slot;vdfLastBlockEpoch=epoch;vdfLastBlockHeight=height;vdfLastBlockRecipients.clear();
    }
    return result;
}
void RecordVdfBlockRecipient(const vector<uint8_t>& data,const shared_ptr<Peer>& peer) {
    if (!VdfProducerMeshEnabled()) return;
    lock_guard lock(peerMutex);
    if (peer->vdfAuthenticated&&!memcmp(vdfLastBlockHash.data(),data.data()+80,32))
        vdfLastBlockRecipients.push_back(U32ToHex(peer->vdfPeerKey));
}
nlohmann::json GetVdfPeerStats() {
    nlohmann::json connections=nlohmann::json::array(),current=nlohmann::json::array(),next=nlohmann::json::array(),following=nlohmann::json::array();
    nlohmann::json ordinary=nlohmann::json::array();
    size_t pending=0,blockConnections=0;set<array<uint8_t,32>> seen;
    lock_guard lock(peerMutex);
    for (const auto& key:vdfNextProducers) following.push_back(U32ToHex(key));
    vector<shared_ptr<Peer>> peers;
    for (const auto& [id,peer]:peerPool) peers.push_back(peer);
    stable_sort(peers.begin(),peers.end(),[](const auto& a,const auto& b){return a->blockOnly>b->blockOnly;});
    for (const auto& peer:peers) {
        if (peer->closePosted) continue;
        pending+=peer->vdfPending;
        blockConnections+=peer->blockOnly&&peer->vdfAuthenticated;
        if (!peer->blockOnly) ordinary.push_back({{"connection_id",peer->key},
            {"public_key",peer->vdfAuthenticated?U32ToHex(peer->vdfPeerKey):string{}},
            {"address",peer->address.to_string()},{"port",peer->port},{"ordinary_slot",peer->ordinarySlot},
            {"authenticated",peer->vdfAuthenticated},{"pending",peer->vdfPending},
            {"reserved",peer->vdfReserved},{"outbound",peer->outbound}});
        if (!peer->vdfAuthenticated||!seen.insert(peer->vdfPeerKey).second) continue;
        auto publicKey=U32ToHex(peer->vdfPeerKey);
        connections.push_back({{"public_key",publicKey},{"address",peer->address.to_string()},{"port",peer->port},{"reserved",peer->vdfReserved},{"block_connection",peer->blockOnly}});
        if (ContainsVdfKey(vdfCurrentProducers,peer->vdfPeerKey)) current.push_back(publicKey);
        if (ContainsVdfKey(vdfNextProducers,peer->vdfPeerKey)) next.push_back(publicKey);
    }
    return {{"producer_mesh",VdfProducerMeshEnabled()},{"producer_peers",current},{"next_producers",following},
        {"next_producer_peers",next},{"peer_connections",connections},{"peer_reserve_limit",vdfPeerReserveLimit},
        {"peer_pending_limit",vdfPeerPendingLimit},{"peer_pending",pending},{"peer_authentication_failures",vdfPeerAuthenticationFailures.load()},
        {"block_connections",blockConnections},{"ordinary_connections",ordinary},
        {"last_block_recipients",vdfLastBlockRecipients},{"last_block_slot",vdfLastBlockSlot},
        {"last_block_epoch",vdfLastBlockEpoch},{"last_block_height",vdfLastBlockHeight},{"last_block_hash",U32ToHex(vdfLastBlockHash)}};
}
