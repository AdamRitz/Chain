# 上游来源

两个合约包装类保持上游原文件内容，来自 FISCO BCOS `java-sdk-demo` 的 `v3.10.0` 标签，采用 Apache License 2.0，许可证见同目录 `LICENSE`。

| 文件 | SHA-256 |
|---|---|
| ParallelOk.java | d24a52100d9c72567655e4299aabd7935d767dee4c95d920e8e5d68e3ca6567d |
| DagTransfer.java | a2b4bcf89766dee7bb7eb54c83223aa3ac7625ce971d3bda34dbac0aee6517f1 |

源文件：[ParallelOk.java](https://github.com/FISCO-BCOS/java-sdk-demo/blob/v3.10.0/src/main/java/org/fisco/bcos/sdk/demo/contract/ParallelOk.java)、[DagTransfer.java](https://github.com/FISCO-BCOS/java-sdk-demo/blob/v3.10.0/src/main/java/org/fisco/bcos/sdk/demo/contract/DagTransfer.java)。

ParallelOk 使用上游内置 Solidity 字节码与并行 ABI。DagTransfer 调用地址 `0x000000000000000000000000000000000000100c` 的预编译转账合约。
