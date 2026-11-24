# EVM dependencies

The execution sources are vendored so a normal build requires no network access.

| Project | Version | Git commit | Upstream |
|---|---|---|---|
| evmone | 0.12.0 | `bf6c91df5a0e0ad9de525eca8e9d8286c8ec5ab1` | https://github.com/ipsilon/evmone |
| EVMC (Ethereum Client-VM Connector) | 12.0.0 | `8874e4784319e148b0fd75f9813cd5e5708bb67a` | https://github.com/ipsilon/evmc |
| intx | 0.11.0 | `500eec553aad838c1c33e930d5781f1246dacf0d` | https://github.com/chfast/intx |
| ethash / Keccak | 1.1.0 | `83bd5ad51acf7d7fdba215347cc805d51a194d2b` | https://github.com/chfast/ethash |

Each directory retains its upstream license. `ThirdParty/CMakeLists.txt` selects the execution sources; upstream test executables and upstream download/build scripts are not invoked.

Chain modifications to upstream sources:

- `execution_state.hpp`: use the matching Windows aligned allocation/free functions with MinGW as well as MSVC.
- `test/state/host.cpp`: obtain `CHAINID` from the transaction context and return zero for unavailable historical block hashes.
- `test/state/state.hpp`, `state.cpp`: retain returned bytes in the execution receipt and clear the transaction journal after completion.

Chain adapts the state transition and host implementation under upstream `test/state`. The upstream input/output stub tables in `precompiles_stubs.cpp` are **not compiled**. `EVM/Precompiles.cpp` provides a real bounded modular exponentiation implementation. Pairing at address `0x08` returns `EVMC_PRECOMPILE_FAILURE`; Shanghai is the selected revision, so Cancun's point-evaluation precompile is outside the selected protocol. Ecrecover, SHA-256, RIPEMD-160, identity, modular exponentiation, BN254 addition/multiplication and BLAKE2 compression are available.

Contract compilation: `Contracts/Counter.compiled.json` was produced from `Counter.sol` with Solidity `0.8.28`, optimizer runs 200, EVM target `shanghai`. Compiler version and settings are embedded in the artifact. Node execution uses the pinned source libraries above.

2026-10-13 overlay patch: `test/state/state.hpp`, `state.cpp` add a read-only parent with lazy account/slot loading, deletion markers, and cleanup restricted to each transaction's accessed accounts and slots. `host.cpp` routes storage reads, writes, warm access and create-collision checks through the overlay. Chain publishes only changed state after its atomic database write. `ThroughputCorrectness`, `ForkStateCorrectness` and `EvmNetwork` cover parent isolation, rollback, account recreation, repeated calls, nested calls, failure and restart.

## VDF dependency

| Project | Version | Git commit | Upstream |
|---|---|---|---|
| chiavdf | 1.2.3 | `b72bae6f1d013183c7d89a9e5aeef7f8215b26e9` | https://github.com/Chia-Network/chiavdf |

`chiavdf/` contains the transitive source dependencies of `verifier.h` and
`prover_slow.h`, the portable leading-zero counter, and the Windows uint128
fallback. The sources are unmodified. The upstream Apache-2.0 license, embedded
MIT notices (including PicoSHA2 and NUCOMP), the uint128 license, and the
public-domain/MIT-0 notice in `refcode/lzcnt.c` are retained. The upstream Python,
network client, hardware, assembly generation and build-download code are not
included or invoked.

The `chain_vdf` static library links system GMP and GMPXX (`libgmp-dev` on Debian
and Ubuntu; `mingw-w64-ucrt-x86_64-gmp` with MSYS2 UCRT). GMP's own license and
redistribution requirements apply to that dependency. CMake also accepts MPIR
and MPIRXX library names with their matching headers. Windows uses native
128-bit integers when the compiler supports them, otherwise upstream's fallback.

`Crypto/VDF.cpp` is the sole translation unit including the upstream headers.
It fixes the challenge at 32 bytes, discriminant at 1024 bits, initial element
at `{0x08, 0, ...}`, and proof encoding at 200 bytes (100-byte Y followed by the
100-byte Wesolowski proof). Strict form decoding validates canonical encoding.
Iterations are restricted to 1 through 100,000,000 before computation. Above
1,000,000 iterations the prover uses upstream's checkpoint implementation,
which computes the same bytes with less memory and about twice the squarings.
The caller constructs the challenge from its protocol context and supplies the
expected iterations; this primitive does not choose epochs or elect producers.

`VdfCorrectness` covers deterministic generation, small iteration counts,
challenge/iteration/proof changes, invalid lengths, canonical identity padding,
malformed forms, iteration bounds, concurrent calls, and the official fixed
proof in chiavdf `src/vdf.txt` lines 85-90. `vdf_test --extended` also generates
that 2,023,424-iteration proof through the checkpoint implementation and compares
all 200 bytes with the upstream result.
