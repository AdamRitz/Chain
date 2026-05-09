//
// Created by 61485 on 2026/4/29.
//
#include <iostream>
#include <boost/asio.hpp>
#include <functional>
#include <iostream>
#include <system_error>
#include <fstream>
using  namespace  boost::asio;

int Example1() {
    io_context io;
    steady_timer t(io,chrono::seconds(5));
    io.run();
    t.wait();
    std::cout << "Hello, World!" << std::endl;
    return 0;
}
int Example2() {
    io_context io;
    steady_timer t(io,chrono::seconds(2));

    t.async_wait([](const boost::system::error_code& ec)
    {
        if (!ec)
        {
            std::cout << "timer expired" << std::endl;
        }
    });
    io.run();

    std::cout << "Hello, World2!" << std::endl;
    return 0;
}
// void Print(const boost::system::error_code&,int &count) {
//  count++;
//     if (count <=5) {
//         steady_timer t(io,chrono::seconds(2));
//     }
// }
// void Example3() {
//     io_context io;
//     int count = 1;
//     steady_timer t(io,chrono::seconds(2));
//     t.async_wait(std::bind(Print,boost::asio::placeholders::error,count));
//     io.run();
// }
void printer(std::error_code& /*e*/,steady_timer*) {

}


void ExampleReadFile() {
    // 三种文件流
    // ofstream output file stream 输出文件流
    // ifstream input file stream 输入文件流
    // fstream file stream 输入输出文件流
    std::fstream infile;
    infile.open("D:/Project/Chain/1.txt",std::ios::in);
    if (!infile.is_open()) {
        std::cerr << "Can't open file" << std::endl;
    }
    std::string str;

    std::getline(infile,str);

    std::cout<< std::string(str);
}

void ExampleWriteFile() {
    std::fstream file;
    file.open("D:/Project/Chain/1.txt",std::ios::out|std::ios::in | std::ios::app);
    if (!file.is_open()) {
        std::cerr << "Can't open file" << std::endl;
    }
    file << "test";

    file.flush();
    file.seekg(0,std::ios::beg);

    std::string str;
    std::getline(file,str);
    std::cout << std::string(str);

}
void ExampleNameSpace() {
    // myfunc.h
    // namespace mynet {
    //     void hello();
    // }
}
void ExampleErrorCode() {

    std::error_code ec;

    std::ifstream file("not_exist.txt");

    if (!file) {
        ec = std::make_error_code(std::errc::no_such_file_or_directory);
    }

    if (ec) {
        std::cerr << "error: " << ec.message() << std::endl;
    } else {
        std::cout << "open file success" << std::endl;
    }

}
struct Number {
    int value;
    Number operator+(const Number a) {
        Number result;
        result.value=value+a.value;
        return  result;
    }
    Number operator-(const Number a) {
        Number result;
        result.value=value-a.value;
        return  result;
    }
};

#include <chrono>
using namespace chrono;
int main() {
    time_point a = steady_clock::now();
    std::cout << a.time_since_epoch().count();
}