#pragma once
#include "Support.h"

// Network tooling runs outside the app lock. A UAC prompt must not suspend block
// indexing, status polling, shutdown requests, or wallet operations.
class NetworkSetup {
    unique_ptr<Child> helper;
    fs::path output;
    future<Json> probe;
    Json state = {{"busy", false}, {"checked", false}};
  public:
    Json Status() {
#ifdef _WIN32
        if (helper && WaitForSingleObject(helper->process, 0) == WAIT_OBJECT_0) {
            helper.reset();
            try {
                if (fs::file_size(output) > 256 * 1024) throw runtime_error("Network result too large");
                auto value = ReadJson(output);
                state["firewall"] = value;
                state["checked"] = true;
            } catch (const exception &e) { state["firewall"] = {{"error", e.what()}}; }
            state["busy"] = false;
            state["checked_ms"] = NowMs();
        }
#endif
        if (probe.valid() && probe.wait_for(std::chrono::seconds(0)) == future_status::ready) {
            state["probe"] = probe.get();
            state["probing"] = false;
        }
        return state;
    }
    Json Start(const string &action, const Json &request, const fs::path &binary, const fs::path &root) {
        Status();
        if (helper) throw runtime_error("网络配置正在进行，请先完成 Windows 权限提示");
#ifdef _WIN32
        auto path = binary.parent_path() / "norn-network.exe";
        if (!fs::is_regular_file(path)) throw runtime_error("缺少网络配置组件，请安装完整新版应用");
        vector<string> args{action};
        if (action == "configure") {
            auto profile = request.value("profile", string("Private"));
            auto remote = request.value("remote", string("LocalSubnet"));
            if ((profile != "Private" && profile != "Domain" && profile != "Public") || remote.empty() ||
                remote.size() > 1024 || remote.find_first_not_of("0123456789abcdefABCDEF:.,/LocalSubnet") != string::npos)
                throw invalid_argument("请选择网络类型，并填写 LocalSubnet 或 IPv4/IPv6 地址与网段");
            args.push_back(profile); args.push_back(remote);
        }
        auto directory = root / "network-tools";
        fs::create_directories(directory);
        output = directory / "node.log";
        // This exact transient log is written only by the unelevated helper.
        if (fs::exists(output)) fs::remove(output);
        helper = make_unique<Child>(path, args, directory, directory / "stop.request");
        state["busy"] = true;
        state["action"] = action;
        state.erase("firewall");
#else
        state["firewall"] = {{"supported", false}, {"error", "自动防火墙配置仅支持 Windows；请联系系统管理员"}};
#endif
        return Status();
    }
    Json Probe(const string &endpoint, const string &networkId) {
        Status();
        if (probe.valid()) throw runtime_error("连接检测正在进行");
        auto [host, port] = ParseEndpoint(endpoint);
        probe = async(launch::async, [host, port, endpoint, networkId] {
            Json result = {{"endpoint", endpoint}, {"network_id", networkId}, {"checked_ms", NowMs()}, {"ok", false}};
            try {
                auto start = Clock::now();
                auto data = Query(host, port, 13);
                result["height"] = data.at("height");
                result["head"] = data.at("head");
                result["elapsed_ms"] = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start).count();
                result["ok"] = true;
            } catch (const exception &e) { result["error"] = e.what(); }
            return result;
        });
        state["probing"] = true;
        state.erase("probe");
        return Status();
    }
};
