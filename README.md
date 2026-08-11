# container-mcp

`container-mcp` 让宿主机上的 Codex 访问已经运行的 Docker 容器。它通过
`http://127.0.0.1:9943/mcp` 提供 Streamable HTTP 接口；本机多个 Codex 项目
可以共享同一个单例服务进程。

服务没有默认容器。启动时使用可重复的 `--allow-container NAME` 建立白名单，
每次工具调用都必须显式传入 `container`，并且只能选择白名单内的容器。一个
服务进程因此可以安全地路由多个容器，不需要为每个容器另开端口和后台进程。

服务提供三个工具：

- `container_info(container)` 获取指定运行中容器与宿主机隔离相关的只读元信息，
  包括镜像、用户、工作目录、privileged、capabilities、namespace、设备、挂载、
  网络、端口和资源限制。停止态容器不会返回元信息。
- `dexec(command, container, timeout_sec?, stdin?)` 在指定容器中执行命令，并将
  完整命令、stdout 和 stderr 集中记录到配置的 `RUNLOG/`。
- `apply_patch(patch, container)` 使用 Codex patch 格式修改指定容器内文件。
  patch 中的 Add、Delete、Update 和 Move 路径都必须是容器内绝对路径。

同一容器的 patch 请求在服务内串行，不同容器互不阻塞。每个 patch 会先完成
解析和上下文检查，再按顺序写入文件；普通写入通过同目录临时文件和原子 rename
提交。服务不提供多文件回滚，提交中途失败时会返回已经完成的操作。patch 不
单独写 RUNLOG。

## 本机安装

`.venv` 是 `uv run --project ...` 为源码工程创建的隔离 Python 环境，用来固定
解释器和依赖，避免污染系统 Python。它适合开发，但不是发行物，也不需要复制
给使用者。

在本仓库根目录执行一次本机工具安装：

```bash
uv tool install --force .
container-mcp --help
```

`uv tool install` 会构建 wheel、创建独立的工具环境，并把 `container-mcp` 命令
安装到 `uv tool dir --bin` 显示的目录（Linux 通常是 `~/.local/bin`）。若该目录
不在 `PATH`，执行一次 `uv tool update-shell`，重新打开 shell 后即可在任意目录
直接使用。源码更新后重复执行上面的 `--force` 安装命令。

也可以先构建可复制的 wheel 再安装：

```bash
uv build --wheel .
uv tool install ./dist/container_mcp-0.1.0-py3-none-any.whl
```

无论从源码还是已安装命令运行，服务状态都默认使用
`$XDG_STATE_HOME/container-mcp/`；未设置 `XDG_STATE_HOME` 时使用
`~/.local/state/container-mcp/`。两种入口因此共享同一个单例 PID 和后台日志。
安装版的默认 RUNLOG 也在该目录下；源码版默认使用本仓库根目录的 `RUNLOG/`。
可用 `CONTAINER_MCP_STATE_DIR` 覆盖状态目录，也可在启动时用绝对路径的
`--runlog-dir` 单独指定 RUNLOG。

## 启动与停止

安装完成后可在任意目录启动后台服务。下面把 `simjoin` 和 `dev-container` 同时
加入白名单；按实际容器名增删重复的参数：

```bash
container-mcp \
  --allow-container simjoin \
  --allow-container dev-container \
  --allow-container oprace \
  --runlog-dir /home/yuxd/repos/play-simjoin/RUNLOG \
  start-service --port 9943
```

`start-service` 保持全局单例：即使换端口，再次启动也会报告已有服务。关闭终端
不会停止它。安装版服务自身的 stdout 和 stderr 追加写入
`~/.local/state/container-mcp/container-mcp.log`；`dexec` 的完整命令及输出写入
上面指定的 RUNLOG。修改白名单时，先停止服务，再用新的参数重新启动。

查看后台日志、检查状态或停止服务：

```bash
tail -f ~/.local/state/container-mcp/container-mcp.log

container-mcp status-service

container-mcp stop-service
```

容器名和 ID 只允许字母、数字、下划线、点和连字符。HTTP 服务固定绑定本机
回环地址，不对其他主机开放，但没有额外认证；allowlist 是其容器访问边界。

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
运行的服务，不再为每个会话启动 Python/uv 子进程。消费项目只需要配置服务
URL，不需要依赖本仓库路径。

Agent 调用工具时必须明确目标容器，例如：

```json
{"container":"simjoin","command":"pwd","timeout_sec":10}
```

若名称不在启动 allowlist 中，服务会在调用 Docker 前拒绝请求。

执行进程终止、设备操作、挂载修改、系统配置等潜在高风险命令前，应先调用：

```json
{"container":"simjoin"}
```

对应工具为 `container_info`。重点检查 `privileged`、`caps_add`、`pid_ns`、
`network`、`devices` 和 `mounts`。容器若共享宿主机 namespace、设备或目录，
容器内操作可能直接影响宿主机，不能把“在容器里”当作充分的安全隔离。

### 人工操作

人工调用 `dexec`，可以复用同一个执行核心而不经过 MCP：

简单调用：

```bash
container-mcp exec --container simjoin 'pwd'
```

通过 stdin 执行多行 Bash 脚本：

```bash
container-mcp exec --container simjoin 'bash -s' - <<'SCRIPT'
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
uv run --group test --locked python -m pytest -q
```

不要为了测试替换正在运行的 9943 服务。可在第一个终端以前台方式启动临时
服务：

```bash
uv run --no-dev --locked \
  container-mcp --allow-container simjoin \
  --runlog-dir /tmp/container-mcp-test-runlog \
  serve --host 127.0.0.1 --port 19943
```

在第二个终端执行测试：

```bash
CONTAINER_MCP_LIVE=1 CONTAINER_MCP_TEST_CONTAINER=simjoin \
CONTAINER_MCP_URL=http://127.0.0.1:19943/mcp \
  uv run --group test --locked \
  python -m pytest -q tests/test_live_patch.py
```

测试结束后回到第一个终端按 `Ctrl-C` 停止已确认的前台进程。

服务运行后，Streamable HTTP 集成测试会并发初始化四个客户端，并通过服务
验证 stdin、进度、自然 124、超时和取消后的进程清理：

```bash
CONTAINER_MCP_LIVE=1 CONTAINER_MCP_TEST_CONTAINER=simjoin \
  uv run --group test --locked \
  python -m pytest -q tests/test_live_mcp.py
```
