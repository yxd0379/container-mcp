# dexec 实现说明

## MCP 工具与线程关联

Codex 0.144.6 会在每次 MCP `tools/call` 的 `_meta.threadId` 中附带当前线程
UUID。服务使用该请求级 ID 写入
`RUNLOG/YYMMDD_<threadId>.log`，调用方无需也不能传 `thread_id`：

```json
{
  "command": "cd /root/repos/feature_retrieval && pwd",
  "timeout_sec": 10,
  "stdin": "optional UTF-8 text"
}
```

若 `_meta.threadId` 缺失或不是 canonical UUID，服务会在执行 Docker 前
失败关闭。服务启动环境中的 `CODEX_THREAD_ID` 是静态快照，不能区分多个
Codex、子 Agent 或嵌套 Codex，因此不会用于日志关联。

## Codex patch

`apply_patch(patch)` 接受 Codex 的 `*** Begin Patch` 格式，支持 Add、Delete、
Update 和 Move。所有路径必须是容器内绝对路径，因此工具没有 `cwd`、workspace
或路径权限层；文件访问权限由固定容器及其 `docker exec` 用户决定。

宿主机 Python 负责解析 patch、读取原内容、匹配上下文并生成新内容。容器内
只执行固定的 Bash 读写脚本，文件路径通过 argv 传递，文件内容通过 stdin
传递，不会把模型文本拼接到 shell 命令中。实现依赖当前 dexec 已经要求的
Bash、GNU timeout 和 coreutils，不在容器内安装 helper。

同一服务进程只有一个 patch `asyncio.Lock`，所以同一固定容器的 patch 调用
全局串行，不使用 workspace 锁、树形锁或文件锁。该锁不阻止并发 `dexec`
命令或容器内其他进程修改文件。

写入分成两段：先预检查所有 hunk，常见的格式和上下文错误不会产生任何写入；
再按 patch 顺序逐文件提交。普通写入在目标目录创建临时文件并用 rename 原子
替换；删除使用单文件 remove；Move 是“写目标、删除源”两个步骤。没有多文件
事务、回滚、journal 或恢复。提交中途失败时工具返回错误并列出已完成操作。
patch 不写独立 RUNLOG。

## 容器执行与 stdin

服务内部通过 `docker exec <container> timeout ... bash -ic` 执行命令。
`stdin` 省略时不传 `docker exec -i`；即使传入空字符串，也会使用 `-i` 并
关闭输入流。二进制数据不应通过该文本参数传递。

MCP 工具通过可选的 `stdin` 字段接收文本。Manually 调用的 CLI 使用
`exec COMMAND [-]`：省略尾部参数时不传 stdin，尾部 `-` 表示读取当前
进程的 stdin 并转发给容器命令。CLI 不接受 stdin 文件路径；需要读取文件时
使用 shell 重定向，例如 `mcp-dexec ... exec 'bash -s' - < script.sh`。

## 输出、超时与取消

stdout/stderr 直接增量写入临时磁盘文件，内存只保留有限预览与尾部。
服务约每秒发送一次 MCP logging/progress 通知；最终工具结果中每个流最多
返回 60,000 字符，完整内容在集中 RUNLOG 中。

容器侧 GNU `timeout`、wrapper pidfile 和取消清理共同管理命令进程树。
自然返回的退出码 124 与超时会被区分；超时、客户端取消和内部异常都会在
清理并持久化 RUNLOG 后结束。主动 double-fork 或 daemonize 以脱离进程树的
命令不受支持。
