# Pinned upstream sources; see VERSIONS.md. Only the execution libraries are built.
file(GLOB EVMONE_SOURCES CONFIGURE_DEPENDS evmone/lib/evmone/*.cpp)
list(FILTER EVMONE_SOURCES EXCLUDE REGEX "cpu_check.cpp$")
file(GLOB PRECOMPILE_SOURCES CONFIGURE_DEPENDS evmone/lib/evmone_precompiles/*.cpp)
add_library(chain_evm STATIC ${EVMONE_SOURCES} ${PRECOMPILE_SOURCES}
    ethash/lib/keccak/keccak.c
    evmone/test/state/state.cpp evmone/test/state/host.cpp
    evmone/test/state/hash_utils.cpp evmone/test/state/bloom_filter.cpp
    evmone/test/state/mpt.cpp evmone/test/state/mpt_hash.cpp
    evmone/test/state/precompiles.cpp
    ../EVM/Precompiles.cpp)
target_include_directories(chain_evm SYSTEM PUBLIC evmone/include evmc/include intx/include
    ethash/include evmone/lib evmone/test)
target_compile_definitions(chain_evm PRIVATE PROJECT_VERSION="0.12.0")
target_compile_features(chain_evm PUBLIC cxx_std_20)
target_include_directories(chain_evm SYSTEM PRIVATE "${CHAIN_BOOST_DIR}")
