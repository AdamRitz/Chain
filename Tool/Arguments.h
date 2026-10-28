#pragma once
#include <filesystem>
#include <string>
#include <vector>
#include <stdexcept>
#include "Path.h"
#ifdef _WIN32
#include <windows.h>
#include <shellapi.h>
#endif

std::string PathUtf8(const std::filesystem::path &path) {
    auto text = path.u8string();
    return {reinterpret_cast<const char *>(text.data()), text.size()};
}
// MinGW's narrow main() uses the system code page. Recover the original Unicode command line.
struct Utf8Arguments {
    std::vector<std::string> values;
    std::vector<char *> pointers;
    Utf8Arguments(int &argc, char **&argv) {
#ifdef _WIN32
        int count = 0;
        auto wide = CommandLineToArgvW(GetCommandLineW(), &count);
        if (!wide)
            throw std::runtime_error("Cannot read Unicode command line");
        for (int i = 0; i < count; i++) {
            auto size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wide[i], -1, nullptr, 0, nullptr, nullptr);
            if (!size) {
                LocalFree(wide);
                throw std::runtime_error("Invalid Unicode argument");
            }
            std::string text(size, '\0');
            WideCharToMultiByte(CP_UTF8, 0, wide[i], -1, text.data(), size, nullptr, nullptr);
            text.pop_back();
            values.push_back(std::move(text));
        }
        LocalFree(wide);
        for (auto &value : values)
            pointers.push_back(value.data());
        pointers.push_back(nullptr);
        argc = count;
        argv = pointers.data();
        SetConsoleOutputCP(CP_UTF8);
#else
        (void)argc;
        (void)argv;
#endif
    }
};
