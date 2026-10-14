# 复现入口

`BuildA5Cloud.py` 构建导出的固定提交并运行 Ubuntu 回归；`RunA5Ten.py` 部署同一包，运行五档十台短测并清理每轮数据库。`CloudSixC4.py` 提供现有 SSH 连接、远程调用、工作器和主机采样函数；`RunA5Ten.py` 设置本次发布、结果和临时目录。

`PreflightA5Cloud.py` 保存连接、磁盘、端口和进程核对，`CleanA5LinuxChecks.py` 清理本轮 Ubuntu 回归临时数据库。密码通过隐藏输入传入进程内存。路径和发布版本在脚本开头定义，首次运行使用独立目录；再次运行设置新的轮次目录并核对旧进程。

`AnalyzeA5Ten.py` 在测量样本的首尾计算每节点累计计数增量，并与上次短测比较。主机采样通过交易样本的单调时钟起点映射到 UTC 测量区间。核心编译源码来自 Git 提交 `a5cf8a2`。
