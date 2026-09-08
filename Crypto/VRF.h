//
// Created by 61485 on 2026/5/20.
//

#ifndef CHAIN_VRF_H
#define CHAIN_VRF_H
#include <sodium.h>
#include <array>
#include <vector>
#include <cstring>
#include "../Key/Key.h"
// 标量 1
array<uint8_t, crypto_core_ed25519_SCALARBYTES> one{1};
array<uint8_t, crypto_core_ed25519_SCALARBYTES> eight{8};
array<uint8_t, crypto_core_ed25519_BYTES> g;
struct VRFWallet {
    array<uint8_t, crypto_core_ed25519_SCALARBYTES> sk;
    array<uint8_t, crypto_core_ed25519_BYTES> pk;
};
VRFWallet myVRFWallet;

void InitVRFWallet() {
    crypto_core_ed25519_scalar_random(myVRFWallet.sk.data());
    if (crypto_scalarmult_ed25519_base_noclamp(myVRFWallet.pk.data(), myVRFWallet.sk.data())!=0) throw runtime_error("VRF curve operation failed");
    if (crypto_scalarmult_ed25519_base_noclamp(g.data(), one.data())!=0) throw runtime_error("VRF curve operation failed");
}

vector<uint8_t> VRFGen(const vector<uint8_t>& m) {
    // h = H(m) 把明文数据转为曲线点
    array<uint8_t, 32> hash;
    crypto_generichash(hash.data(),32,m.data(),m.size(),nullptr,0);
    array<uint8_t,crypto_core_ed25519_BYTES> h;
    crypto_core_ed25519_from_uniform(h.data(), hash.data());
    // γ = h^x
    array<uint8_t,crypto_core_ed25519_BYTES> gammar;
    if (crypto_scalarmult_ed25519_noclamp(gammar.data(),myVRFWallet.sk.data(),h.data())!=0) throw runtime_error("VRF curve operation failed");
    // 随机选取 k
    array<uint8_t,crypto_core_ed25519_SCALARBYTES> k;
    crypto_core_ed25519_scalar_random(k.data());
    // c = H(g,h,g^x,h^x,g^k,h^k);
    array<uint8_t,crypto_core_ed25519_BYTES*6> cByte;
    int offset = 0;
    memcpy(cByte.data()+offset,g.data(),crypto_core_ed25519_BYTES);
    offset+=crypto_core_ed25519_BYTES;

    memcpy(cByte.data()+offset,h.data(),h.size());                      // 填充 h
    offset+=crypto_core_ed25519_BYTES;

    array<uint8_t, crypto_core_ed25519_BYTES> gx;
    if (crypto_scalarmult_ed25519_base_noclamp(gx.data(),myVRFWallet.sk.data())!=0) throw runtime_error("VRF curve operation failed");    // 计算 gx
    memcpy(cByte.data()+offset,gx.data(),gx.size());
    offset+=crypto_core_ed25519_BYTES;

    array<uint8_t, crypto_core_ed25519_BYTES> hx;                                        // 计算 hx
    if (crypto_scalarmult_ed25519_noclamp(hx.data(),myVRFWallet.sk.data(),h.data())!=0) throw runtime_error("VRF curve operation failed");
    memcpy(cByte.data()+offset,hx.data(),hx.size());
    offset+=crypto_core_ed25519_BYTES;

    array<uint8_t, crypto_core_ed25519_BYTES> gk;                                        // 计算 gk
    if (crypto_scalarmult_ed25519_base_noclamp(gk.data(),k.data())!=0) throw runtime_error("VRF curve operation failed");
    memcpy(cByte.data()+offset,gk.data(),g.size());
    offset+=crypto_core_ed25519_BYTES;

    array<uint8_t, crypto_core_ed25519_BYTES> hk;                                       // 计算 hk
    if (crypto_scalarmult_ed25519_noclamp(hk.data(),k.data(),h.data())!=0) throw runtime_error("VRF curve operation failed");
    memcpy(cByte.data()+offset,hk.data(),hk.size());
    offset+=crypto_core_ed25519_BYTES;

    array<uint8_t,64> cHash;                                                            // 计算 cHash
    crypto_generichash(cHash.data(),64,cByte.data(),cByte.size(),nullptr,0);
    array<uint8_t,crypto_core_ed25519_SCALARBYTES> cScalar;
    crypto_core_ed25519_scalar_reduce(cScalar.data(),cHash.data());
    // 计算 s =  k - cx;
    array<uint8_t,crypto_core_ed25519_SCALARBYTES> cx;
    crypto_core_ed25519_scalar_mul(cx.data(),cScalar.data(),myVRFWallet.sk.data());

    array<uint8_t,crypto_core_ed25519_SCALARBYTES> s;                                   // 计算 s
    crypto_core_ed25519_scalar_sub(s.data(),k.data(),cx.data());
    // 计算 VRFValue 和 填充输出数据
    vector<uint8_t> Output;
    Output.resize(32+crypto_core_ed25519_BYTES+crypto_core_ed25519_SCALARBYTES*2);
    array<uint8_t,crypto_core_ed25519_BYTES> gammarf;
    if (crypto_scalarmult_ed25519_noclamp(gammarf.data(),eight.data(),gammar.data())!=0) throw runtime_error("VRF curve operation failed");
    // Output =  VRFValue | Proof[gammar.c,s]
    crypto_generichash(Output.data(),32,gammarf.data(),gammarf.size(),nullptr,0);
    offset=0;
    offset += gammarf.size();
    memcpy(Output.data()+offset,gammar.data(),gammar.size());
    offset += gammar.size();
    memcpy(Output.data()+offset,cScalar.data(),cScalar.size());
    offset += cScalar.size();
    memcpy(Output.data()+offset,s.data(),s.size());
    return Output;
}

