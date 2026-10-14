# 本轮测试脚本快照

这些文件保存本轮十节点部署、参数和测量方式，运行位置为二号服务器 `/home/ubuntu/chain`。节点地址和版本目录固定为本轮环境。正式实验入口与可扩展清单见项目 `Test/Experiments.py`、`EXPERIMENTS.md`。

- `CloudThroughputNextV2.py`：同一签名数据、上版与新版交替测试，五档输入各重复三轮。
- `RoundNextV2.py`、`Round100NextV2.py`：新版 50、100 毫秒轮次对照，各六轮。
- `LaunchNext.py`：隐藏输入连接密码，通过标准输入传给子进程，独立运行并保存进程身份及日志。连接密码保留在运行内存中。
- `HostProfileNextV2.py`：每秒采样处理器、磁盘和节点线程信息，结束标记由主测试脚本写入。
- `CleanOldTests.py`：核对旧测试目录、进程身份，归档记录并校验后，清理指定旧测试数据库。清理清单和归档保存供复核。
- `PostflightNext.py`：检查全部节点退出、端口关闭、版本校验值和剩余空间。

本轮使用 `2026-10-13-throughput` 和 `2026-10-14-tps-v2` 两个发布目录，Python 解释器来自已有的 `2026-09-30-d4106d1/.venv`。交易文件由新版 `sender --prepare ... --count 3500000 --wallets 1024` 生成，后续两组轮次使用硬链接复用。三个正式结果目录均有 `plan.json`、`deployment.json`、`dataset-sha256.txt`、`results.json` 和各轮节点记录。

复跑时使用新的结果目录和运行标识，保持两版程序、数据摘要、账户、线程、同步落盘和发送方式固定。每轮结束核对共同链和完整账户，超时及未完成结果一起保存。
