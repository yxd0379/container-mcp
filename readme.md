# container-mcp

`container-mcp` 让宿主机上的 Codex 访问一个已经运行的 Docker 容器。服务在启动
时绑定目标容器，并通过 `http://127.0.0.1:9943/mcp` 提供 Streamable HTTP
接口；monorepo 中的所有 Codex 项目共享同一个服务进程。

服务提供两个工具：

- `dexec(command, timeout_sec?, stdin?)` 在容器中执行命令，并将完整命令、
  stdout 和 stderr 集中记录到 monorepo 根目录的 `RUNLOG/`。
- `apply_patch(patch)` 使用 Codex patch 格式修改容器内文件。patch 中的 Add、
  Delete、Update 和 Move 路径都必须是容器内绝对路径。

所有 patch 请求在服务内全局串行。每个 patch 会先完成解析和上下文检查，再按
顺序写入文件；普通写入通过同目录临时文件和原子 rename 提交。服务不提供
多文件回滚，提交中途失败时会返回已经完成的操作。patch 不单独写 RUNLOG。

## 启动与停止

在 monorepo 根目录启动后台服务：

```bash
uv run --project ./container_mcp --no-dev --locked \
  container-mcp --container simjoin --runlog-dir ./RUNLOG \
  start-service --port 9943
```

`uv run` 会创建或同步 `container_mcp/.venv`，然后启动与当前终端分离的服务。
关闭终端不会停止它。服务自身的 stdout 和 stderr 会追加写入
`container_mcp/.service/container-mcp.log`，不会丢失；`dexec` 执行的完整命令
及输出仍写入根目录的 `RUNLOG/`。

查看后台日志、检查状态或停止服务：

```bash
tail -f ./container_mcp/.service/container-mcp.log

uv run --project ./container_mcp --no-dev --locked \
  container-mcp status-service

uv run --project ./container_mcp --no-dev --locked \
  container-mcp stop-service
```

`--container` 接受已运行的 Docker 容器名称或 ID。HTTP 服务固定绑定本机回环
地址，不对其他主机开放。

### 检查服务

发送 MCP `initialize` 请求检查服务是否已经启动：

```bash
curl --noproxy '*' -i \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}' \
  http://127.0.0.1:9943/mcp
```

服务正常时会返回 `HTTP/1.1 200 OK`、`content-type: text/event-stream` 和
`initialize` 结果，例如：

```text
HTTP/1.1 200 OK
content-type: text/event-stream
mcp-session-id: <session-id>

event: message
data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26",...,"serverInfo":{"name":"container-mcp","version":"1.28.1"},...}}
```

### Codex MCP 配置

每个 Agent 工作目录的 `.codex/config.toml` 使用相同配置：

```toml
[mcp_servers.container]
url = "http://127.0.0.1:9943/mcp"
startup_timeout_sec = 60
tool_timeout_sec = 3700
default_tools_approval_mode = "prompt"
```

这里没有 `command`、`args`、`cwd` 或 `UV_CACHE_DIR`：Codex 只是连接已经
运行的服务，不再为每个会话启动 Python/uv 子进程。monorepo 根目录没有
`.codex`；需要进入 `projects/<项目>` 后启动 Codex。

### 人工操作

人工调用 `dexec`，可以复用同一个执行核心而不经过 MCP：

简单调用：

```bash
uv run --project ./container_mcp --no-dev --locked \
  container-mcp --container simjoin \
  exec 'pwd'
```

通过 stdin 执行多行 Bash 脚本：

```bash
uv run --project ./container_mcp --no-dev --locked \
  container-mcp --container simjoin \
  exec 'bash -s' - <<'SCRIPT'
set -euo pipefail

cd /root/repos/feature_retrieval
printf 'workdir: %s\n' "$PWD"

echo '=== NPU status ==='
npu-smi info

echo '=== source file count ==='
find src -type f \( -name '*.cpp' -o -name '*.h' \) | wc -l
SCRIPT
```

这个例子通过当前进程的 stdin 将多行 Bash 脚本传给容器内的 `bash -s`。
尾部 `-` 表示转发 stdin；省略时不向容器命令传递 stdin。
执行记录写入 `RUNLOG/YYMMDD_manual.log`，进度输出到 stderr，最终预览
输出到 stdout，并返回容器命令的退出码。

## 开发与验证

单元测试不会访问 Docker：

```bash
uv run --directory ./container_mcp --group test --locked pytest -q
```

不要为了测试替换正在运行的 9943 服务。可以另开进程和端口：

```bash
uv run --project ./container_mcp --no-dev --locked \
  container-mcp --container simjoin --runlog-dir /tmp/container-mcp-test-runlog \
  serve --host 127.0.0.1 --port 19943 \
  > /tmp/container-mcp-test-service.log 2>&1 &
test_service_pid=$!

CONTAINER_MCP_LIVE=1 CONTAINER_MCP_URL=http://127.0.0.1:19943/mcp \
  uv run --directory ./container_mcp --group test --locked \
  pytest -q tests/test_live_patch.py

kill "$test_service_pid"
```

临时服务在后台运行，stdout 和 stderr 保留在
`/tmp/container-mcp-test-service.log`；测试完成后按示例停止它。

服务运行后，Streamable HTTP 集成测试会并发初始化四个客户端，并通过服务
验证 stdin、进度、自然 124、超时和取消后的进程清理：

```bash
CONTAINER_MCP_LIVE=1 uv run --directory ./container_mcp --group test --locked \
  pytest -q tests/test_live_mcp.py
```
