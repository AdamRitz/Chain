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
