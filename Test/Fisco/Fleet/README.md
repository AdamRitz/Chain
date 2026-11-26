# FISCO BCOS 3.7.3 百节点测试

在同一批 100 台服务器运行 PBFT100 或 rPBFT5，每台一个节点。控制机运行一个 Java 发送程序，通过 4 个 SDK 入口发送原生转账。默认预热 2 秒、测量 30 秒、等待 5 秒。

比较 TPS 时固定硬件、交易业务、账户数、负载和时长。本轮使用每台 4 核、8 GiB 内存，1024 个账户，环形转账，每笔金额 1；签名在计时前生成。Java 和 SDK 仅安装在控制机。

| 参数 | PBFT100 | rPBFT5 |
|---|---:|---:|
| 完整节点 | 100 | 100 |
| 工作共识节点 | 100 | 5 |
| 最大交易数/块 | 6000 | 6000 |
| min_seal_time | 200 ms | 200 ms |
| leader_period | 1 | 1 |
| epoch_block_num | 1000 | 1000 |
| 交易池上限 | 200000 | 200000 |

两种配置均开启 DAG、交易树广播和区块树广播。`epoch_block_num` 控制 rPBFT 名单轮换。保留官方 3000 ms 共识超时、RPC 4 线程和按 CPU 核数设置的验签线程。

## 准备控制机

控制机需要 Linux x86-64、OpenSSL、Bash 和 Ansible。节点需要 Python 3.9+、systemd 和可用的 SSH。`PY` 指向已安装 Ansible 的 Python 环境。保留仓库的 `Test/Fisco` 目录，准备程序复用其中的 `pom.xml`、`vendor/` 和日志配置。

```bash
TOOLS="$PWD/Test/Fisco/Fleet"
PY=/path/to/ansible-venv/bin/python
ROOT=/opt/fisco-fleet
INVENTORY=/path/to/inventory.json
CONNECTION=/path/to/connection-env.json

"$PY" "$TOOLS/prepare.py" --root "$ROOT"
```

`prepare.py` 下载官方 FISCO 3.7.3、Azul JDK 17、Maven 3.9.9，缓存 SDK 3.7.0 并编译驱动。Maven 压缩包核对官方 SHA512，节点核对固定 SHA256。可用 `--jdk-url`、`--jdk-sha256`、`--maven-url`、`--maven-mirror` 调整下载地址；`--driver-only` 单独准备 Java 驱动。

GitHub 下载较慢时，可先运行分段下载，再执行上面的准备命令：

```bash
"$PY" "$TOOLS/parallel_download.py" \
  --output "$ROOT/downloads/fisco-bcos-linux-x86_64.tar.gz" --threads 32
```

分段下载检查 HTTP Range、长度、缓存摘要、ELF 格式和节点 SHA256，完成后替换目标文件。

## 生成和分发

主机清单使用 Ansible 的 `all.children.chain.hosts`，每台包含 `ansible_host`，必须恰好 100 台。连接参数由 `--connection` 指定，密码通过 `CHAIN_SSH_PASSWORD` 环境变量传入。

```bash
read -rsp 'SSH password: ' CHAIN_SSH_PASSWORD; echo
export CHAIN_SSH_PASSWORD
COMMON=(--root "$ROOT" --inventory "$INVENTORY" --connection "$CONNECTION")

"$PY" "$TOOLS/deploy.py" prepare "${COMMON[@]}" --openssl /usr/bin/openssl
"$PY" "$TOOLS/deploy.py" stage "${COMMON[@]}" --profile pbft100
```

[官方建链脚本](https://raw.githubusercontent.com/FISCO-BCOS/FISCO-BCOS/v3.7.3/tools/BcosAirBuilder/build_chain.sh)保留原件。OpenSSL 适配副本显式生成带 `CA:TRUE`、`keyCertSign` 和 `cRLSign` 的根证书，并跳过权限功能关闭时无用的账户工具下载。生成和分发前校验 100 套节点证书及 100 套 SDK 证书。

官方配置生成中断时，只有 100 套完整且唯一的身份均已存在，才复用这些身份完成配置。已有根证书缺少 CA 扩展，可在首次分发前修复，再重新生成配置包：

```bash
"$PY" "$TOOLS/deploy.py" prepare "${COMMON[@]}" --openssl /usr/bin/openssl --repair-ca
```

修复保留原 CA 私钥、Subject、序列号和 SKI，验证全部叶证书后更新 CA 副本。已分发的配置包按摘要固定；改变证书后使用新的实验根目录。身份恢复也可单独运行 `recover_generated.py --root "$ROOT" --inventory "$INVENTORY" --openssl /usr/bin/openssl`。

## 运行 30 秒测试

每轮使用新的运行名。基准输出放在 `results/RUN/benchmark`，与部署记录分开。

```bash
PROFILE=pbft100
RUN=pbft100-8000-001

cleanup() {
  "$PY" "$TOOLS/deploy.py" stop "${COMMON[@]}" --profile "$PROFILE" --run "$RUN" &&
  "$PY" "$TOOLS/deploy.py" collect "${COMMON[@]}" --profile "$PROFILE" --run "$RUN" &&
  "$PY" "$TOOLS/deploy.py" clean "${COMMON[@]}" --profile "$PROFILE" --run "$RUN"
}
trap 'cleanup || exit 1' EXIT

"$PY" "$TOOLS/deploy.py" start "${COMMON[@]}" --profile "$PROFILE" --run "$RUN"
"$PY" "$TOOLS/benchmark.py" --root "$ROOT" --hosts "$ROOT/hosts.json" \
  --output "$ROOT/results/$RUN/benchmark" --rate 8000 \
  --warmup 2 --seconds 30 --drain 5 --head-poll 0.2 --sample-hz 2
```

测试 rPBFT5 时先 `stage --profile rpbft5`，再将 `PROFILE` 和 `RUN` 改为对应值。两个配置复用相同节点身份及 CA，每次运行使用独立数据库。`deploy.py` 的 `--dry-run` 仅生成 Ansible playbook。

`summary.json` 保存 TPS、实际发送速率、确认时间 P50/P95/P99、失败交易数、余额核对及 100 台状态核对结果。TPS 使用 100 台最小交易计数的增量除以实际测量秒数。确认时间从计划发送时刻开始，到交易成功且 100 台均达到该块高度；`--sample-hz 2` 每秒抽查两笔。全部已提交交易成功并完成后，按实际提交索引重建 1024 个账户余额。

每轮退出时停止临时服务，保存节点日志、配置和状态，再删除本轮 `node/data`。清理要求证据包已拉取且摘要一致、进程已停止、数据库锁可获取。配置、证据和清理记录保留在运行目录。

## 本地测试

```bash
python -B -m unittest discover -s Test/Fisco/Fleet -p 'test_*.py' -v
```

包含下载缓存、HTTP Range、配置生成、身份复用、进程目录检查、数据库清理、SDK 参数和统计测试。证书测试调用本机 OpenSSL，复现缺少 CA 扩展的验证失败，再核对修复后的证书链。
