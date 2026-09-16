# 合约与分叉使用说明

更新日期：2026-09-16。

## 当前功能

以太坊虚拟机（Ethereum Virtual Machine，EVM）使用 evmone 执行引擎，固定 Shanghai 指令与资源计费规则。支持 Solidity 合约部署、调用、持久化存储、返回值、事件、嵌套调用、创建合约和执行回退。发送端沿用本项目 Ed25519 钱包。

多节点通过链头交换及按哈希补齐分支恢复一致。分支比较顺序为累计有效交易数、高度、链头哈希；前两项较大优先，最后一项较小优先。同一父块上的候选继续按交易数、哈希排序。单次恢复窗口为 256 个区块，待处理分支最多 256 个区块、64 MiB。提交状态可在该窗口内重组。

## 创建钱包与测试链

以下命令在项目目录执行。选择新的 `data/evm-demo` 数据目录，配置中的公钥来自新钱包。

```powershell
New-Item -ItemType Directory -Force secrets | Out-Null
build/sender.exe --create-wallet secrets/alice.wallet.json
```

创建 `genesis.local.json`：

```json
{
  "base_fee": 1,
  "accounts": [
    {"public_key": "替换为钱包公钥", "balance": 1000000000}
  ]
}
```

```powershell
build/boost.exe --data data/evm-demo --genesis genesis.local.json --port 8089 --sync 1
```

## 部署与调用 Solidity 合约

`Contracts/Counter.sol` 包含计数器与嵌套调用示例，`Counter.compiled.json` 保存编译结果。编译设置为 Solidity 0.8.28、Shanghai、优化器 200 次。

重新编译示例时，运行 `node Contracts/Compile.cjs <已安装的 solc 0.8.28 模块路径>`。节点运行直接读取字节码，编译器供开发合约时使用。

在另一个终端执行：

```powershell
$contract = Get-Content Contracts/Counter.compiled.json -Raw | ConvertFrom-Json
$code = $contract.contracts.Counter.evm.bytecode.object
$sent = build/evm_sender.exe --wallet secrets/alice.wallet.json --genesis genesis.local.json `
  --deploy $code --gas 1000000 --nonce 1 --port 8089 | ConvertFrom-Json
build/evm_sender.exe --receipt $sent.transaction_hash --port 8089
```

等待回执出现，`status=0` 表示成功，`contract` 为部署地址。将其填入 `$address`，调用 `Increment()`：

```powershell
$address = '替换为部署地址'
$input = $contract.contracts.Counter.evm.methodIdentifiers.'Increment()'
build/evm_sender.exe --wallet secrets/alice.wallet.json --genesis genesis.local.json `
  --call $address --input $input --gas 100000 --nonce 2 --port 8089
build/evm_sender.exe --contract $address --port 8089
build/evm_sender.exe --contract $address --slot ('0' * 64) --port 8089
```

调用参数使用应用二进制接口（Application Binary Interface，ABI）编码后的十六进制字节。钱包的账户序号由普通转账与合约交易共同使用，每次已提交交易递增一。`--amount` 是随调用转入合约的资金，默认零。

回执包含执行状态、实际 Gas、返回值、日志及区块高度。Gas 是执行资源计量单位，每单位价格固定为 1 个链上最小金额单位；实际费用为创世基础费加已用 Gas。交易入池时检查能够支付 Gas 上限与转入金额。执行回退及 Gas 耗尽会消耗账户序号和实际费用；合约状态按执行结果保留或恢复。

EVM 地址取公钥 Keccak-256 摘要的后 20 字节。以下命令显示钱包对应地址和下一次 EVM 创建地址，EVM 创建序号默认零：

```powershell
build/evm_sender.exe --wallet secrets/alice.wallet.json --address --evm-nonce 0
```

原生账户与合约账户共同计入资金总量。执行时，将调用钱包的原生可用余额映射到其 EVM 地址；调用结束后，该地址的剩余资金回到原生账户。其他 EVM 地址收到的资金保存在合约账本，相应钱包下次调用时结算。EVM 内部创建序号单独保存。

钱包在原生账户中预留执行费用和调用转入金额。

## 确定性执行环境

| 项目 | 定义 |
|---|---|
| 链编号 | 20260916 |
| EVM 协议 | Shanghai |
| 时间戳 | 区块高度对应的逻辑时间 |
| 区块编号 | 当前执行高度 |
| 历史块哈希 | 当前分支最近 256 个区块 |
| 单笔 Gas 上限 | 10,000,000 |
| 单块 Gas 上限 | 30,000,000 |
| 单笔输入上限 | 49,152 字节 |
| 区块编码上限 | 2 MiB |
| 签名绑定 | 原生交易字段、完整 EVM 输入、Gas 上限及创世摘要 |

接口采用项目现有二进制网络协议与 `evm_sender`。Ethereum JSON-RPC、以太坊签名交易格式及钱包浏览器插件接入可在此基础上扩展。

预编译支持地址 `0x01` 至 `0x07`、`0x09`。地址 `0x08` 的 BN254 配对预编译返回执行失败；依赖该操作的配对验证合约需要补齐实现。模幂操作的每个整数长度上限为 65,536 字节。依赖版本与源代码调整见 [ThirdParty/VERSIONS.md](ThirdParty/VERSIONS.md)。

## 分叉恢复流程

1. 节点交换创世摘要、链头、高度和累计交易数，创世状态相同的节点继续同步。
2. 发现不同链头时按哈希请求区块，逐步补齐共同祖先之后的分支。
3. 验证签名和结构，对更优分支回滚至共同祖先并重放账户及合约交易。
4. 原生账户、合约代码和存储、费用、回执、交易索引、当前链头使用同一批次提交。
5. 重建交易池，将退出主链且仍满足账户规则的交易重新入池，再广播当前链头。

执行失败或写盘失败时，原分支及其内存状态继续保留。重启读取已原子提交的分支。未来高度的候选按哈希保留，父块不同的候选分别处理。

## 测试及性能图

新增 `ForkNetwork`、`EvmNetwork`、`ForkStateCorrectness`，与四项原测试一起运行。覆盖分区重连、同数哈希决胜、多块切换、无效候选、原生和 EVM 状态回滚、回执与索引清理、写盘失败、部署、嵌套调用、事件、Gas 及重启恢复。

```powershell
ctest --test-dir build --output-on-failure
python Test/BenchCores.py --bin build --baseline <修改前构建目录> `
  --work bench-results/cores-20260916 --native-count 300000 --evm-count 100000 --repeats 3
```

测试目录每次使用新路径。核心数脚本在 Windows 上读取物理核心拓扑，设置节点处理器亲和性，并保留一个物理核心给发送端。原生转账核对所有账户；合约调用核对最终计数值及运行代码。图表纵轴统计从发送端启动到全部交易执行、同步写盘完成的每秒交易数（Transactions Per Second，TPS）。

再次运行整套网络测试时，可用 `cmake -S . -B build -DCHAIN_TEST_DATA_ROOT=<新的绝对路径>` 指定新的测试数据根目录。图表命令：`python Test/PlotCores.py --results <测试目录>/results.json --out <图表目录>`。

## 数据库升级

当前数据库版本为 4，默认路径为 `data/accounts-v4`。新增 EVM 状态、回执、累计交易数和区块回滚记录。版本 2、3 的已有数据库保留，使用新的数据目录和创世配置运行本版本。
