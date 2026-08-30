from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import dexec, server as runtime


def _container_argument(value: str) -> str:
    try:
        return runtime.validate_container(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _port_argument(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


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

    serve_parser = subparsers.add_parser(
        "serve",
        help="Run a foreground Streamable HTTP MCP service.",
    )
    serve_parser.add_argument(
        "--host",
        default=runtime.DEFAULT_HTTP_HOST,
        choices=("127.0.0.1", "localhost", "::1"),
        help="Loopback address to bind (default: 127.0.0.1).",
    )
    serve_parser.add_argument(
        "--port",
        default=runtime.DEFAULT_HTTP_PORT,
        type=_port_argument,
        help="HTTP port (default: 9943).",
    )

    start_parser = subparsers.add_parser(
        "start-service",
        help="Start a detached local service from this source checkout.",
    )
    start_parser.add_argument(
        "--port",
        default=runtime.DEFAULT_HTTP_PORT,
        type=_port_argument,
        help="HTTP port (default: 9943).",
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


def _wait_for_service(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"service exited during startup with code {process.returncode}")
        try:
            with socket.create_connection((runtime.DEFAULT_HTTP_HOST, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(
        f"service did not listen on {runtime.DEFAULT_HTTP_HOST}:{port} within 15 seconds"
    )


def _start_detached_service(port: int) -> int:
    existing_pid = _read_service_pid()
    if existing_pid is not None and _service_process_matches(existing_pid):
        raise RuntimeError(f"service is already running with pid {existing_pid}")

    runtime.TMP_DIR.mkdir(parents=True, exist_ok=True)
    runtime.SERVICE_PID_PATH.unlink(missing_ok=True)
    command = [
        sys.executable,
        str(runtime.PROJECT_DIR / "run.py"),
        "--runlog-dir",
        str(runtime.RUNLOG_DIR),
        "--managed-service",
    ]
    for container in sorted(runtime.ALLOWED_CONTAINERS):
        command.extend(("--allow-container", container))
    command.extend(("serve", "--host", runtime.DEFAULT_HTTP_HOST, "--port", str(port)))
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
        _wait_for_service(port, process)
    except Exception:
        if process.poll() is None:
            process.terminate()
        runtime.SERVICE_PID_PATH.unlink(missing_ok=True)
        raise
    return process.pid


def _stop_detached_service() -> int:
    pid = _read_service_pid()
    if pid is None or not _service_process_matches(pid):
        runtime.SERVICE_PID_PATH.unlink(missing_ok=True)
        raise RuntimeError("service is not running")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not _service_process_matches(pid):
            runtime.SERVICE_PID_PATH.unlink(missing_ok=True)
            return pid
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    runtime.SERVICE_PID_PATH.unlink(missing_ok=True)
    return pid


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    runtime.ALLOWED_CONTAINERS = frozenset(args.allow_container)
    runtime.RUNLOG_DIR = runtime.configure_runlog_dir(args.runlog_dir)

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

    if args.mode in {None, "serve", "start-service"} and not runtime.ALLOWED_CONTAINERS:
        print(
            "container-mcp: error: at least one --allow-container is required for MCP service mode",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if args.mode == "serve":
        runtime.server.settings.host = args.host
        runtime.server.settings.port = args.port
        runtime.run_http_server()
        return
    if args.mode == "start-service":
        try:
            pid = _start_detached_service(args.port)
        except (OSError, RuntimeError) as exc:
            print(f"container-mcp: service start failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(
            f"started container-mcp on {runtime.DEFAULT_HTTP_HOST}:{args.port} (pid {pid}); "
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
    runtime.server.run("stdio")
