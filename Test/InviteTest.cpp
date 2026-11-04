#include "../Classroom/Gateway/Invite.h"
#include <iostream>
void Require(bool value, const char *message) { if (!value) throw runtime_error(message); }
int main() {
    try {
        Require(sodium_init() >= 0, "sodium");
        for (const auto &text : {"::", "::1", "fe80::1", "fe90::1", "fea0::1", "febf::1", "ff02::1", "2001:db8::1", "127.0.0.1", "169.254.1.1", "192.0.2.1"})
            Require(InviteScope(ip::make_address(text)) == "excluded", text);
        Require(InviteScope(ip::make_address("fd12::1")) == "private", "ULA");
        Require(InviteScope(ip::make_address("192.168.2.3")) == "lan", "LAN");
        Require(InviteScope(ip::make_address("100.64.1.1")) == "lan", "CGN");
        Require(InviteScope(ip::make_address("240e::123")) == "internet_candidate", "GUA");
        Json raw = Json::array();
        auto add = [&](string name, string address, bool route=true) {
            raw.push_back({{"interface",name},{"address",address},{"preferred",true},{"up",true},{"default_route",route}});
        };
        add("VMware Network Adapter VMnet8", "192.168.88.1"); add("Ethernet", "192.168.1.2");
        add("Ethernet", "FE90::123"); add("Ethernet", "240e::123"); add("VPN", "fd12::1", false);
        add("Ethernet", "240e::456"); raw.back()["skip_as_source"] = true;
        auto selected = SelectInviteAddresses(raw, 8787);
        Require(selected["seeds"].size() == 3 && selected["seeds"][0] == "[240e::123]:8787", "dual-stack address selection");
        Require(selected["external_verified"] == false, "local selection is not external proof");
        auto onlyLan = SelectInviteAddresses(Json::array({raw[1]}),8787);
        Require(onlyLan["scope"] == "local", "LAN must not claim Internet");
        Require(onlyLan["ipv6"]["global_addresses"]==0 && onlyLan["ipv6"]["default_route"]==false,"IPv4 does not imply IPv6 Internet");
        for (unsigned i = 1; i <= 10; i++) add("Ethernet", "240e::" + to_string(i));
        auto many = SelectInviteAddresses(raw,8787);
        Require(many["seeds"].size() == 8 && find(many["seeds"].begin(), many["seeds"].end(), "192.168.1.2:8787") != many["seeds"].end(), "temporary IPv6 addresses cannot crowd out IPv4 fallback");
        Json invite = {{"title","论文讨论"},{"version",2},{"seeds",Json::array({"[240e::123]:8787"})}};
        auto encoded = EncodeInvite(invite);
        string wrapped;
        for (size_t i=0;i<encoded.size();++i) { wrapped+=encoded[i]; if (i>6 && i%80==0) wrapped+="\r\n"; }
        Require(DecodeInvite(wrapped)==invite,"copy/paste line wrapping");
        Require(DecodeInvite("邀请你加入\n\n" + encoded + "\n") == invite, "whole message roundtrip");
        Require(DecodeInvite(invite.dump()) == invite && DecodeInvite(invite) == invite, "legacy JSON");
        auto file = InviteEnvelope(invite);
        Require(DecodeInvite(file) == invite, "versioned invitation file");
        file["invite"]["title"] = "changed";
        bool tampered=false; try { DecodeInvite(file); } catch (...) { tampered=true; }
        Require(tampered,"checksum detects copied content changes");
        bool rejected = false;
        try { DecodeInvite(encoded.substr(0,encoded.size()-4)); } catch (...) { rejected = true; }
        Require(rejected, "truncated invitation");
        rejected=false;
        try { DecodeInvite(string(MaxInviteBytes*2+1,'x')); } catch (...) { rejected=true; }
        Require(rejected,"input size bound");
        Json unreachable={{"endpoints",Json::array({{{"family","IPv6"},{"scope","internet_candidate"},{"code","no_route"}}})}};
        auto reason=InviteFailureMessage(unreachable,{{"route_checked",true},{"default_route",false},{"global_addresses",0}});
        Require(reason.find("默认路由")!=string::npos,"missing IPv6 route diagnosis");
        Require(InviteFailureMessage({{"endpoints",Json::array({{{"code","refused"}}})}},Json::object()).find("拒绝")!=string::npos,"refused port diagnosis");
        auto real = CollectInviteAddresses(8787);
        Require(real["seeds"].size()<=8 && real["external_verified"]==false,"native adapter collector");
        std::cout << "Invite scope, native adapters, envelope, legacy and bounds passed\n";
        return 0;
    } catch (const exception &e) { std::cerr << e.what() << '\n'; return 1; }
}
