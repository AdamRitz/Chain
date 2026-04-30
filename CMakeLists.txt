cmake_minimum_required(VERSION 4.0)
project(Chain)

set(CMAKE_CXX_STANDARD 20)
set(OPENSSL_ROOT_DIR "D:/Software/Code/Language/MinGW64/ucrt64")

find_package(OpenSSL REQUIRED)
add_executable(boost main.cpp)
# 添加头文件搜索目录
target_include_directories(boost PRIVATE D:/Project/Includes/boost_1_91_0)
# 链接头文件
find_package(OpenSSL REQUIRED)
target_link_libraries(boost PRIVATE ws2_32 OpenSSL::Crypto mswsock)


add_executable(test Test.cpp)
target_include_directories(test PRIVATE D:/Project/Includes/boost_1_91_0)
target_link_libraries(test PRIVATE ws2_32 mswsock)