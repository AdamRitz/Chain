# Ansible 多节点部署与对照测试

控制端为 Linux、Python 3.11+，本轮固定 Ansible Core 2.19.15；目标为有 systemd 的 x86-64 Linux。inventory 的 `chain` 组包含全部机器，每台设置唯一的 `chain_node_id`、SSH 地址 `ansible_host`、节点之间可达的 `chain_p2p_address`。内网主机可使用 `ProxyJump`。示例只含文档保留地址，不含实际机器或密码。

```bash
python3 -m venv .venv
.venv/bin/pip install -r ansible/requirements.txt
cp ansible/inventory.example.yml /srv/chain/inventory.yml
cp ansible/vars.example.yml /srv/chain/run.yml
cd ansible
../.venv/bin/ansible-playbook -i /srv/chain/inventory.yml stage.yml -e @/srv/chain/run.yml
```

目标端 `chain_python` 需 Python 3.9+ 和 `psutil`；Ansible 模块的 `ansible_python_interpreter` 可与它不同。默认值在 `group_vars/all.yml`，可在清单或 `-e` 覆盖。预先准备 SSH known_hosts；凭据使用 SSH key、`--ask-pass` 或 Ansible Vault。

## 发布包

`chain_releases` 为 `{id, package, sha256}` 数组，可同时准备 baseline、新版。`package` 为控制端 tar.gz 绝对路径，`sha256` 是压缩包 SHA256。单版本也可用 `chain_release_id`、`chain_package`、`chain_package_sha256`。

```text
manifest.json
bin/boost
bin/sender
bin/evm_sender
lib/<shared libraries>
source/<repository including Test/ClusterWorker.py and Test/Netem.py>
```

打包时将符号链接解引用为普通文件。清单覆盖全部普通文件，`manifest.json` 本身除外：

```json
{"schema":1,"source_commit":"0123456789abcdef0123456789abcdef01234567","files":[
  {"path":"bin/boost","sha256":"64 lowercase hex digits","mode":"0755"}
]}
```

stage 核对两端包哈希、归档路径与容量、每个文件哈希、ELF 和依赖库，再原子发布到 `<chain_release_root>/<id>`。同 ID 不可换包；已有版本每次重新核验。源码、依赖、发布包和旧版本保留。

## 一键测试

配置增加 `chain_dataset`、`chain_dataset_sha256`、`chain_genesis_file`、`chain_genesis_sha256`、唯一 `chain_run_id`、绝对 `chain_result_dir`。数据集和创世文件位于控制端。`chain_extra_vars_file` 可指向同一配置，供 driver 后续复用。

```bash
../.venv/bin/ansible-playbook -i /srv/chain/inventory.yml benchmark.yml -e @/srv/chain/run.yml
```

执行 preflight → stage → 输入哈希 → context → `Test/AnsibleBenchmark.py --ansible-context context.json` → 无论 driver 成败都 stop、collect、delete-db。测试默认使用 18089；本 playbook 只管理隔离实验进程。公平对照所需的原 Chain/FISCO 暂停与恢复由 `service_guard.yml` 和调用方外层 `finally` 管理，并设置远端恢复 watchdog。

DB 布局为 `<chain_test_root>/<case_run_id>/<node_id>/db`。`run_group` 只索引 `.ansible-runs/<run_group>/` 的回收记录。总测试生成 32 位小写 UUID `run_owner`；每次操作核对 owner、group、release、PID、创建时间、二进制和完整 argv。既有 case 不被新的 start 接管。默认 300 秒到期保护只停止有记录的实验进程，不自动删库。

context 的 `releases[].sha256`、`binary_sha256` 都是实际 boost 哈希，`package_sha256` 是压缩包哈希。context 包含清单路径、SSH/P2P 地址、数据集、创世、版本目录和结果目录。driver 负责相同发压条件、实际发送率、共同链边界、账户状态及延迟核对。

context 与回收结果保存在 `chain_result_dir`；driver 独占全新的 `chain_result_dir/benchmark` 子目录。同一次测试不要重复使用已有 benchmark 目录，新测试使用新的 run ID 和输出目录。

## Worker 与恢复接口

`workers.yml` 一次覆盖选中机器，`--limit` 可选择 10 或 20 台。结果写入 `<chain_worker_results_dir>/<node_id>.json`，格式为 `{ok,result}`；失败也先保存证据再报错。

| 变量 | 含义 |
|---|---|
| `chain_worker_action` | start、stop、collect、export-headers、delete-db |
| `chain_worker_release_id` | 已 stage 的版本 |
| `chain_run_id` | 当前 case ID，最多 100 个字母/数字/下划线/连字符 |
| `chain_run_group` | 总测试 ID |
| `chain_run_owner` | context.run_owner 的 32 位 UUID hex |
| `chain_worker_genesis` | start 的创世 JSON 对象 |
| `chain_worker_arguments_by_node` | node_id 到 argv 数组的映射，优先于共用参数 |
| `chain_worker_arguments` | 共用 argv，必须恰好一个隔离端口参数 |
| `chain_worker_restart` | 同 owner 且已停止的 case 重开，默认 false |
| `chain_worker_results_dir` | 此动作的控制端证据目录 |

禁止 arguments 覆盖 `--data`、`--genesis`、`--metrics`。start 保留实际 `binary_sha256`。export-headers 在归属核对且停止的同一 DB 上，使用保留的 HeaderReader 和该 release 的 RocksDB 库只读导出块头与分数。

```bash
../.venv/bin/ansible-playbook -i /srv/chain/inventory.yml cleanup.yml -e @/srv/chain/run.yml \
  -e chain_run_group=original_group -e chain_run_owner=original_32_hex_owner
```

回收先停止、收集，再请求既有 ClusterWorker 删本轮 DB。路径或身份核对失败则保留该 DB、返回错误，其他节点继续。代码、参数、创世、记录和日志保留。

## 滚动部署与回退

原 `chain-node.service` 需正在运行并显式包含唯一的 `--data`。设置新版 release ID 和唯一部署 ID：

```bash
../.venv/bin/ansible-playbook -i /srv/chain/inventory.yml deploy.yml -e @/srv/chain/run.yml \
  -e chain_release_id=optimized-example -e chain_deployment_id=upgrade_20261116
```

默认逐台执行，`chain_serial` 可调整批次。从 MainPID 读取原 argv 和 DB，只替换独立 systemd drop-in 中的可执行文件与依赖路径。重启核对程序、全部参数、DB、创世和链排名；失败自动还原旧 override、重启并核对恢复，同时停止后续批次。数据库不迁移、不删除。

原 override 和前后快照保存在 `<chain_tools_root>/deployments/<deployment_id>/<node_id>.json`。显式回退：

```bash
../.venv/bin/ansible-playbook -i /srv/chain/inventory.yml rollback.yml -e @/srv/chain/run.yml \
  -e chain_deployment_id=upgrade_20261116
```

回退拒绝覆盖部署后另行修改的 override。当前协议和 schema 7 保持兼容；自动化不引入提议委员会或最终性证书。

## 验证

```bash
python -m unittest discover -s ansible/tests -v
../.venv/bin/ansible-playbook -i /srv/chain/inventory.yml benchmark.yml --syntax-check -e @/srv/chain/run.yml
```

回归覆盖包校验、路径/链接、重复发布、防篡改、失败部署还原及 DB 保留、既有 case 拒绝、跨 owner 拒绝和失败回收。默认并发 SSH 为 32，机器数由清单决定；1024 节点吞吐和亚秒最终性需要协议及规模实验另行验证。
