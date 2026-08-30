# container-mcp

`container-mcp` 是一个只从本仓库运行的 MCP 服务，让 Codex 访问白名单中的
Docker 容器。服务监听 `http://127.0.0.1:9943/mcp`，多个 Codex 项目可以共享。

依赖：宿主机需要 `uv`、Docker CLI 和 Docker 权限；目标容器需要 Bash、GNU
`timeout` 与 coreutils。

## 启动

建议先切到源码仓库，让启动进程的 cwd 固定为仓库根目录：

```bash
cd /home/yuxd/repos/container-mcp

uv run --no-dev --locked python run.py \
  --allow-container simjoin \
  --allow-container oprace \
  --runlog-dir ./tmp/RUNLOG \
  start-service --port 9943

# 或在其他目录启动（展示可移植性）
 uv run --directory /home/yuxd/repos/container-mcp \
    --no-dev --locked python run.py \
    --allow-container simjoin \
    --runlog-dir ./tmp/RUNLOG \
    start-service --port 9943
```

```bash
uv run --no-dev --locked python run.py stop-service
uv run --no-dev --locked python run.py status-service
```

后台服务日志和 PID 固定写入源码仓库的 `tmp/`，文件分别是
`container-mcp.log` 和 `container-mcp.pid`。`dexec` 完整输出由
`--runlog-dir` 指定，相对路径按源码仓库解析。

需要以前台方式调试时：

```bash
uv run --no-dev --locked python run.py \
  --allow-container simjoin \
  serve --host 127.0.0.1 --port 19943
```

## Codex 配置

消费项目的 `.codex/config.toml`：

```toml
[mcp_servers.container]
url = "http://127.0.0.1:9943/mcp"
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

真实 MCP/Docker 集成测试使用临时前台端口：

```bash
CONTAINER_MCP_LIVE=1 CONTAINER_MCP_TEST_CONTAINER=simjoin \
CONTAINER_MCP_URL=http://127.0.0.1:19943/mcp \
  uv run --group test --locked python -m pytest -q tests/test_live.py
```
