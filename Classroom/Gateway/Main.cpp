#include "App.h"
#include <csignal>
#ifdef _WIN32
#include <shellapi.h>
#else
#include <sys/file.h>
#endif
atomic<bool> gatewayRunning{true};

struct RootLock {
#ifdef _WIN32
    HANDLE handle = INVALID_HANDLE_VALUE;
    explicit RootLock(const fs::path &root) {
        handle = CreateFileW((root / L"gateway.lock").c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_ALWAYS,
                             FILE_ATTRIBUTE_NORMAL, nullptr);
        if (handle == INVALID_HANDLE_VALUE)
            throw runtime_error("此应用目录已在使用，请关闭已有窗口");
    }
    ~RootLock() {
        if (handle != INVALID_HANDLE_VALUE)
            CloseHandle(handle);
    }
#else
    int handle = -1;
    explicit RootLock(const fs::path &root) {
        handle = open((root / "gateway.lock").c_str(), O_CREAT | O_RDWR, 0600);
        if (handle < 0 || flock(handle, LOCK_EX | LOCK_NB))
            throw runtime_error("Classroom directory is already in use");
    }
    ~RootLock() {
        if (handle >= 0)
            close(handle);
    }
#endif
};

string Mime(const fs::path &path) {
    auto ext = path.extension().string();
    if (ext == ".html")
        return "text/html; charset=utf-8";
    if (ext == ".js")
        return "text/javascript; charset=utf-8";
    if (ext == ".css")
        return "text/css; charset=utf-8";
    if (ext == ".svg")
        return "image/svg+xml";
    if (ext == ".png")
        return "image/png";
    if (ext == ".ico")
        return "image/x-icon";
    return "application/octet-stream";
}
void Serve(tcp::socket socket, ClassroomApp &app, const fs::path &web, const string &token, const string &origin) {
    try {
        auto &io = static_cast<io_context &>(socket.get_executor().context());
        beast::flat_buffer buffer;
        http::request_parser<http::string_body> parser;
        parser.body_limit(2 * 1024 * 1024);
        parser.header_limit(16384);
        steady_timer timer(io);
        boost::system::error_code error;
        bool done = false;
        timer.expires_after(std::chrono::seconds(5));
        timer.async_wait([&](boost::system::error_code ec) {
            if (!ec) {
                boost::system::error_code ignored;
                socket.close(ignored);
            }
        });
        http::async_read(socket, buffer, parser, [&](boost::system::error_code ec, size_t) {
            error = ec;
            done = true;
            timer.cancel();
        });
        io.restart();
        while (!done)
            io.run_one();
        if (error)
            return;
        auto request = parser.release();
        http::response<http::string_body> response{http::status::ok, 11};
        response.keep_alive(false);
        response.set(http::field::server, "Chain Classroom");
        response.set(http::field::cache_control, "no-store");
        response.set("X-Content-Type-Options", "nosniff");
        response.set("Referrer-Policy", "no-referrer");
        response.set("Content-Security-Policy",
                     "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                     "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
        auto fail = [&](http::status status, const string &message) {
            response.result(status);
            response.set(http::field::content_type, "application/json; charset=utf-8");
            response.body() = Json({{"error", message}}).dump();
        };
        auto expectedHost = origin.substr(7);
        auto host = string(request[http::field::host]);
        auto source = string(request[http::field::origin]);
        if (host != expectedHost || (!source.empty() && source != origin))
            fail(http::status::forbidden, "Invalid request origin");
        else if (request.target() == "/api" && request.method() == http::verb::post) {
            auto authorization = string(request[http::field::authorization]);
            auto expected = "Bearer " + token;
            if (authorization.size() != expected.size() ||
                sodium_memcmp(authorization.data(), expected.data(), expected.size()))
                fail(http::status::unauthorized, "需要本机应用访问令牌");
            else {
                try {
                    auto payload = Json::parse(request.body());
                    if (payload.at("action") == "shutdown") {
                        gatewayRunning = false;
                        response.body() = "{\"ok\":true}";
                    } else {
                        lock_guard lock(app.guard);
                        response.body() = app.Api(payload).dump();
                    }
                    response.set(http::field::content_type, "application/json; charset=utf-8");
                } catch (const exception &e) {
                    fail(http::status::bad_request, e.what());
                }
            }
        } else if (request.method() == http::verb::get) {
            auto target = string(request.target());
            if (target == "/")
                target = "/index.html";
            if (target.find("..") != string::npos || target.find('%') != string::npos ||
                target.find('\\') != string::npos || target.find('?') != string::npos)
                fail(http::status::bad_request, "Invalid asset path");
            else {
                auto path = web / target.substr(1);
                if (!fs::is_regular_file(path) || fs::file_size(path) > 10 * 1024 * 1024)
                    fail(http::status::not_found, "Page not found");
                else {
                    ifstream input(path, ios::binary);
                    response.body() = {istreambuf_iterator<char>(input), istreambuf_iterator<char>()};
                    response.set(http::field::content_type, Mime(path));
                }
            }
        } else
            fail(http::status::method_not_allowed, "Use POST /api");
        response.prepare_payload();
        done = false;
        timer.expires_after(std::chrono::seconds(5));
        timer.async_wait([&](boost::system::error_code ec) {
            if (!ec) {
                boost::system::error_code ignored;
                socket.close(ignored);
            }
        });
        http::async_write(socket, response, [&](boost::system::error_code, size_t) {
            done = true;
            timer.cancel();
        });
        io.restart();
        while (!done)
            io.run_one();
    } catch (const exception &e) {
        cerr << "Classroom request: " << e.what() << '\n';
    }
}

int main(int argc, char **argv) {
    try {
        Utf8Arguments unicode(argc, argv);
        if (sodium_init() < 0)
            throw runtime_error("Cannot initialize signing library");
        auto executable = fs::absolute(Utf8Path(argv[0])).parent_path();
        map<string, string> args;
        bool openBrowser = false;
        for (int i = 1; i < argc; i++) {
            string name = argv[i];
            if (name == "--open") {
                openBrowser = true;
                continue;
            }
            if (i + 1 >= argc)
                throw invalid_argument("Missing argument");
            args[name] = argv[++i];
        }
#ifdef _WIN32
        auto base = getenv("LOCALAPPDATA");
        fs::path defaultRoot = base ? Utf8Path(base) / "ChainClassroom" : executable / "classroom-data";
#else
        auto base = getenv("HOME");
        fs::path defaultRoot = base ? Utf8Path(base) / ".local/share/chain-classroom" : executable / "classroom-data";
#endif
        auto path = [&](const string &key, const fs::path &fallback) {
            return fs::absolute(args.count(key) ? Utf8Path(args[key]) : fallback);
        };
        auto root = path("--data", defaultRoot), web = path("--web", executable / "web");
        fs::create_directories(root);
        RootLock instance(root);
        auto binary = path("--node", executable /
#ifdef _WIN32
                                         "boost.exe"
#else
                                         "boost"
#endif
        );
        if (!fs::is_regular_file(binary) || !fs::is_regular_file(web / "index.html"))
            throw runtime_error("缺少节点或界面资源，请使用完整安装包");
        ClassroomApp app(root, binary, path("--contract", executable / "ClassroomChat.compiled.json"));
#ifdef _WIN32
        struct ParentMonitor {
            HANDLE handle = nullptr;
            ~ParentMonitor() { if (handle) CloseHandle(handle); }
            bool Alive() const { return !handle || WaitForSingleObject(handle, 0) == WAIT_TIMEOUT; }
        } parent;
        if (args.count("--parent-pid")) {
            parent.handle = OpenProcess(SYNCHRONIZE, false, stoul(args["--parent-pid"]));
            if (!parent.handle) throw runtime_error("Desktop parent process is not running");
        }
#endif
        io_context acceptIo;
        unsigned requested = args.count("--port") ? stoul(args["--port"]) : 0;
        if (requested > 65535)
            throw invalid_argument("Invalid gateway port");
        tcp::acceptor listener(acceptIo, {ip::address_v4::loopback(), uint16_t(requested)});
        listener.non_blocking(true);
        auto port = listener.local_endpoint().port();
        auto origin = "http://127.0.0.1:" + to_string(port), token = RandomHex(32), url = origin + "/#" + token;
        cout << Json({{"url", url},
                      {"port", port},
                      {"pid",
#ifdef _WIN32
                       GetCurrentProcessId()
#else
                       getpid()
#endif
                      }})
                    .dump()
             << endl;
#ifdef _WIN32
        if (openBrowser)
            ShellExecuteW(nullptr, L"open", Utf8Path(url).wstring().c_str(), nullptr, nullptr, SW_SHOWNORMAL);
#else
        (void)openBrowser;
#endif
        signal(SIGINT, [](int) { gatewayRunning = false; });
        signal(SIGTERM, [](int) { gatewayRunning = false; });
        bool resumeSavedProfile = app.profile.is_object();
        thread ticker([&, resumeSavedProfile] {
            bool first = true;
            while (gatewayRunning) {
                {
                    lock_guard lock(app.guard);
                    if (first && resumeSavedProfile) {
                        try {
                            app.Start();
                        } catch (const exception &e) {
                            app.lastError = e.what();
                        }
                    } else
                        app.Tick();
                    first = false;
                }
                for (int i = 0; i < 10 && gatewayRunning; i++)
                    this_thread::sleep_for(std::chrono::milliseconds(100));
            }
        });
        thread_pool pool(4);
        atomic<size_t> activeRequests{0};
        while (gatewayRunning) {
#ifdef _WIN32
            if (!parent.Alive()) break;
#endif
            if (args.count("--stop-file") && fs::exists(Utf8Path(args["--stop-file"])))
                break;
            // Each connection owns its I/O context, so timeout callbacks run on its worker only.
            auto io = make_shared<io_context>();
            auto socket = make_shared<tcp::socket>(*io);
            boost::system::error_code error;
            listener.accept(*socket, error);
            if (error == error::would_block || error == error::try_again) {
                this_thread::sleep_for(std::chrono::milliseconds(20));
                continue;
            }
            if (error) {
                cerr << error.message() << endl;
                gatewayRunning = false;
                break;
            }
            if (activeRequests.fetch_add(1) >= 8) {
                activeRequests--;
                boost::system::error_code ignored;
                socket->close(ignored);
                continue;
            }
            post(pool, [&, io, socket] {
                Serve(std::move(*socket), app, web, token, origin);
                activeRequests--;
            });
        }
        gatewayRunning = false;
        ticker.join();
        pool.join();
        lock_guard lock(app.guard);
        app.Stop();
        return 0;
    } catch (const exception &e) {
        cerr << "Classroom failed: " << e.what() << endl;
        return 1;
    }
}
