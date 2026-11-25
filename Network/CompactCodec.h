#ifndef CHAIN_COMPACT_CODEC_H
#define CHAIN_COMPACT_CODEC_H
#include <algorithm>
#include <array>
#include <cstdint>
#include <optional>
#include <span>
#include <stdexcept>
#include <unordered_map>
#include <vector>
#include <sodium.h>

namespace chain::compact {
using Hash=std::array<uint8_t,32>;
using Key=std::array<uint8_t,crypto_shorthash_siphash24_KEYBYTES>;
constexpr size_t kHeaderBytes=112,kNonceBytes=8,kShortIdBytes=6;
constexpr size_t kMaxPrefilledTransactions=64,kMaxPrefilledBytes=65536;
inline uint64_t ReadLittle(std::span<const uint8_t> data) {
    uint64_t result=0;
    for (size_t i=0;i<data.size();++i) result|=uint64_t(data[i])<<(i*8);
    return result;
}
inline void AppendLittle(std::vector<uint8_t>& data,uint64_t value,size_t bytes) {
    for (size_t i=0;i<bytes;++i) data.push_back(uint8_t(value>>(i*8)));
}
inline Key DeriveKey(std::span<const uint8_t> header,uint64_t nonce) {
    if (header.size()!=kHeaderBytes) throw std::invalid_argument("Invalid compact header");
    std::array<uint8_t,kHeaderBytes+kNonceBytes> input{};
    std::copy(header.begin(),header.end(),input.begin());
    for (size_t i=0;i<kNonceBytes;++i) input[kHeaderBytes+i]=uint8_t(nonce>>(8*i));
    Key key{};
    crypto_generichash(key.data(),key.size(),input.data(),input.size(),nullptr,0);
    return key;
}
inline uint64_t ShortId(const Key& key,const Hash& hash) {
    std::array<uint8_t,crypto_shorthash_siphash24_BYTES> result{};
    crypto_shorthash_siphash24(result.data(),hash.data(),hash.size(),key.data());
    return ReadLittle(std::span<const uint8_t>(result).first(kShortIdBytes));
}
struct Decoded {
    uint64_t nonce=0;
    Key key{};
    std::vector<uint64_t> ids;
    std::vector<uint8_t> contracts;
    size_t transactionEnd=0;
};
inline bool Decode(std::span<const uint8_t> data,size_t maxTransactions,size_t maxExtension,Decoded& result) {
    if (data.size()<kHeaderBytes+kNonceBytes) return false;
    auto count=ReadLittle(data.subspan(72,8));
    if (count>maxTransactions||count>(SIZE_MAX-kHeaderBytes-kNonceBytes-7)/7) return false;
    const size_t bitmapBytes=(size_t(count)+7)/8;
    const size_t end=kHeaderBytes+kNonceBytes+bitmapBytes+size_t(count)*kShortIdBytes;
    if (end>data.size()||data.size()-end>maxExtension) return false;
    if (count%8&&data[kHeaderBytes+kNonceBytes+bitmapBytes-1]>>(count%8)) return false;
    Decoded decoded;
    decoded.nonce=ReadLittle(data.subspan(kHeaderBytes,kNonceBytes));
    decoded.key=DeriveKey(data.first(kHeaderBytes),decoded.nonce);
    decoded.ids.reserve(count); decoded.contracts.reserve(count);
    for (size_t i=0;i<count;++i) {
        decoded.contracts.push_back((data[kHeaderBytes+kNonceBytes+i/8]>>(i%8))&1);
        decoded.ids.push_back(ReadLittle(data.subspan(kHeaderBytes+kNonceBytes+bitmapBytes+i*kShortIdBytes,kShortIdBytes)));
    }
    decoded.transactionEnd=end;
    result=std::move(decoded);
    return true;
}
inline std::vector<uint8_t> Encode(std::span<const uint8_t> header,std::span<const Hash> hashes,
        std::span<const uint8_t> contracts,uint64_t nonce) {
    if (header.size()!=kHeaderBytes||hashes.size()!=contracts.size()||ReadLittle(header.subspan(72,8))!=hashes.size())
        throw std::invalid_argument("Invalid compact transaction count");
    auto key=DeriveKey(header,nonce);
    std::vector<uint8_t> result(header.begin(),header.end());
    AppendLittle(result,nonce,kNonceBytes);
    size_t bitmap=result.size(); result.resize(bitmap+(hashes.size()+7)/8,0);
    for (size_t i=0;i<hashes.size();++i) {
        if (contracts[i]>1) throw std::invalid_argument("Invalid compact transaction type");
        result[bitmap+i/8]|=uint8_t(contracts[i]<<(i%8));
        AppendLittle(result,ShortId(key,hashes[i]),kShortIdBytes);
    }
    return result;
}
struct PrefilledTransaction {
    uint32_t index=0;
    std::vector<uint8_t> transaction;
};
struct PrefilledDecoded {
    std::span<const uint8_t> compact;
    Decoded decoded;
    std::vector<PrefilledTransaction> transactions;
};
// Type 52 encloses the unchanged type 51 payload. Increasing indices give one
// canonical encoding and let validation reject duplicates before allocation.
inline bool DecodePrefilled(std::span<const uint8_t> data,size_t maxTransactions,size_t maxExtension,
        size_t maxTransactionBytes,PrefilledDecoded& result) {
    if (data.size()<8+kHeaderBytes+kNonceBytes) return false;
    const auto length=ReadLittle(data.first(4));
    if (length>data.size()-8) return false;
    PrefilledDecoded decoded;
    decoded.compact=data.subspan(4,size_t(length));
    if (!Decode(decoded.compact,maxTransactions,maxExtension,decoded.decoded)) return false;
    size_t offset=4+size_t(length);
    const auto count=ReadLittle(data.subspan(offset,4)); offset+=4;
    if (count>kMaxPrefilledTransactions||count>decoded.decoded.ids.size()) return false;
    std::unordered_map<uint64_t,size_t> idCounts;
    for (const auto id:decoded.decoded.ids) ++idCounts[id];
    size_t bytes=0; uint64_t previous=0;
    for (size_t i=0;i<count;++i) {
        if (data.size()-offset<8) return false;
        const auto index=ReadLittle(data.subspan(offset,4)),size=ReadLittle(data.subspan(offset+4,4)); offset+=8;
        if (index>=decoded.decoded.ids.size()||(i&&index<=previous)||size<176||size>maxTransactionBytes||
                size>data.size()-offset||size>kMaxPrefilledBytes-bytes) return false;
        if (idCounts[decoded.decoded.ids[index]]!=1) return false;
        Hash hash{}; std::copy_n(data.begin()+offset+80,32,hash.begin());
        if (ShortId(decoded.decoded.key,hash)!=decoded.decoded.ids[index]) return false;
        if (decoded.decoded.contracts[index]?(size<221):size!=176) return false;
        decoded.transactions.push_back({uint32_t(index),{data.begin()+offset,data.begin()+offset+size}});
        offset+=size; bytes+=size; previous=index;
    }
    if (offset!=data.size()) return false;
    result=std::move(decoded); return true;
}
inline std::vector<uint8_t> EncodePrefilled(std::span<const uint8_t> compact,
        std::span<const PrefilledTransaction> transactions) {
    if (compact.size()>UINT32_MAX||transactions.size()>kMaxPrefilledTransactions)
        throw std::invalid_argument("Too many prefilled transactions");
    std::vector<uint8_t> result; result.reserve(8+compact.size());
    AppendLittle(result,compact.size(),4); result.insert(result.end(),compact.begin(),compact.end());
    AppendLittle(result,transactions.size(),4);
    size_t bytes=0; uint32_t previous=0;
    for (size_t i=0;i<transactions.size();++i) {
        const auto& entry=transactions[i];
        if ((i&&entry.index<=previous)||entry.transaction.size()>kMaxPrefilledBytes-bytes)
            throw std::invalid_argument("Invalid prefilled transaction order or bytes");
        AppendLittle(result,entry.index,4); AppendLittle(result,entry.transaction.size(),4);
        result.insert(result.end(),entry.transaction.begin(),entry.transaction.end());
        bytes+=entry.transaction.size(); previous=entry.index;
    }
    return result;
}
// A repeated short ID, or two different local transaction hashes with the same
// ID, must be fetched by index. Never choose the first hash in a collision.
class ShortIdIndex {
    std::unordered_map<uint64_t,size_t> positions;
    std::vector<std::optional<Hash>> hashes;
    std::vector<bool> ambiguous;
public:
    explicit ShortIdIndex(std::span<const uint64_t> ids):hashes(ids.size()),ambiguous(ids.size(),false) {
        positions.reserve(ids.size());
        for (size_t i=0;i<ids.size();++i) {
            auto [found,inserted]=positions.emplace(ids[i],i);
            if (!inserted) { ambiguous[found->second]=true; ambiguous[i]=true; }
        }
    }
    std::optional<size_t> Find(uint64_t id) const {
        auto found=positions.find(id);
        return found==positions.end()?std::nullopt:std::optional<size_t>(found->second);
    }
    void Observe(size_t position,const Hash& hash) {
        if (ambiguous[position]) return;
        if (hashes[position]&&*hashes[position]!=hash) { ambiguous[position]=true; hashes[position].reset(); }
        else hashes[position]=hash;
    }
    bool IsAmbiguous(size_t position) const { return ambiguous[position]; }
    const std::optional<Hash>& MatchedHash(size_t position) const { return hashes[position]; }
};
}
#endif
