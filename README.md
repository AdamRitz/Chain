# Chain

C++20 区块链原型，使用 Boost.Asio、libsodium、默克尔树和 RocksDB。当前实现账户余额、连续交易序号、可配置基础费、并发验签、按账户组织的交易池、原子区块与账户落库，以及单生产者到跟随节点的同步。

修改前阅读 [MODULE_GUIDE.md](MODULE_GUIDE.md)，了解模块职责、锁顺序、数据格式和后续问题。最新变更和性能见 [ACCOUNT_MODEL_REPORT_2026-09-09.md](ACCOUNT_MODEL_REPORT_2026-09-09.md)。

候选规则保留“交易数量更多优先，同数量比较哈希”。多生产者最终确认和分叉收敛属于后续研究任务。每秒交易数（Transactions Per Second，TPS）统计本地完成账户执行与数据库提交的交易。

## 构建与测试

本机 Windows 环境：

```powershell
$env:PATH = 'D:\Software\Code\Language\MinGW64\ucrt64\bin;' + $env:PATH
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release `
  '-DCMAKE_CXX_COMPILER=D:/Software/Code/Language/MinGW64/ucrt64/bin/g++.exe' `
  '-DCHAIN_BOOST_DIR=D:/Project/Includes/boost_1_91_0' `
  '-DPython3_EXECUTABLE=D:/Software/Code/Tool/Anaconda/python.exe'
cmake --build build -j 4
ctest --test-dir build --output-on-failure
```

四项测试覆盖核心交易与并发、账户状态约束、网络复制、钱包与账户接口。全部使用新数据库。Python 功能测试使用标准库，性能采样另需 `psutil`。

## 建立测试链

生成已签名交易，同时生成创世资金配置：

```powershell
New-Item -ItemType Directory -Path bench-results -Force
build/sender.exe --prepare bench-results/transactions.bin --count 500000
```

输出 `transactions.bin` 与 `transactions.bin.genesis.json`。配置只记录公钥和初始余额，每个测试账户默认分配 1,000,000,000 个最小单位。`--balance` 和 `--base-fee` 可设定分配与基础费，基础费默认零。

启动生产者：

```powershell
build/boost.exe --data data/accounts-leader --genesis bench-results/transactions.bin.genesis.json `
  --bind 127.0.0.1 --port 8089 --io-threads 4 --verify-threads 8 --block-ms 50 --sync 1
```

另一个终端发包：

```powershell
build/sender.exe --file bench-results/transactions.bin --count 500000 --port 8089 --batch 128 --connections 8
build/sender.exe --stats --port 8089
```

跟随节点使用相同创世配置：

```powershell
build/boost.exe --data data/accounts-follower --genesis bench-results/transactions.bin.genesis.json `
  --port 8090 --produce 0 --seed 127.0.0.1:8089
```

重启已有账户数据库时，程序读取库中保存的创世配置。显式提供 `--genesis` 时检查与数据库一致。全新数据库省略创世配置时，初始资金总量为零。

## 钱包、转账和查询

```powershell
New-Item -ItemType Directory -Path secrets -Force
build/sender.exe --create-wallet secrets/alice.wallet.json
build/sender.exe --create-wallet secrets/bob.wallet.json
```

复制输出的公钥，创建 `genesis.local.json`：

```json
{
  "base_fee": 1,
  "accounts": [
    {"public_key": "替换为 Alice 的公钥：64 个十六进制字符", "balance": 1000000}
  ]
}
```

用该配置和新的数据目录启动节点，再查询或转账：

```powershell
build/sender.exe --account <Alice公钥> --port 8089
build/sender.exe --wallet secrets/alice.wallet.json --receiver <Bob公钥> --amount 100 --nonce 1 --port 8089
```

`--nonce` 是账户上一笔已确认序号加一；`--amount` 使用正整数最小单位。钱包模式默认发送一笔，`--count` 可生成连续序号的多笔交易。交易池允许最多领先已确认序号 4096 的未来交易，批量发送需结合确认进度控制数量。钱包文件保存明文种子，存放于 `secrets/`；该目录及 `*.wallet.json` 已加入忽略规则。

基础费在确认时扣除并销毁，自转和互转遵循相同收费规则。资金分配和基础费由创世块绑定，新公钥账户余额从零开始。

## 常用节点参数

| 参数 | 默认 | 作用 |
|---|---:|---|
| `--data` | `data/accounts-v3` | 账户数据库目录 |
| `--genesis` | 已有库读取保存的配置；新库空分配 | 初始资金和基础费 |
| `--io-threads` | 4 | 网络输入输出（Input/Output，I/O）线程 |
| `--verify-threads` | 8 | 签名验证线程 |
| `--block-ms` | 50 | 凑交易等待目标，可执行交易满块时提前触发 |
| `--max-block-txs` | 6000 | 每块交易上限 |
| `--max-pending` | 32768 | 待验证任务上限，满时暂停连接读取 |
| `--max-pool` | 200000 | 交易池容量 |
| `--sync` | 1 | 同步写入预写日志（Write-Ahead Log，WAL） |
| `--produce` | 1 | 是否由本机生成区块 |
| `--seed` | 空 | 同步节点的地址与端口 |
| `--run-seconds` | 持续运行 | 到时退出 |
| `--metrics` | 空 | 保存退出统计 |

先加载 `config.yaml`，再由命令行覆盖。默认监听本机回环地址。

## 性能测试

```powershell
python Test/RunBench.py --bin build --work bench-results/account-run `
  --count 500000 --repeats 3 --suite node --threads 8 24
```

使用外部数据集时，同时提供匹配的 `--genesis`，或保留数据文件旁的 `.genesis.json`。脚本在计时结束后核对所有账户余额、序号和供应总量。`--suite replica --count 100000` 测试同机两节点都保存完成的速度。

新旧版本交替对比：

```powershell
python Test/CompareAccounts.py --before-bin <旧版本构建目录> --after-bin build `
  --dataset <同一份交易文件> --genesis <匹配的创世配置> `
  --work bench-results/compare --count 500000 --repeats 3 --threads 8 24
```

两版使用同一个新发送端和相同交易字节。`local_commit_tps` 从首批交易接收到最后一次本地提交计时；发包速率、包含客户端启动的速率、低负载延迟分别记录。

## 数据格式与升级

账户版使用 `SchemaVersion=3`。交易保持 176 字节，区块保持 112 字节头部加交易。账户以 `user/公钥` 为键，保存 16 字节余额与序号。

运行账户版时使用新的数据目录和创世配置。旧账本缺少账户执行历史，程序会保留原格式并提示创建新库。历史性能资料按原日期保留。

后续重点：交易签名绑定链标识、确认与分叉收敛、候选接收公平性、长期负载与多机测试。Git 提交和同步流程见 [GIT_SYNC.md](GIT_SYNC.md)。
