// Chain adapters for precompiles not implemented by evmone 0.12's state library.
#include <state/precompiles_stubs.hpp>
#include <boost/multiprecision/cpp_int.hpp>
#include <algorithm>
namespace evmone::state {
ExecutionResult expmod_stub(const uint8_t* input,size_t size,uint8_t* output,size_t capacity) noexcept {
    using boost::multiprecision::cpp_int;
    auto number=[&](size_t offset,size_t length) {
        cpp_int value=0;
        for (size_t i=0;i<length;i++) { value<<=8; if (offset+i<size) value+=input[offset+i]; }
        return value;
    };
    auto a=number(0,32),b=number(32,32),c=number(64,32);
    // Bound allocations independently of the advertised input lengths.
    if (a>65536||b>65536||c>65536) return {EVMC_PRECOMPILE_FAILURE,0};
    auto al=a.convert_to<size_t>(),bl=b.convert_to<size_t>(),cl=c.convert_to<size_t>();
    if (cl>capacity) return {EVMC_PRECOMPILE_FAILURE,0};
    if (!cl) return {EVMC_SUCCESS,0};
    auto modulus=number(96+al+bl,cl);
    cpp_int value=0;
    if (modulus!=0) value=boost::multiprecision::powm(number(96,al),number(96+al,bl),modulus);
    for (size_t i=cl;i>0;i--) { output[i-1]=static_cast<uint8_t>(value&255); value>>=8; }
    return {EVMC_SUCCESS,cl};
}
ExecutionResult ecpairing_stub(const uint8_t*,size_t,uint8_t*,size_t) noexcept {
    return {EVMC_PRECOMPILE_FAILURE,0};
}
ExecutionResult point_evaluation_stub(const uint8_t*,size_t,uint8_t*,size_t) noexcept {
    return {EVMC_PRECOMPILE_FAILURE,0};
}
}
