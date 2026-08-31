from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server

from . import dexec, server as runtime


def _container_argument(value: str) -> str:
    try:
        return runtime.validate_container(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the container MCP server or execute a container command manually."
    )
    parser.add_argument(
        "--allow-container",
        action="append",
        default=[],
        type=_container_argument,
        metavar="NAME",
        help="Container name or id tools may access; repeat to allow multiple containers.",
    )
    parser.add_argument(
        "--runlog-dir",
        default=str(runtime.DEFAULT_RUNLOG_DIR),
        help=f"RUNLOG directory (default: {runtime.DEFAULT_RUNLOG_DIR}).",
    )
    parser.add_argument(
        "--socket-path",
        default=str(runtime.SERVICE_SOCKET_PATH),
        help=f"Unix socket path (default: {runtime.SERVICE_SOCKET_PATH}).",
    )
    parser.add_argument("--managed-service", action="store_true", help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="mode")
    exec_parser = subparsers.add_parser(
        "exec",
        help="Execute one command manually instead of starting the MCP server.",
    )
    exec_parser.add_argument(
        "--timeout-sec",
        default=120,
        type=int,
        help="Command timeout in seconds, from 1 to 3600 (default: 120).",
    )
    exec_parser.add_argument(
        "--container",
        required=True,
        type=_container_argument,
        help="Name or id of the container in which to execute the command.",
    )
    exec_parser.add_argument("command", help="Shell command to execute inside the container.")
    exec_parser.add_argument(
        "stdin_source",
        nargs="?",
        choices=("-",),
        metavar="-",
        help="Pass this process's standard input to the container command.",
    )

    subparsers.add_parser(
        "serve",
        help="Run a foreground Streamable HTTP MCP service.",
    )
    subparsers.add_parser(
        "start-service",
        help="Start a detached local service from this source checkout.",
    )
    subparsers.add_parser("stop-service", help="Stop the detached local service.")
    subparsers.add_parser("status-service", help="Show detached local service status.")
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _argument_parser().parse_args(argv)


def _manual_stdin(args: argparse.Namespace) -> str | None:
    return sys.stdin.read() if args.stdin_source == "-" else None


class _ManualContext:
    """Render bounded execution progress without an MCP request context."""

    async def report_progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        return None

    async def info(self, message: str, logger_name: str | None = None) -> None:
        print(message, file=sys.stderr, flush=True)


async def _relay_messages(source: Any, target: Any) -> None:
    async with target:
        async for message in source:
            if isinstance(message, Exception):
                raise message
            await target.send(message)


async def _stdio_proxy(socket_path: Path) -> None:
    transport = httpx.AsyncHTTPTransport(uds=os.fspath(socket_path))
    async with httpx.AsyncClient(
        transport=transport,
        timeout=None,
        trust_env=False,
    ) as client:
        async with stdio_server() as (stdio_read, stdio_write):
            async with streamable_http_client(
                runtime.MCP_HTTP_URL,
                http_client=client,
            ) as (http_read, http_write, _):
                stdio_task = asyncio.create_task(_relay_messages(stdio_read, http_write))
                daemon_task = asyncio.create_task(_relay_messages(http_read, stdio_write))
                tasks = {stdio_task, daemon_task}
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    if stdio_task in done:
                        stdio_task.result()
                        return
                    daemon_task.result()
                    raise ConnectionError("container-mcp daemon disconnected")
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)


def run_stdio_proxy(socket_path: Path) -> None:
    asyncio.run(_stdio_proxy(socket_path))


def _manual_exit_status(exit_code: int | str) -> int:
    if isinstance(exit_code, int):
        return exit_code
    if exit_code == "timeout":
        return 124
    if exit_code == "cancelled":
        return 130
    return 1


def _read_service_pid() -> int | None:
    try:
        value = runtime.SERVICE_PID_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return int(value) if value.isdigit() and int(value) > 1 else None


def _service_process_matches(pid: int) -> bool:
    try:
        command = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return b"--managed-service" in command and b"serve" in command


def _remove_stale_socket(socket_path: Path) -> None:
    try:
        mode = socket_path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"socket path exists and is not a socket: {socket_path}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(os.fspath(socket_path))
        except ConnectionRefusedError:
            socket_path.unlink()
            return
    raise RuntimeError(f"service socket is already in use: {socket_path}")


