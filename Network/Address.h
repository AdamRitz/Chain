#pragma once
#include <boost/asio.hpp>
#include <charconv>
#include <string>
#include <utility>
#include <stdexcept>
using namespace boost::asio;
using tcp = ip::tcp;

ip::address CanonicalAddress(ip::address address) {
    if (address.is_v6() && address.to_v6().is_v4_mapped()) {
        auto bytes = address.to_v6().to_bytes();
        return ip::address_v4({bytes[12], bytes[13], bytes[14], bytes[15]});
    }
    return address;
}
std::pair<std::string, uint16_t> ParseEndpoint(const std::string &value) {
    std::string host, service;
    if (value.starts_with('[')) {
        auto end = value.find(']');
        if (end == std::string::npos || end + 1 >= value.size() || value[end + 1] != ':')
            throw std::invalid_argument("Use [IPv6]:port");
        host = value.substr(1, end - 1);
        service = value.substr(end + 2);
        if (!ip::make_address(host).is_v6())
            throw std::invalid_argument("Bracketed host must be IPv6");
    } else {
        auto colon = value.find(':');
        if (colon == std::string::npos || value.find(':', colon + 1) != std::string::npos)
            throw std::invalid_argument("Use host:port or [IPv6]:port");
        host = value.substr(0, colon);
        service = value.substr(colon + 1);
    }
    unsigned port = 0;
    auto result = std::from_chars(service.data(), service.data() + service.size(), port);
    if (host.empty() || host.size() > 253 || host.find_first_of(" /\\\r\n\t") != std::string::npos ||
        result.ec != std::errc() || result.ptr != service.data() + service.size() || !port || port > 65535)
        throw std::invalid_argument("Invalid peer endpoint");
    return {host, uint16_t(port)};
}
std::string FormatEndpoint(const std::string &host, uint16_t port) {
    return (host.find(':') == std::string::npos ? host : "[" + host + "]") + ":" + std::to_string(port);
}
tcp::socket OpenNodeSocket(io_context &io, const std::string &host, uint16_t port) {
    tcp::resolver resolver(io);
    tcp::socket socket(io);
    connect(socket, resolver.resolve(host, std::to_string(port)));
    socket.set_option(tcp::no_delay(true));
    return socket;
}
