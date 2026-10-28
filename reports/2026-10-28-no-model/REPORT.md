# Chain 无学习模型基线恢复

日期：2026-10-28。按用户要求，将当前 Chain 节点恢复为 `933905448951c600156a8ad2a0b3c3a895916fb5` 的运行实现，同时保留最新 FISCO 原生转账与容量测试工具。以新提交完成文件级恢复，保留此前完整 Git 历史。

## 恢复范围

以下 13 个文件与指定基线逐字节一致，SHA256 见 [源码验证](data/source-validation.json)：

- 节点与构建：`main.cpp`、`CMakeLists.txt`。
- 状态与网络：`DB/DB.h`、`Network/Client.h`、`Network/Server.h`。
- 时间与交易：`Time/Time.h`、`Transaction/Block.h`、`Transaction/Transaction.h`。
- 配套测试：`Test/Cluster.py`、`Test/EvmNetworkTest.py`、`Test/FlowTest.cpp`、`Test/ForkStateTest.cpp`、`Test/UserTest.cpp`。

当前节点沿用四时间戳/中位数同步、固定轮次候选收集、原生账户规则、EVM、分叉恢复及同步写盘。数据库格式仍为 schema 7。本次没有迁移既有数据库，也没有新跑性能压测。

学习模型、候选策略头文件、模型 JSON、训练工具和模型专属测试源码保留为历史研究资料。通过 Ninja 的实际编译依赖核对，当前节点只引用 `Time/Time.h`，不引用学习模型/候选策略头或 `Tool/VerificationBudget.h`，见 [编译依赖](data/compiled-includes.json)。不是仅把新模型开关设为关闭。

当前节点不再接受模型版本的 `--clock-control`、`--clock-model`、`--round-scheduler` 及后续候选策略参数。实际探测 `--clock-control learned` 返回 `Unknown argument`，且未创建数据库。原有 `--clock-sync`、`--round-ms`、`--candidate-ms` 等基线参数继续有效。复现模型实验需构建对应报告记载的历史提交。

## FISCO 与已有资料

对工作区 `Test/Fisco` 中 39 个非缓存文件逐个记录并复核 SHA256，本次均未改变，包括 `Aligned` 原生 `0x100c` 驱动和 `Capacity` 容量测试、签名有效期处理、十路发送、资源采样、证据压缩及离线分析。文件摘要见 [FISCO 保留清单](data/fisco-preserved.json)。

FISCO 的 [原生转账结果](../2026-10-23-fisco-native/REPORT.md)、[配置探索与五档短测](../2026-10-26-fisco-capacity/REPORT.md)、[五档 60 秒 ×3 次测试](../2026-10-27-fisco-long/REPORT.md)，以及 Chain 的全部历史实验报告继续保留。FISCO 驱动通过独立 SDK/JSON-RPC 工作，不依赖被移出当前节点构建的 Chain 模型接口。

开始任务时已有的未提交修改和未跟踪文件保留在工作区，没有混入本次提交。`MODULE_GUIDE.md` 的既有 FISCO 行修改单独保留为未暂存差异，本次仅提交当前基线相关的文档更新。FISCO 保留清单是工作区快照，也包含已有的本地未提交工具。

## 验证

使用 Windows MinGW UCRT64 Release 构建成功。正式 CMake 测试恢复为该基线的 11 项：ChainCorrectness、AccountCorrectness、ForkStateCorrectness、ThroughputCorrectness、FlowCorrectness、ChainNetwork、AccountNetwork、ForkNetwork、EvmNetwork、ExperimentNetwork、AdmissionNetwork。

首轮 10/11 通过；ExperimentNetwork 的一个本地节点遇到 Windows `bind` 错误 `system:10013`，未成功监听端口。只对该失败项使用新临时数据库重跑，6.30 秒通过；未修改节点或测试逻辑以消除失败。首轮与重跑结果均保留：[首轮 XML](data/windows-ctest.xml)、[重跑 XML](data/windows-ctest-retry.xml)。最终 11 项均有通过记录。

FISCO `Capacity/TestCapacity.py` 的 3 项分片、证据压缩与日志窗口检查通过；`Test/Fisco/TestAnalyze.py` 的 7 项分析回归通过。模型专属的 ClockCorrectness、ClockToolsCorrectness、ClockNetwork 不属于无模型基线的构建集合，其源码仍保留。汇总见 [验证记录](data/validation.json)。

## 本地临时数据库与部署范围

测试仅使用新建的 `build/no-model-20261028-*` 目录。结束后核对没有进程使用该测试目录。安全策略拦截了按目录边界核对后的递归删除请求，工具返回 `blocked by policy`；因此 23 份临时 RocksDB 数据库保留原样，路径和大小记录在 [数据库清理记录](data/database-cleanup.json)。未将它们加入 Git。

本次完成本地仓库、Windows 构建和 GitHub 代码版本切换；没有修改十台服务器的常驻服务或 FISCO 配置，也没有执行新的十节点部署或压测。未来部署此基线时需同步使用基线支持的启动参数，不能沿用历史服务中 `learned/bounded` 的模型参数。