def _wait_for_service(socket_path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"service exited during startup with code {process.returncode}")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                probe.connect(os.fspath(socket_path))
                if process.poll() is None:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"service did not listen on {socket_path} within 15 seconds")


def _start_detached_service(socket_path: Path) -> int:
    existing_pid = _read_service_pid()
    if existing_pid is not None and _service_process_matches(existing_pid):
        raise RuntimeError(f"service is already running with pid {existing_pid}")

    runtime.TMP_DIR.mkdir(parents=True, exist_ok=True)
    runtime.TMP_DIR.chmod(0o700)
    runtime.SERVICE_PID_PATH.unlink(missing_ok=True)
    _remove_stale_socket(socket_path)
    command = [
        sys.executable,
        str(runtime.PROJECT_DIR / "run.py"),
        "--runlog-dir",
        str(runtime.RUNLOG_DIR),
        "--socket-path",
        str(socket_path),
        "--managed-service",
    ]
    for container in sorted(runtime.ALLOWED_CONTAINERS):
        command.extend(("--allow-container", container))
    command.append("serve")
    with runtime.SERVICE_LOG_PATH.open("ab") as service_log:
        process = subprocess.Popen(
            command,
            cwd=runtime.PROJECT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=service_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    runtime.SERVICE_PID_PATH.write_text(f"{process.pid}\n", encoding="utf-8")
    try:
        _wait_for_service(socket_path, process)
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        runtime.SERVICE_PID_PATH.unlink(missing_ok=True)
        try:
            _remove_stale_socket(socket_path)
        except (OSError, RuntimeError):
            pass
        raise
    return process.pid


def _stop_detached_service() -> int:
    pid = _read_service_pid()
    if pid is None or not _service_process_matches(pid):
        runtime.SERVICE_PID_PATH.unlink(missing_ok=True)
        raise RuntimeError("service is not running")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while _service_process_matches(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _service_process_matches(pid):
        os.kill(pid, signal.SIGKILL)
    runtime.SERVICE_PID_PATH.unlink(missing_ok=True)
    return pid


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    runtime.ALLOWED_CONTAINERS = frozenset(args.allow_container)
    runtime.RUNLOG_DIR = runtime.configure_runlog_dir(args.runlog_dir)
    socket_path = runtime.configure_socket_path(args.socket_path)

    if args.mode == "exec":
        try:
            result, exit_code = asyncio.run(
                dexec.execute(
                    args.command,
                    timeout_sec=args.timeout_sec,
                    stdin=_manual_stdin(args),
                    container=args.container,
                    thread_id=runtime.MANUAL_RUN_ID,
                    ctx=_ManualContext(),
                    runlog_dir=runtime.RUNLOG_DIR,
                )
            )
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"container-mcp: error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        print(result)
        raise SystemExit(_manual_exit_status(exit_code))

    if args.mode in {"serve", "start-service"} and not runtime.ALLOWED_CONTAINERS:
        print(
            "container-mcp: error: at least one --allow-container is required for MCP service mode",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if args.mode == "serve":
        _remove_stale_socket(socket_path)
        runtime.run_uds_server(socket_path)
        return
    if args.mode == "start-service":
        try:
            pid = _start_detached_service(socket_path)
        except (OSError, RuntimeError) as exc:
            print(f"container-mcp: service start failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(
            f"started container-mcp on {socket_path} (pid {pid}); "
            f"log: {runtime.SERVICE_LOG_PATH}"
        )
        return
    if args.mode == "stop-service":
        try:
            pid = _stop_detached_service()
        except (OSError, RuntimeError) as exc:
            print(f"container-mcp: service stop failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(f"stopped container-mcp (pid {pid})")
        return
    if args.mode == "status-service":
        pid = _read_service_pid()
        if pid is None or not _service_process_matches(pid):
            print("container-mcp is not running")
            raise SystemExit(3)
        print(f"container-mcp is running (pid {pid})")
        return
    try:
        run_stdio_proxy(socket_path)
    except Exception as exc:
        print(f"container-mcp: proxy failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
