# 实现概要

## 模块

- `run.py`：仓库源码入口。
- `server.py`：FastMCP、白名单、服务日志和工具注册。
- `cli.py`：前台、后台、状态、停止和人工执行命令。
- `dexec.py`：命令执行、输出预览、超时清理和 RUNLOG。
- `dinspect.py`：`docker inspect` 元信息。
- `dpatch.py`：容器文件读写与 Codex patch 解析。

## 执行模型

`dexec` 使用参数化的 `docker exec` 启动容器内 `timeout` 和 Bash。stdout/stderr
先写临时文件，返回值每个流最多保留 60,000 字符，完整内容追加到
`RUNLOG/YYMMDD_<threadId>.log`。thread ID 只接受 MCP 请求 `_meta.threadId`
中的 canonical UUID。

容器侧 pidfile、GNU `timeout` 和宿主机清理流程共同处理超时与取消。清理和日志
持久化位于 shielded cancellation scope 中；主动 daemonize 脱离进程树的命令不
受支持。

## Patch 模型

宿主机解析 patch、读取原文件并预检所有 hunk；容器内只运行固定 Bash 脚本，
路径通过 argv、内容通过 stdin 传递。普通写入使用同目录临时文件和原子 rename。

同一容器的 patch 通过 `asyncio.Lock` 串行化，不同容器可以并行。提交仍是逐文件
执行，没有多文件事务；Move 是先写目标再删除源，部分失败会返回已完成操作。

## 安全边界

容器名在调用 Docker 前经过格式和 allowlist 校验。服务只绑定 loopback，但没有
应用层认证。容器若处于 privileged 模式，或共享宿主机 namespace、设备和 bind
mount，容器内操作仍可能影响宿主机，因此高风险操作前必须检查 `dinspect`。
