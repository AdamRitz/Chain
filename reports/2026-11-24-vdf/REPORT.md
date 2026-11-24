# VDF 新链实现、部署与功能验证

2026-11-24。dev-006 在一台 2 vCPU 的 EL9 x86-64 云服务器上完成全量测试，并以独立 systemd 服务运行。源码、调试符号、创世配置和节点数据库分别保留；GDB 已命中 `PrepareVdfBlock`。

源码以 `d566e81214633492ffb5de4d930e4798f6d6b5a8` 为基准叠加本次工作树修改。实际测试内容由下列源码包及逐文件清单标识，基准提交号不能单独标识本次改动。完整构建后核对 780 个文件，差异仅为 `ansible/scripts/vdf_instance.py` 和对应 Python 测试；C++ 源码保持一致。

| 制品 | SHA-256 |
|---|---|
| dev-006 源码包 | `26fc52cefb74eb4343fbdedb86bc909880e986d9adeffccb466725d3f66bebd0` |
| 源码逐文件清单 | `a6487250261eb8fb5fcd5618c39480c1d5c4acd670bb7e20d6dc44969fc0be2d` |
| 节点二进制 `boost` | `5a0f7ac1a84caa3f3b0d7d07f00296ded11f4bd9750654fe032ac0a27ef569ba` |
| 修正后的部署脚本 | `df04220f240973dc9ae6b7aedb3f4d3001eb04c8ddd9810e557ba5dd0b42988a` |

## 验证结果

GCC 13、`RelWithDebInfo` 构建，启用 Classroom 目标。Ansible 完成源码校验、构建、全量 CTest 和独立实例部署。

| 验证 | 结果 | 耗时 |
|---|---|---:|
| dev-006 全量 CTest | 30/30，通过且无跳过 | 177.18 s |
| `VdfNetwork` | 同机 7 个真实进程，5 个场景全部通过 | 40.4466 s |
| 修正部署脚本后的 Python 测试 | 64/64，通过且无跳过 | 1.035 s |

七进程测试使用 6 个生产节点和 1 个仅同步节点，执行 `election`、`rejection`、`restart`、`partition`、`fallback`。每个场景独立创建创世和数据库，使用真实 VDF，参数为 `iterations=4096`、`epoch_blocks=8`、`slot_ms=250`、`fallback_slots=20`。覆盖选举名单、无效证明及身份拒绝、重启、分区重组，以及停止当前五名生产者后由第六个身份恢复出块。

此前发现的 Linux 重启端口占用已通过 `reuse_address` 修正。fallback 测试曾将接收端延迟提交的旧成员块误认作恢复块，现按停机后发送的唯一交易定位，并检查实际名单、生产者、当前证明和父子槽间隔。部署脚本修正了 `WorkingDirectory` 的单值路径编码，Linux `systemd-analyze verify` 回归已通过；该修正以独立部署工具应用，保持已验证节点源码包与二进制不变。

## 正式实例与源码调试

独立服务 `chain-vdf-vdf-20261124-002.service` 监听 TCP 18089。正式链采用 `iterations=65536`、`epoch_blocks=32`、`slot_ms=50`、`fallback_slots=20`、`future_slots=2`，统一 `start_time_ms` 写入创世。配置最多五名正常生产者；本次正式实例运行一名生产者。

在同一服务器启动新的观察节点后，历史区块同步及区块字节核对通过。发送 2 笔签名转账后，服务与观察节点均达到高度 1290，链头相同、链上交易数均为 2、手续费销毁均为 2。发送账户余额为 `999999999999984`、nonce 为 2；接收账户余额为 14、nonce 为 0。观察节点产生的 VDF 和本地候选均为 0，随后正常退出。

最终健康检查中，服务保持 `active`，高度从 15409 增至 15430，所在组为 482，未报告失败。独立调试进程的 GDB 断点命中 `Network/Vdf.h:123` 的 `PrepareVdfBlock`，并能查看源码及调用栈。最终收尾清理 94 个测试数据库，共 141,685,398 字节，保留日志、配置、身份、钱包、SQLite 和测试结果。

## 当前范围

主机防火墙已允许 TCP 18089；从外部测试 SSH 可达，18089 连接仍超时，云安全组待核对。公网节点连接尚未通过。

本轮完成同机多进程协议正确性及正式实例功能验证，未运行 1000 节点 TPS 压测，也未测量大规模网络下的最终确认延迟。`slot_ms=50` 本身不能证明亚秒最终确认。本轮未生成新的 Windows 安装包。

协议及复现参数见 [VDF_GUIDE.md](../../VDF_GUIDE.md)，部署入口见 [ansible/vdf_deploy.yml](../../ansible/vdf_deploy.yml)。脱敏结果及私有原始证据的文件校验值见 [summary.json](summary.json)。
