#pragma once
#include <filesystem>
#include <string_view>
inline std::filesystem::path Utf8Path(std::string_view text) {
    return std::filesystem::path(std::u8string(reinterpret_cast<const char8_t *>(text.data()), text.size()));
}
