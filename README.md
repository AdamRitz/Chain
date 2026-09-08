# Chain

C++20 区块链原型。主要组件为 Boost.Asio 协程/TCP、libsodium Ed25519 与 BLAKE2b、Merkle 树和 RocksDB。沿用现有函数和目录结构：`ProcessTxPackage → txpool → GenerateBlock → VerifyBlock → CommitBlock`。

目前完成交易签名验证、批量接收、并发验签、有限交易池、原子区块落库、单生产者广播和跟随节点历史同步。**尚未执行余额和 nonce 规则，也没有 BFT 共识、分叉回滚或最终性证明。** TPS 指验证通过并完成本地数据库提交的交易数。

## 构建与测试（本机 Windows）

```powershell
$env:PATH = 'D:\Software\Code\Language\MinGW64\ucrt64\bin;' + $env:PATH
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release `
  '-DCMAKE_CXX_COMPILER=D:/Software/Code/Language/MinGW64/ucrt64/bin/g++.exe' `
  '-DCHAIN_BOOST_DIR=D:/Project/Includes/boost_1_91_0' `
  '-DPython3_EXECUTABLE=D:/Software/Code/Tool/Anaconda/python.exe'
cmake --build build -j 6
ctest --test-dir build --output-on-failure
```

CMake 最低 3.22。依赖由本机 MinGW prefix 中的 RocksDB、libsodium、spdlog、yaml-cpp 和 JSON 头文件提供。CTest 会将编译器目录置于测试 PATH 前面，避免误加载其他工具的同名 DLL。网络测试需要 Python 3；未找到解释器时只注册 C++ 核心测试。手工执行网络测试：

```powershell
& 'D:/Software/Code/Tool/Anaconda/python.exe' Test/NetworkTest.py --bin build --work network-test-data
```

测试为每次运行建立新目录，不使用现有链数据库。`Test.cpp`、`Test/TestHash.cpp` 是早期独立练习，不是节点回归测试。`crypto` 是实验 VRF 的独立小程序；VRF 尚未接入出块选主。

## 运行

单生产者（显式选择新数据库路径）：

```powershell
build/boost.exe --data data/leader --bind 127.0.0.1 --port 8089 `
  --io-threads 4 --verify-threads 8 --block-ms 50 --sync 1
```

跟随节点：

```powershell
build/boost.exe --data data/follower --port 8090 --produce 0 --seed 127.0.0.1:8089
```

默认仅监听回环地址。局域网实验可通过 `--bind` 设置网卡地址；协议尚无节点身份鉴别。只运行一个生产者；多个生产者的竞争规则不构成一致性协议。

常用参数：

| 参数 | 默认 | 作用 |
|---|---:|---|
| `--data` | `test` | 数据库目录；生产运行应显式指定 |
| `--io-threads` | 4 | 网络线程数 |
| `--verify-threads` | 8 | CPU 验签线程数 |
| `--block-ms` | 50 | 小批量最大等待目标；满块可提前触发 |
| `--max-block-txs` | 6000 | 单块交易上限 |
| `--max-pending` | 32768 | 验证队列交易数上限；满时暂停读取 |
| `--max-pool` | 200000 | 交易池上限；超出会记录拒绝计数 |
| `--sync` | 1 | 同步 WAL 写盘；0 只用于对照测试 |
| `--produce` | 1 | 生产者或跟随节点 |
| `--run-seconds` | 不限 | 有限时间运行并退出 |
| `--metrics` | 不输出文件 | 退出时写入 JSON 和 RocksDB 统计 |

`config.yaml` 中的 node 配置先加载，命令行再覆盖。信号或定时停止时先停网络、完成已投递验证、再提交生产者剩余池交易。跟随节点不会自行确认池内交易。没有完整收到或尚未入验证队列的网络数据不承诺在退出时处理。

## 压测与统计

先生成已签名数据，再发包；生成签名不计入节点 TPS：

```powershell
New-Item -ItemType Directory -Path bench-results -Force
build/sender.exe --prepare bench-results/transactions.bin --count 500000
build/sender.exe --file bench-results/transactions.bin --count 500000 --port 8089 --batch 128 --connections 8
build/sender.exe --stats --port 8089
```

`sender` 输出的是发包速率。节点 `local_commit_tps` 使用首批接收至最后提交的时间；须确认 `committed` 达到发送总量、`pool=0`、`pending=0`、`invalid=0`、`rejected=0` 后读取最终值。

完整可复现测试需要 Python 的 `psutil`，输出目录必须不存在：

```powershell
& 'D:/Software/Code/Tool/Anaconda/python.exe' Test/RunBench.py --bin build --work bench-results/run-2026-09-08 --count 500000 --repeats 3
```

包含纯验签扩展性、hash 对照、逐笔与批量数据库写入、同步/异步 WAL、1/4/8/16/24 验签线程、单笔/128 笔报文及低负载延迟。写盘实验请将 `--work` 放在需要测量的磁盘。每组都启动隔离节点并验证提交总数。测量结束后脚本终止自己启动的进程；`NetworkTest.py` 另外验证定时正常退出与重启恢复。

使用 `--suite replica --count 100000` 可单独测同机两个节点都提交到相同链头的吞吐。它包含跟随者重验签与同步等待，仍不是 BFT 最终性指标。

`results.json` 保留原始数值；CPU 是节点进程 CPU 时间除以墙钟时间，`average_cpu_cores=1` 表示平均占用一个逻辑处理器。各 worker 计时会重叠，不能相加当成总延迟。低负载延迟包含 TCP 发包和轮询确认开销，不是共识最终性延迟。

## 兼容性与当前任务

交易仍是 176 字节，区块仍是 112 字节头部加交易；整数明确使用小端。节点握手新增两字节监听端口，需要一起升级连接的节点。消息长度统一为正文长度，旧的错误帧实现不兼容。

新数据库使用 `tx/`、`block/`、`height/` 前缀与 `SchemaVersion=2`；旧数据库保留无前缀读取回退。启动时检查现有链头，不再覆盖创世块；如果旧链头本身非法，会明确报错，不自动修复历史数据。本次测试未修改旧数据库。

最新修复、性能数据和账户/UTXO 选择见 `OPTIMIZATION_REPORT_2026-09-08.md`；后续任务见 `NEXT_STEPS_2026-09-08.md`。Git 自动推送和日期命名规则见 `GIT_SYNC.md`。
