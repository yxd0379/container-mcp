# container-mcp

`container-mcp` 是一个只从本仓库运行的 MCP 服务，让 Codex 访问白名单中的
Docker 容器。

它提供三个工具：

- `dexec`：按 Codex 对话 thread ID 将命令和完整输出落盘，便于审计。
- `dpatch`：稳定修改容器内文件，降低 Codex 直接使用 Docker 命令进行 patch 的
  失败率。
- `dinspect`：按需提炼 `docker inspect` 信息，提供更精细、更节省 token 的容器
  状态查询。

## 启动

依赖：宿主机需要 `uv`、Docker CLI 和 Docker 权限；目标容器需要 Bash、GNU
`timeout` 与 coreutils。

建议先切到源码仓库，让启动进程的 cwd 固定为仓库根目录：

```bash
cd /home/yuxd/repos/container-mcp

uv run --no-dev --locked python run.py \
    --allow-container simjoin \
    --allow-container oprace \
    --runlog-dir ./tmp/RUNLOG \
    start-service

# 或在其他目录启动（展示可移植性）
 uv run --directory /home/yuxd/repos/container-mcp \
    --no-dev --locked python run.py \
    --allow-container simjoin \
    --runlog-dir ./tmp/RUNLOG \
    start-service
```

```bash
uv run --no-dev --locked python run.py stop-service
uv run --no-dev --locked python run.py status-service
```

后台服务日志和 PID 默认写入源码仓库的 `tmp/`，文件分别是
`container-mcp.log` 和 `container-mcp.pid`；socket 是 `container-mcp.sock`，
权限为 `0600`。`dexec` 完整输出由 `--runlog-dir` 指定，相对路径按源码仓库解析。

需要以前台方式调试时：

```bash
uv run --no-dev --locked python run.py \
  --allow-container simjoin \
  --socket-path ./tmp/container-mcp-dev.sock \
  serve
```

## 架构

```text
+--------------------------+          +--------------------------+
| Codex client A           |          | Codex client B           |
|                          |          |                          |
|  Codex                   |          |  Codex                   |
|    | MCP stdio           |          |    | MCP stdio           |
|    v                     |          |    v                     |
|  CLI proxy process       |          |  CLI proxy process       |
|  (same run.py program)   |          |  (same run.py program)   |
+------------+-------------+          +-------------+------------+
             |                                      |
             +------------------+-------------------+
                                |
                         HTTP over UDS
                                |
                                v
+----------------------------------------------------------------+
| container-mcp daemon                                           |
|                                                                |
|  +----------------------------------------------------------+  |
|  | Unix socket: tmp/container-mcp.sock (mode 0600)          |  |
|  +----------------------------+-----------------------------+  |
|                               |                                |
|                               v                                |
|  +----------------------------------------------------------+  |
|  | FastMCP server with Uvicorn                              |  |
|  |                                                          |  |
|  |  +---------+        +---------+        +----------+      |  |
|  |  | dexec   |        | dpatch  |        | dinspect |      |  |
|  |  +---------+        +---------+        +----------+      |  |
|  +----------------------------+-----------------------------+  |
|                               |                                |
+-------------------------------+--------------------------------+
                                |
                                v
                    +-----------+------------+
                    | Allowlisted containers |
                    +------------------------+
```

每个 Codex 客户端启动一个相同 `run.py` 程序的 CLI 代理进程；这些进程通过源码
目录中的 Unix socket 共享同一个后台服务，访问权限由 Linux 文件权限控制。

## Codex 配置

消费项目的 `.codex/config.toml`：

```toml
[mcp_servers.container]
command = "uv"
args = ["run", "--no-dev", "--locked", "python", "run.py"]
cwd = "/home/yuxd/repos/container-mcp"
startup_timeout_sec = 60
tool_timeout_sec = 3700
default_tools_approval_mode = "prompt"
```

## MCP 接口

- `dinspect(container)`：读取运行中容器的权限、namespace、设备、挂载、
  网络和资源限制。
- `dexec(command, container, timeout_sec?, stdin?)`：执行命令，返回有限预览，并将
  完整命令和输出写入按 Codex thread ID 区分的 RUNLOG。
- `dpatch(patch, container)`：用 Codex patch 格式修改容器内绝对路径。

每次调用都必须传 `container`，且目标必须在启动白名单中。Patch 会先预检全部
hunk；同一容器的 patch 串行执行，但不提供多文件回滚，也不阻止 `dexec` 或容器
内其他进程同时修改文件。

## 人工执行与测试

```bash
uv run --no-dev --locked python run.py \
  exec --container simjoin 'pwd'

uv run --group test --locked python -m pytest -q
```

传递多行 stdin 时，在命令后加 `-`：

```bash
uv run --no-dev --locked python run.py \
  exec --container simjoin 'bash -s' - < script.sh
```

另一个终端运行：

```bash
test_dir=/tmp/container-mcp-test.XXXXXX  # 使用上一个终端输出的真实路径
CONTAINER_MCP_LIVE=1 \
CONTAINER_MCP_TEST_CONTAINER=simjoin \
CONTAINER_MCP_TEST_SOCKET="$test_dir/container-mcp.sock" \
CONTAINER_MCP_TEST_RUNLOG_DIR="$test_dir/RUNLOG" \
  uv run --group test --locked python -m pytest -q tests/test_live.py
```