bool VRFVerify(const vector<uint8_t>& message,const vector<uint8_t>& m,const array<uint8_t,crypto_core_ed25519_BYTES>& pk) {
    if (message.size()!=128||crypto_core_ed25519_is_valid_point(pk.data())!=1||crypto_core_ed25519_is_valid_point(message.data()+32)!=1) return false;
    array<uint8_t,32> base;
    if (crypto_scalarmult_ed25519_base_noclamp(base.data(),one.data())!=0) return false;
    for (size_t offset: {size_t(64),size_t(96)}) {
        array<uint8_t,64> scalar{};
        array<uint8_t,32> reduced;
        memcpy(scalar.data(),message.data()+offset,32);
        crypto_core_ed25519_scalar_reduce(reduced.data(),scalar.data());
        if (sodium_memcmp(reduced.data(),message.data()+offset,32)!=0) return false;
    }
    // 反序列化消息 message = VRFValue | Proof[gammar,c,s]
    array<uint8_t,32> VRFValue;
    array<uint8_t,crypto_core_ed25519_BYTES> gammar;
    array<uint8_t,crypto_core_ed25519_SCALARBYTES> c,s;
    int offset = 0;
    memcpy(VRFValue.data(),message.data()+offset,VRFValue.size());
    offset += VRFValue.size();
    memcpy(gammar.data(),message.data()+offset,gammar.size());
    offset += gammar.size();
    memcpy(c.data(),message.data()+offset,c.size());
    offset += c.size();
    memcpy(s.data(),message.data()+offset,s.size());
    // 验证 VRFValue
    array<uint8_t,crypto_core_ed25519_BYTES> pkc;
    if (crypto_scalarmult_ed25519_noclamp(pkc.data(),c.data(),pk.data())!=0) return false;
    array<uint8_t,crypto_core_ed25519_BYTES> gs;
    if (crypto_scalarmult_ed25519_base_noclamp(gs.data(),s.data())!=0) return false;
    array<uint8_t,crypto_core_ed25519_BYTES> u;
    // u = pk^c * g^s
    if (crypto_core_ed25519_add(u.data(),gs.data(),pkc.data())!=0) return false;
    array<uint8_t,32> hash;
    crypto_generichash(hash.data(),32,m.data(),m.size(),nullptr,0);
    array<uint8_t, crypto_core_ed25519_BYTES> h;
    crypto_core_ed25519_from_uniform(h.data(),hash.data());

    array<uint8_t,crypto_core_ed25519_BYTES> gammarc,hs;
    if (crypto_scalarmult_ed25519_noclamp(gammarc.data(),c.data(),gammar.data())!=0) return false;
    if (crypto_scalarmult_ed25519_noclamp(hs.data(),s.data(),h.data())!=0) return false;
    array<uint8_t,crypto_core_ed25519_BYTES> v;
    if (crypto_core_ed25519_add(v.data(),gammarc.data(),hs.data())!=0) return false;
    // cHash = Hash(cByte) = Hash (g,h,PK,gammar,u,v)
    array<uint8_t,64> cHash;
    array<uint8_t,crypto_core_ed25519_BYTES*6> cByte;
    offset = 0;
    memcpy(cByte.data()+offset,base.data(),base.size());
    offset += g.size();
    memcpy(cByte.data()+offset,h.data(),h.size());
    offset += h.size();
    memcpy(cByte.data()+offset,pk.data(),pk.size());
    offset += pk.size();
    memcpy(cByte.data()+offset,gammar.data(),gammar.size());
    offset += gammar.size();
    memcpy(cByte.data()+offset,u.data(),u.size());
    offset += u.size();
    memcpy(cByte.data()+offset,v.data(),v.size());
    crypto_generichash(cHash.data(),64,cByte.data(),cByte.size(),nullptr,0);
    array<uint8_t,crypto_core_ed25519_SCALARBYTES> cScalar;
    crypto_core_ed25519_scalar_reduce(cScalar.data(),cHash.data());
    if (sodium_memcmp(c.data(),cScalar.data(),crypto_core_ed25519_SCALARBYTES)!=0) {
        return false;
    };
    // 计算 VRF
    array<uint8_t,crypto_core_ed25519_BYTES> gammarf;
    array<uint8_t,32> beta;
    if (crypto_scalarmult_ed25519_noclamp(gammarf.data(),eight.data(),gammar.data())!=0) return false;
    crypto_generichash(beta.data(),32,gammarf.data(),gammarf.size(),nullptr,0);

    if (sodium_memcmp(beta.data(),VRFValue.data(),32)!=0) {
        return false;
    }
    return true;
}

#endif //CHAIN_VRF_H
