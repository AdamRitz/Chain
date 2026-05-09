cmake_minimum_required(VERSION 4.0)
project(Chain)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_PREFIX_PATH "D:/Software/Code/Language/MinGW64/ucrt64")
add_executable(boost main.cpp)
# 添加头文件搜索目录
target_include_directories(boost PRIVATE D:/Project/Includes/boost_1_91_0)
# 链接头文件
target_link_libraries(boost PRIVATE ws2_32  mswsock RocksDB::rocksdb sodium spdlog::spdlog)

find_package(RocksDB CONFIG REQUIRED)
find_package(spdlog REQUIRED)
add_executable(test Test.cpp)
target_include_directories(test PRIVATE D:/Project/Includes/boost_1_91_0)
target_link_libraries(test PRIVATE ws2_32 mswsock )