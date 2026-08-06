# mcp-dexec

`mcp-dexec` 让宿主机上的 Codex 访问一个已经运行的 Docker 容器。服务在启动
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

## Usage

在 monorepo 根目录安装依赖并以前台方式手动启动服务：

```bash
# need comment
uv sync --project ./mcp_dexec --no-dev --locked
# need comment
uv run --project ./mcp_dexec --no-dev --locked \
  mcp-dexec --container simjoin --runlog-dir ./RUNLOG \
  serve --host 127.0.0.1 --port 9943
```

`--container` 接受已运行的 Docker 容器名称或 ID。服务仅允许绑定回环地址；
按 `Ctrl-C` 即可停止前台服务。

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
data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26",...,"serverInfo":{"name":"simjoin-dexec","version":"1.28.1"},...}}
```

### Codex MCP 配置

每个 Agent 工作目录的 `.codex/config.toml` 使用相同配置：

```toml
[mcp_servers.dexec]
url = "http://127.0.0.1:9943/mcp"
startup_timeout_sec = 60
tool_timeout_sec = 3700
default_tools_approval_mode = "prompt"
```

这里没有 `command`、`args`、`cwd` 或 `UV_CACHE_DIR`：Codex 只是连接已经
运行的服务，不再为每个会话启动 Python/uv 子进程。monorepo 根目录没有
`.codex`；需要进入 `projects/<项目>` 后启动 Codex。

### Manually 操作

Manually 调用 `dexec`，可以复用同一个执行核心而不经过 MCP：

简单调用：

```bash
uv run --project ./mcp_dexec --no-dev --locked \
  mcp-dexec --container simjoin \
  exec 'pwd'
```

通过 stdin 执行多行 Bash 脚本：

```bash
uv run --project ./mcp_dexec --no-dev --locked \
  mcp-dexec --container simjoin \
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
uv run --project ./mcp_dexec --group test --locked pytest -q
```

不要为了测试替换正在运行的 9943 服务。可以另开进程和端口：

```bash
uv run --project ./mcp_dexec --no-dev --locked \
  mcp-dexec --container simjoin --runlog-dir /tmp/mcp-dexec-test-runlog \
  serve --host 127.0.0.1 --port 19943

MCP_DEXEC_LIVE=1 MCP_DEXEC_URL=http://127.0.0.1:19943/mcp \
  uv run --project ./mcp_dexec --group test --locked \
  pytest -q mcp_dexec/tests/test_live_patch.py
```

服务运行后，Streamable HTTP 集成测试会并发初始化四个客户端，并通过服务
验证 stdin、进度、自然 124、超时和取消后的进程清理：

```bash
MCP_DEXEC_LIVE=1 uv run --project ./mcp_dexec --group test --locked \
  pytest -q mcp_dexec/tests/test_live_mcp.py
```
