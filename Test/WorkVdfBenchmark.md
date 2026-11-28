# Work VDF 参数标定

`WorkVdfBenchmark.cpp` 单独运行当前 `Crypto/WorkElection.h` 的 Work 计算和验证。它使用固定的公开测试密钥、链标识和历史摘要，不启动节点，不读取节点数据库。每一轮使用不同的历史摘要，并将上一轮输出作为下一轮 Work 的 `previousOutput`。

## 编译

依赖 GCC 13、GMP 开发包和 libsodium 开发包。在仓库根目录执行：

```bash
mkdir -p build-work-vdf-calibration
/opt/rh/gcc-toolset-13/root/usr/bin/gcc -O2 \
  -c ThirdParty/chiavdf/src/refcode/lzcnt.c \
  -o build-work-vdf-calibration/lzcnt.o
/opt/rh/gcc-toolset-13/root/usr/bin/g++ \
  -std=c++20 -O2 -DNDEBUG -DVDF_MODE=0 -DFAST_MACHINE=1 \
  -IThirdParty/chiavdf/src \
  Test/WorkVdfBenchmark.cpp build-work-vdf-calibration/lzcnt.o \
  -lgmpxx -lgmp -lsodium -pthread \
  -o build-work-vdf-calibration/work_vdf_benchmark
```

其他系统将编译器路径替换为实际 GCC 13 路径。该文件直接包含 `Crypto/VDF.cpp`，使上游头文件中的实现只进入一个 C++ 编译单元；不要再链接 `chain_vdf` 或单独编译的 `Crypto/VDF.cpp`。

## 参数

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `--iterations N` | 65536 | 每份 Work 的迭代次数，范围 4096～100000000 |
| `--rounds K` | 1 | 连续生成或读取 K 份 Work，范围 1～100000 |
| `--verify-repeats N` | 5 | 每份 Work、完整 Ticket 和缓存命中各验证 N 次，范围 1～10000 |
| `--skip-eval` | 关闭 | 跳过单独计算输出的计时，仍生成和验证完整证明 |
| `--corpus-dir PATH` | 未设置 | 保存公开 Work 记录；已有同名记录时拒绝覆盖 |
| `--verify-only` | 关闭 | 从 `--corpus-dir` 读取记录，只验证，不生成 Work |
| `--compare-stored` | 关闭 | 额外运行保存中间值的 `ProveSlow`，对比生产路径的完整证明 |

`--verify-only` 需要 `--corpus-dir`，不能与 `--compare-stored` 同用。读取样本时，`--iterations` 和 `--rounds` 应与生成样本时一致。

例如，绑定一个 CPU 核心生成一份 Work：

```bash
taskset -c 0 ./build-work-vdf-calibration/work_vdf_benchmark \
  --iterations 65536 --rounds 1 --verify-repeats 5 \
  > work-vdf-65536.jsonl
```

用 `taskset -pc $$` 查看当前 shell 允许使用的 CPU，选择其中一个编号。`taskset` 只设置进程的 CPU 亲和性；为比较不同参数，应保持编译选项、CPU 编号和服务器负载一致。错误写入标准错误，失败返回非零退出码。

## 保存和复用 32 份公开记录

```bash
taskset -c 0 ./build-work-vdf-calibration/work_vdf_benchmark \
  --iterations 65536 --rounds 32 --verify-repeats 5 --skip-eval \
  --corpus-dir ./work-vdf-public-65536 \
  > work-vdf-32-generate.jsonl

taskset -c 0 ./build-work-vdf-calibration/work_vdf_benchmark \
  --iterations 65536 --rounds 32 --verify-repeats 5 --verify-only \
  --corpus-dir ./work-vdf-public-65536 \
  > work-vdf-32-verify.jsonl
```

记录名为 `work-<iterations>-<round>.bin`，内容使用生产代码的 `EncodeWork` 编码，只有周期编号、历史摘要、前一份输出和证明。读取时检查长度、编码、周期、历史摘要、前后输出关系和数学证明。

程序使用这些记录调用生产代码的 `CreateTicket`，抽查参数为四组、每组八份。历史达到 32 份时，每张票携带 32 份 Work；不足 32 份时，按生产规则携带全部已有记录。记录多于 32 份时，按生产规则抽取四组历史记录。`--verify-only` 会重新构造公开测试票，测量完整验证及缓存命中。

## 比较保存中间值的证明算法

```bash
taskset -c 0 ./build-work-vdf-calibration/work_vdf_benchmark \
  --iterations 3000000 --rounds 1 --verify-repeats 5 --compare-stored \
  > work-vdf-3000000-compare.jsonl
```

程序先调用当前生产路径 `CreateWork`，再对同一挑战调用 `ProveSlow`。第二次调用单独计时，其完整 200 字节证明必须与生产路径一致，并再次通过 `VerifyWork`。比较只影响这个测试程序，不修改生产代码选择证明算法的阈值。

## 输出

标准输出每行一个 JSON 对象：

| `type` | 含义 |
|---|---|
| `config` | 本次迭代次数、记录数、验证次数和测试模式 |
| `evaluate_output` | 构造判别式、执行 T 次连续平方并序列化输出的时间，不构造证明；输出必须与 `CreateWork` 相同 |
| `create_work` | 生产路径生成完整 Work 的时间，包括输出和证明生成 |
| `create_work_stored_intermediates` | `--compare-stored` 额外执行 `ProveSlow` 的时间 |
| `verify_work` | 一份 Work 的完整数学验证，不使用应用层证明缓存 |
| `ticket_size` | 本张票的 Work 数量和完整编码字节数 |
| `verify_ticket_cold` | 完整验证一张票，传入的验证缓存为空指针，每次都验证全部证明 |
| `verify_ticket_cache_fill` | 使用一个空缓存验证并保存结果，仅执行一次 |
| `verify_ticket_cache_hit` | 使用已填充的缓存验证同一张票；仍检查票的上下文、历史摘要和组内连续性 |
| `summary` | 每类操作的样本数、最小值、中位数、P95、最大值和总耗时 |

计时记录包含：

| 字段 | 含义 |
|---|---|
| `wall_seconds` | 该次操作经过的实际时间，使用单调时钟 |
| `cpu_seconds` | Linux `getrusage` 记录的用户态与内核态 CPU 时间增量 |
| `peak_rss_kib` | Linux 进程从启动到此时的历史内存峰值，单位 KiB |
| `valid` | 本次验证或输出比较是否通过 |
| `round` | Work 的零起始周期编号；票计时中为本次记录总数 |
| `repeat` | 零起始的验证重复编号 |

`peak_rss_kib` 不会在操作之间清零。要比较两个参数的内存峰值，应分别启动进程。`--compare-stored` 记录的峰值也包含此前执行过的操作。

单独计算输出和 `CreateWork` 会各自从头计算一次，二者耗时不能相加作为生产生成耗时。`CreateWork` 计时不包含后续验证；保存文件和打印 JSON 也不计入对应操作的时间。`verify_ticket_cold` 指没有应用层验证缓存，操作系统和 CPU 缓存不做清空。

中位数采用排序后中间值，偶数样本取中间两项平均；P95 取排序后第 `ceil(0.95 × N)` 项。`sum_seconds` 为该类操作的墙钟时间之和。当前仅 Linux 收集 CPU 和 RSS，其他平台这两个字段为零。
