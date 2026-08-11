from __future__ import annotations

import argparse
import asyncio
import codecs
import fcntl
import getpass
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

import anyio
import mcp.types as mcp_types
from mcp.server.fastmcp import Context, FastMCP

from .patch_engine import FileOperation, PatchError, apply_update, parse_patch


PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT_CANDIDATE = PACKAGE_DIR.parents[1]
SOURCE_ROOT = SOURCE_ROOT_CANDIDATE if (SOURCE_ROOT_CANDIDATE / "pyproject.toml").is_file() else None
PROJECT_DIR = SOURCE_ROOT or PACKAGE_DIR
WORKING_DIR = Path.cwd()
SOURCE_CHECKOUT = SOURCE_ROOT is not None


def _default_state_dir() -> Path:
    configured = os.environ.get("CONTAINER_MCP_STATE_DIR")
    if configured:
        path = Path(configured).expanduser()
        return (WORKING_DIR / path).resolve() if not path.is_absolute() else path.resolve()
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home.expanduser().resolve() / "container-mcp"


SERVICE_STATE_DIR = _default_state_dir()
DEFAULT_RUNLOG_DIR = PROJECT_DIR / "RUNLOG" if SOURCE_CHECKOUT else SERVICE_STATE_DIR / "RUNLOG"
RUNLOG_DIR = DEFAULT_RUNLOG_DIR
ALLOWED_CONTAINERS: frozenset[str] | None = None
CODEX_THREAD_ID_META_KEY = "threadId"
MANUAL_RUN_ID = "manual"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 9943
DEFAULT_SERVICE_NAME = "container-mcp.service"
# Stable filenames let source and uv-tool entry points manage the same singleton.
SERVICE_PID_PATH = SERVICE_STATE_DIR / "container-mcp.pid"
SERVICE_LOG_PATH = SERVICE_STATE_DIR / "container-mcp.log"
MAX_RETURN_CHARS = 60_000
PROGRESS_INTERVAL_SEC = 1.0
PROGRESS_TAIL_CHARS = 1_000
STDIN_LOG_CHAR_LIMIT = 200_000
IO_CHUNK_SIZE = 64 * 1024
MAX_FILTERED_LINE_BYTES = 4 * 1024
CONTAINER_KILL_AFTER_SEC = 5
HOST_TIMEOUT_OVERHEAD_SEC = CONTAINER_KILL_AFTER_SEC + 3
PROCESS_TERMINATE_GRACE_SEC = 5
CONTAINER_CLEANUP_TIMEOUT_SEC = 8
STDIN_CLOSE_TIMEOUT_SEC = 5
PATCH_COMMAND_TIMEOUT_SEC = 30
CONTAINER_INFO_TIMEOUT_SEC = 10
PATCH_MISSING_EXIT_CODE = 44
PATCH_DIRECTORY_EXIT_CODE = 45
_BASH_JOB_CONTROL_NOISE = (
    re.compile(r"^bash: cannot set terminal process group .*: Inappropriate ioctl for device$"),
    re.compile(r"^bash: no job control in this shell$"),
    re.compile(r"^bash: \[\d+: \d+ \(\d+\)\] tcsetattr: Inappropriate ioctl for device$"),
)
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_LOG_LEVEL_ORDER = {
    "debug": 0,
    "info": 1,
    "notice": 2,
    "warning": 3,
    "error": 4,
    "critical": 5,
    "alert": 6,
    "emergency": 7,
}
_mcp_log_level = "info"
_CONTAINER_WRAPPER = """
pidfile=$1
command=$2
marker=$3
printf '%s\\n' "$$" > "$pidfile"
bash -ic "$command"
status=$?
printf '\\n%s%d\\n' "$marker" "$status" >&2
rm -f -- "$pidfile"
exit "$status"
""".strip()
_CONTAINER_CLEANUP = """
pidfile=$1
attempts=$2
while [ ! -r "$pidfile" ] && [ "$attempts" -gt 0 ]; do
    sleep 0.05
    attempts=$((attempts - 1))
done
if [ -r "$pidfile" ]; then
    printf 'found\\n'
    IFS= read -r pid < "$pidfile"
    case "$pid" in
        ''|*[!0-9]*) ;;
        *)
            kill_tree() {
                local parent=$1
                local children child
                children=$(ps -eo pid=,ppid= | awk -v parent="$parent" '$2 == parent { print $1 }')
                for child in $children; do
                    kill_tree "$child"
                done
                kill -KILL -- "$parent" 2>/dev/null || true
            }
            kill_tree "$pid"
            kill -KILL -- "-$pid" 2>/dev/null || true
            ;;
    esac
else
    printf 'absent\\n'
fi
rm -f -- "$pidfile"
""".strip()
_PATCH_READ = """
export LC_ALL=C
path=$1
if [ -d "$path" ]; then
    printf 'path is a directory: %s\n' "$path" >&2
    exit 45
fi
if [ ! -e "$path" ] && [ ! -L "$path" ]; then
    exit 44
fi
cat -- "$path"
""".strip()
_PATCH_WRITE = """
set -eu
export LC_ALL=C
path=$1
expected_size=$2
parent=${path%/*}
if [ -z "$parent" ]; then
    parent=/
fi
if [ -d "$path" ]; then
    printf 'path is a directory: %s\n' "$path" >&2
    exit 45
fi
mkdir -p -- "$parent"
tmp=$(mktemp "$parent/.dpatch.XXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
cat > "$tmp"
actual_size=$(wc -c < "$tmp")
if [ "$actual_size" -ne "$expected_size" ]; then
    printf 'incomplete stdin: expected %s bytes, received %s\n' "$expected_size" "$actual_size" >&2
    exit 46
fi
if [ -e "$path" ] || [ -L "$path" ]; then
    chmod --reference="$path" "$tmp"
else
    chmod 0644 "$tmp"
fi
mv -fT -- "$tmp" "$path"
trap - EXIT HUP INT TERM
""".strip()
_PATCH_DELETE = """
set -eu
export LC_ALL=C
path=$1
if [ -d "$path" ]; then
    printf 'path is a directory: %s\n' "$path" >&2
    exit 45
fi
rm -- "$path"
""".strip()
_CONTAINER_INSPECT_FORMAT = """
name={{.Name}}
image={{.Config.Image}}
status={{.State.Status}}
user={{.Config.User}}
cwd={{.Config.WorkingDir}}
cmd={{json .Config.Cmd}}

runtime={{.HostConfig.Runtime}}
privileged={{.HostConfig.Privileged}}
caps_add={{json .HostConfig.CapAdd}}
caps_drop={{json .HostConfig.CapDrop}}
security={{json .HostConfig.SecurityOpt}}
apparmor={{json .AppArmorProfile}}
readonly_rootfs={{.HostConfig.ReadonlyRootfs}}

network={{.HostConfig.NetworkMode}}
pid_ns={{.HostConfig.PidMode}}
ipc_ns={{.HostConfig.IpcMode}}
uts_ns={{.HostConfig.UTSMode}}
user_ns={{.HostConfig.UsernsMode}}
cgroup_ns={{.HostConfig.CgroupnsMode}}

devices={{json .HostConfig.Devices}}
device_rules={{json .HostConfig.DeviceCgroupRules}}
device_requests={{json .HostConfig.DeviceRequests}}
mounts={{json .Mounts}}

ports={{json .Config.ExposedPorts}}
port_bindings={{json .HostConfig.PortBindings}}
publish_all_ports={{.HostConfig.PublishAllPorts}}
extra_hosts={{json .HostConfig.ExtraHosts}}
dns={{json .HostConfig.Dns}}

pids_limit={{json .HostConfig.PidsLimit}}
oom_kill_disable={{json .HostConfig.OomKillDisable}}
ulimits={{json .HostConfig.Ulimits}}
""".strip()
_patch_locks: dict[str, asyncio.Lock] = {}


server = FastMCP(
    "container-mcp",
    instructions=(
        "Run commands inside an explicitly selected already-running container. "
        "Every tool call must name its target container. "
        "The Codex thread id is read automatically from each MCP request's metadata; "
        "do not pass it as a tool argument. Commands and complete output are persisted "
        "under the configured RUNLOG directory. Text stdin is supported through the "
        "optional stdin argument. The apply_patch tool applies Codex-style patches "
        "to absolute paths inside the selected container. Use container_info before "
        "potentially risky operations and evaluate privileged mode, capabilities, "
        "host or shared namespaces, devices, and host mounts. Never assume container "
        "isolation protects the host; avoid actions that could affect host processes, "
        "devices, or filesystems."
    ),
)


@server._mcp_server.set_logging_level()
async def _set_logging_level(level: mcp_types.LoggingLevel) -> None:
    global _mcp_log_level
    _mcp_log_level = str(level)


class _TextCapture:
    """Incrementally decode a byte stream to disk with bounded in-memory views."""

    def __init__(self, path: Path, preview_limit: int = MAX_RETURN_CHARS) -> None:
        self.path = path
        self._file = path.open("w", encoding="utf-8", errors="replace")
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._preview_parts: list[str] = []
        self._preview_chars = 0
        self._preview_limit = preview_limit
        self._tail = ""
        self.total_chars = 0
        self.total_bytes = 0
        self._finished = False

    def feed(self, data: bytes) -> None:
        if self._finished:
            raise RuntimeError("cannot write to a finished capture")
        self.total_bytes += len(data)
        self._write_text(self._decoder.decode(data))

    def finish(self) -> None:
        if self._finished:
            return
        self._write_text(self._decoder.decode(b"", final=True))
        self._file.close()
        self._finished = True

    def _write_text(self, text: str) -> None:
        if not text:
            return
        self._file.write(text)
        self.total_chars += len(text)

        remaining = self._preview_limit - self._preview_chars
        if remaining > 0:
            preview = text[:remaining]
            self._preview_parts.append(preview)
            self._preview_chars += len(preview)

        if len(text) >= PROGRESS_TAIL_CHARS:
            self._tail = text[-PROGRESS_TAIL_CHARS:]
        else:
            self._tail = (self._tail + text)[-PROGRESS_TAIL_CHARS:]

    @property
    def tail(self) -> str:
        return self._tail

    def result_text(self) -> str:
        value = "".join(self._preview_parts)
        omitted = self.total_chars - self._preview_chars
        if omitted > 0:
            value += f"\n...[truncated {omitted} chars; complete output is in RUNLOG]"
        return value


class _StderrFilter:
    """Filter known control lines without buffering unbounded stderr lines."""

    def __init__(self, capture: _TextCapture, completion_marker: str | None = None) -> None:
        self._capture = capture
        self._completion_marker = completion_marker
        self._pending = bytearray()
        self._passthrough_until_newline = False
        self._deferred_blank: bytes | None = None
        self.completion_exit_code: int | None = None

    def feed(self, data: bytes) -> None:
        while data:
            if self._passthrough_until_newline:
                self._flush_deferred_blank()
                newline = data.find(b"\n")
                if newline < 0:
                    self._capture.feed(data)
                    return
                self._capture.feed(data[: newline + 1])
                data = data[newline + 1 :]
                self._passthrough_until_newline = False
                continue

            self._pending.extend(data)
            data = b""
            while True:
                newline = self._pending.find(b"\n")
                if newline < 0:
                    break
                line = bytes(self._pending[: newline + 1])
                del self._pending[: newline + 1]
                self._emit_line(line)

            if len(self._pending) > MAX_FILTERED_LINE_BYTES:
                self._flush_deferred_blank()
                self._capture.feed(bytes(self._pending))
                self._pending.clear()
                self._passthrough_until_newline = True

    def finish(self) -> None:
        if self._pending:
            self._emit_line(bytes(self._pending))
            self._pending.clear()
        self._flush_deferred_blank()

    def _emit_line(self, line: bytes) -> None:
        text = line.rstrip(b"\r\n").decode("utf-8", errors="replace")
        if self._completion_marker and text.startswith(self._completion_marker):
            status = text[len(self._completion_marker) :]
            if status.isdigit() and 0 <= int(status) <= 255:
                self.completion_exit_code = int(status)
                self._deferred_blank = None
                return
        if not text:
            self._flush_deferred_blank()
            self._deferred_blank = line
            return
        self._flush_deferred_blank()
        if not any(pattern.fullmatch(text) for pattern in _BASH_JOB_CONTROL_NOISE):
            self._capture.feed(line)

    def _flush_deferred_blank(self) -> None:
        if self._deferred_blank is not None:
            self._capture.feed(self._deferred_blank)
            self._deferred_blank = None


class _ProgressFiles:
    """Read bounded live views from files that the child writes directly."""

    def __init__(self, stdout_path: Path, stderr_path: Path, completion_marker: str) -> None:
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.completion_marker = completion_marker
        self._stdout_size = 0
        self._stderr_size = 0
        self._latest_tail = ""

    def snapshot(self) -> tuple[int, int, str]:
        stdout_size = self._file_size(self.stdout_path)
        stderr_size = self._file_size(self.stderr_path)
        if stdout_size != self._stdout_size:
            latest = self._read_tail(self.stdout_path, stdout_size)
            if latest.strip():
                self._latest_tail = latest
        if stderr_size != self._stderr_size:
            latest = self._read_tail(self.stderr_path, stderr_size)
            filtered = "\n".join(
                line for line in latest.splitlines() if not line.startswith(self.completion_marker)
            )
            if filtered.strip():
                self._latest_tail = filtered
        self._stdout_size = stdout_size
        self._stderr_size = stderr_size
        return stdout_size, stderr_size, self._latest_tail

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    @staticmethod
    def _read_tail(path: Path, size: int) -> str:
        if size == 0:
            return ""
        with path.open("rb") as source:
            source.seek(max(0, size - PROGRESS_TAIL_CHARS * 4))
            return source.read(PROGRESS_TAIL_CHARS * 4).decode("utf-8", errors="replace")[-PROGRESS_TAIL_CHARS:]


def _yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _safe_thread_id(thread_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", thread_id)


def _codex_thread_id(ctx: Context | None) -> str:
    """Read the request-scoped Codex thread id without trusting process environment."""
    try:
        meta = ctx.request_context.meta if ctx is not None else None
    except (AttributeError, ValueError):
        meta = None
    raw_thread_id = getattr(meta, CODEX_THREAD_ID_META_KEY, None) if meta is not None else None
    if not isinstance(raw_thread_id, str) or not raw_thread_id.strip():
        raise RuntimeError(
            "MCP request is missing _meta.threadId. Use a current Codex client; "
            "the thread id cannot be inferred safely from the MCP process environment."
        )
    candidate = raw_thread_id.strip()
    try:
        parsed = uuid.UUID(candidate)
    except (AttributeError, ValueError) as exc:
        raise RuntimeError("MCP request _meta.threadId is not a valid UUID") from exc
    canonical = str(parsed)
    if candidate.lower() != canonical:
        raise RuntimeError("MCP request _meta.threadId is not a canonical UUID")
    return canonical


def _validate_stdin(stdin: str | None) -> None:
    if stdin is None:
        return
    try:
        for offset in range(0, len(stdin), IO_CHUNK_SIZE):
            stdin[offset : offset + IO_CHUNK_SIZE].encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("stdin must contain valid UTF-8 text without isolated surrogate code points") from exc


def _log_file(thread_id: str) -> Path:
    RUNLOG_DIR.mkdir(parents=True, exist_ok=True)
    date_id = datetime.now().strftime("%y%m%d")
    return RUNLOG_DIR / f"{date_id}_{_safe_thread_id(thread_id)}.log"


def _codex_thread_metadata(thread_id: str) -> tuple[str, str]:
    state_db = Path.home() / ".codex" / "state_5.sqlite"
    if not state_db.is_file():
        return "", ""

    try:
        with sqlite3.connect(state_db) as connection:
            row = connection.execute(
                "select title, rollout_path from threads where id = ? limit 1",
                (thread_id,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return "", ""

    if row is None:
        return "", ""
    return str(row[0] or ""), str(row[1] or "")


def _write_log_header(fh: TextIO, thread_id: str) -> None:
    codex_title, codex_rollout_path = _codex_thread_metadata(thread_id)
    fh.write("---\n")
    fh.write(f"date: {_yaml_quote(datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z'))}\n")
    fh.write(f"host-cwd: {_yaml_quote(str(WORKING_DIR))}\n")
    fh.write(f"codex-thread-id: {_yaml_quote(thread_id)}\n")
    if codex_title:
        fh.write(f"codex-title: {_yaml_quote(codex_title)}\n")
    if codex_rollout_path:
        fh.write(f"codex-rollout-path: {_yaml_quote(codex_rollout_path)}\n")
    fh.write("---\n\n")


def _copy_capture(fh: TextIO, label: str, capture: _TextCapture) -> None:
    if capture.total_chars == 0:
        return
    fh.write(f"```{label}\n")
    with capture.path.open("r", encoding="utf-8", errors="replace") as source:
        shutil.copyfileobj(source, fh, length=IO_CHUNK_SIZE)
    with capture.path.open("rb") as source:
        source.seek(-1, os.SEEK_END)
        ends_with_newline = source.read(1) == b"\n"
    if not ends_with_newline:
        fh.write("\n")
    fh.write("```\n")


def _stdin_byte_count(stdin: str) -> int:
    return sum(
        len(stdin[offset : offset + IO_CHUNK_SIZE].encode("utf-8"))
        for offset in range(0, len(stdin), IO_CHUNK_SIZE)
    )


def _append_log(
    *,
    container: str,
    command: str,
    stdin: str | None,
    stdout: _TextCapture,
    stderr: _TextCapture,
    start: datetime,
    end: datetime,
    exit_code: int | str,
    thread_id: str,
) -> None:
    duration_sec = int((end - start).total_seconds())
    path = _log_file(thread_id)
    with path.open("a+", encoding="utf-8", errors="replace") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            _write_log_header(fh, thread_id)
        fh.write("--------------------------------------------------------------------------------\n")
        fh.write(
            "time: "
            f"{start.astimezone().strftime('%Y-%m-%d %H:%M:%S %z')} -> "
            f"{end.astimezone().strftime('%Y-%m-%d %H:%M:%S %z')} "
            f"({duration_sec}s), exit-code: {exit_code}\n"
        )
        fh.write(f"container: {container}\n")
        fh.write(f"executor: {_yaml_quote(f'docker exec {container} timeout ... bash -ic')}\n")
        fh.write("```bash\n")
        fh.write(command)
        if not command.endswith("\n"):
            fh.write("\n")
        fh.write("```\n")
        if stdin is not None:
            stdin_bytes = _stdin_byte_count(stdin)
            if not stdin:
                fh.write("stdin: 0 bytes\n")
            elif len(stdin) <= STDIN_LOG_CHAR_LIMIT and all(char.isprintable() or char.isspace() for char in stdin):
                fh.write("```stdin\n")
                fh.write(stdin)
                if not stdin.endswith("\n"):
                    fh.write("\n")
                fh.write("```\n")
            else:
                fh.write(f"stdin: {stdin_bytes} bytes (not embedded in log)\n")
        _copy_capture(fh, "stdout", stdout)
        _copy_capture(fh, "stderr", stderr)
        fh.write("\n")
        fh.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _decode_outputs(
    raw_stdout_path: Path,
    raw_stderr_path: Path,
    temp_path: Path,
    completion_marker: str,
) -> tuple[_TextCapture, _TextCapture, int | None]:
    stdout = _TextCapture(temp_path / "stdout.txt")
    stderr = _TextCapture(temp_path / "stderr.txt")
    stderr_filter = _StderrFilter(stderr, completion_marker)
    try:
        with raw_stdout_path.open("rb") as source:
            while data := source.read(IO_CHUNK_SIZE):
                stdout.feed(data)
        with raw_stderr_path.open("rb") as source:
            while data := source.read(IO_CHUNK_SIZE):
                stderr_filter.feed(data)
        stderr_filter.finish()
        return stdout, stderr, stderr_filter.completion_exit_code
    except Exception:
        stdout.finish()
        stderr.finish()
        raise


def _format_result(exit_code: int | str, stdout: _TextCapture, stderr: _TextCapture) -> str:
    status = "ok" if exit_code == 0 else "failed"
    sections = [f"status: {status}", f"exit_code: {exit_code}"]
    stdout_text = stdout.result_text()
    stderr_text = stderr.result_text()
    if stdout_text:
        sections.extend(("stdout:", stdout_text.rstrip("\n")))
    if stderr_text:
        sections.extend(("stderr:", stderr_text.rstrip("\n")))
    return "\n".join(sections)


async def _feed_stdin(writer: asyncio.StreamWriter, stdin: str) -> None:
    try:
        for offset in range(0, len(stdin), IO_CHUNK_SIZE):
            writer.write(stdin[offset : offset + IO_CHUNK_SIZE].encode("utf-8"))
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _progress_message(
    thread_id: str,
    started: datetime,
    progress_files: _ProgressFiles,
    state: str,
) -> tuple[float, str]:
    elapsed = max(0.0, (datetime.now().astimezone() - started).total_seconds())
    stdout_bytes, stderr_bytes, latest = progress_files.snapshot()
    prefix = _safe_thread_id(thread_id)[-12:] or "unknown"
    summary = (
        f"[dexec:{prefix}] {state} {elapsed:.1f}s; "
        f"stdout={stdout_bytes}B stderr={stderr_bytes}B"
    )
    if latest:
        latest = _ANSI_ESCAPE.sub("", latest).replace("\r", "\n")
        latest_line = next((line.strip() for line in reversed(latest.splitlines()) if line.strip()), "")
        if latest_line:
            summary += f"; latest: {latest_line[-500:]}"
    return elapsed, summary


async def _notify_progress(
    ctx: Context | None,
    thread_id: str,
    started: datetime,
    progress_files: _ProgressFiles,
    state: str,
) -> None:
    if ctx is None:
        return
    elapsed, message = _progress_message(thread_id, started, progress_files, state)
    try:
        await ctx.report_progress(elapsed, message=message)
    except Exception:
        pass
    if _LOG_LEVEL_ORDER["info"] >= _LOG_LEVEL_ORDER.get(_mcp_log_level, 1):
        try:
            await ctx.info(message, logger_name="container-mcp")
        except Exception:
            pass


async def _progress_loop(
    stop: asyncio.Event,
    ctx: Context | None,
    thread_id: str,
    started: datetime,
    progress_files: _ProgressFiles,
) -> None:
    await _notify_progress(ctx, thread_id, started, progress_files, "running")
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=PROGRESS_INTERVAL_SEC)
            return
        except asyncio.TimeoutError:
            await _notify_progress(ctx, thread_id, started, progress_files, "running")


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_TERMINATE_GRACE_SEC)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _cleanup_container_group(
    container: str,
    pidfile: str,
    wait_attempts: int = 0,
) -> tuple[str, bool]:
    """Kill the command process group from inside the container and remove its pidfile."""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            container,
            "bash",
            "-c",
            _CONTAINER_CLEANUP,
            "dexec-cleanup",
            pidfile,
            str(wait_attempts),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKING_DIR,
        )
    except OSError as exc:
        return f"Container cleanup could not start: {exc}", False
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=CONTAINER_CLEANUP_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        await _terminate_process(process)
        return "Container cleanup command timed out.", False
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        return f"Container cleanup failed with exit code {process.returncode}: {detail}", False
    found = stdout.decode("utf-8", errors="replace").strip() == "found"
    return "", found


async def _finish_stdin_task(task: asyncio.Task[None] | None) -> str:
    if task is None:
        return ""
    if not task.done():
        task.cancel()
    try:
        results = await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True),
            timeout=STDIN_CLOSE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        return "Closing command stdin timed out."
    result = results[0]
    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
        return f"Writing command stdin failed: {type(result).__name__}: {result}"
    return ""


async def _execute(
    command: str,
    timeout_sec: int = 120,
    stdin: str | None = None,
    *,
    container: str,
    thread_id: str,
    ctx: Context | None = None,
) -> tuple[str, int | str]:
    if not command.strip():
        raise ValueError("command must not be empty")
    if timeout_sec < 1 or timeout_sec > 3600:
        raise ValueError("timeout_sec must be between 1 and 3600")
    _validate_stdin(stdin)

    start = datetime.now().astimezone()
    invocation_id = secrets.token_hex(16)
    completion_marker = f"__CONTAINER_MCP_COMPLETE_{invocation_id}__="
    pidfile = f"/tmp/container-mcp-{invocation_id}.pid"
    guarded_command = "trap 'exit 143' TERM\n" + command
    temp_path = Path(tempfile.mkdtemp(prefix="container-mcp-"))
    raw_stdout_path = temp_path / "stdout.raw"
    raw_stderr_path = temp_path / "stderr.raw"
    raw_stdout_path.touch()
    raw_stderr_path.touch()
    progress_files = _ProgressFiles(raw_stdout_path, raw_stderr_path, completion_marker)
    stop_progress = asyncio.Event()
    process: asyncio.subprocess.Process | None = None
    stdin_task: asyncio.Task[None] | None = None
    progress_task: asyncio.Task[None] | None = None
    raw_return_code: int | None = None
    host_timed_out = False
    cancelled_error: asyncio.CancelledError | None = None
    pending_error: Exception | None = None
    diagnostics: list[str] = []
    stdout: _TextCapture | None = None
    stderr: _TextCapture | None = None
    exit_code: int | str = "error"

    try:
        args = ["docker", "exec"]
        if stdin is not None:
            args.append("-i")
        args.extend(
            (
                container,
                "timeout",
                "--signal=TERM",
                f"--kill-after={CONTAINER_KILL_AFTER_SEC}s",
                f"{timeout_sec}s",
                "bash",
                "-c",
                _CONTAINER_WRAPPER,
                "dexec-wrapper",
                pidfile,
                guarded_command,
                completion_marker,
            )
        )

        raw_stdout_file = raw_stdout_path.open("wb", buffering=0)
        raw_stderr_file = raw_stderr_path.open("wb", buffering=0)
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=raw_stdout_file,
                stderr=raw_stderr_file,
                cwd=WORKING_DIR,
            )
        finally:
            raw_stdout_file.close()
            raw_stderr_file.close()

        if stdin is not None:
            assert process.stdin is not None
            stdin_task = asyncio.create_task(_feed_stdin(process.stdin, stdin))
        progress_task = asyncio.create_task(
            _progress_loop(stop_progress, ctx, thread_id, start, progress_files)
        )
        try:
            raw_return_code = await asyncio.wait_for(
                process.wait(),
                timeout=timeout_sec + HOST_TIMEOUT_OVERHEAD_SEC,
            )
        except asyncio.TimeoutError:
            host_timed_out = True
    except asyncio.CancelledError as exc:
        cancelled_error = exc
    except OSError as exc:
        diagnostics.append(f"{exc}\nIs Docker installed and available on PATH?")
    except Exception as exc:
        pending_error = exc
        diagnostics.append(f"Internal dexec error: {type(exc).__name__}: {exc}")
    finally:
        # AnyIO uses level cancellation. Shield the complete kill, reap, audit, and
        # temporary-file cleanup sequence so cancellation cannot leak a command.
        with anyio.CancelScope(shield=True):
            needs_container_cleanup = cancelled_error is not None or host_timed_out or pending_error is not None
            container_group_found = False
            if needs_container_cleanup:
                wait_attempts = 20 if process is not None and not host_timed_out else 0
                cleanup_error, container_group_found = await _cleanup_container_group(
                    container,
                    pidfile,
                    wait_attempts,
                )
                if cleanup_error:
                    diagnostics.append(cleanup_error)
            if process is not None and process.returncode is None:
                await _terminate_process(process)
            if needs_container_cleanup and process is not None and not container_group_found:
                cleanup_error, _ = await _cleanup_container_group(container, pidfile, 20)
                if cleanup_error:
                    diagnostics.append(cleanup_error)
            if process is not None and raw_return_code is None:
                raw_return_code = process.returncode

            stdin_error = await _finish_stdin_task(stdin_task)
            if stdin_error:
                diagnostics.append(stdin_error)
            stop_progress.set()
            if progress_task is not None:
                await asyncio.gather(progress_task, return_exceptions=True)

            try:
                stdout, stderr, completed_exit_code = await asyncio.to_thread(
                    _decode_outputs,
                    raw_stdout_path,
                    raw_stderr_path,
                    temp_path,
                    completion_marker,
                )
            except Exception:
                await asyncio.to_thread(shutil.rmtree, temp_path, True)
                raise

            if cancelled_error is not None:
                exit_code = "cancelled"
                diagnostics.append("Command cancelled by the MCP client.")
            elif host_timed_out:
                exit_code = "timeout"
                diagnostics.append(f"Command timed out after {timeout_sec}s and required host-side cleanup.")
            elif completed_exit_code is not None:
                exit_code = completed_exit_code
            elif raw_return_code == 124:
                exit_code = "timeout"
                cleanup_error, _ = await _cleanup_container_group(container, pidfile)
                if cleanup_error:
                    diagnostics.append(cleanup_error)
                diagnostics.append(f"Command timed out after {timeout_sec}s.")
            elif raw_return_code is not None:
                exit_code = raw_return_code

            if diagnostics:
                diagnostic_text = "\n".join(diagnostics)
                if stderr.total_chars and not stderr.tail.endswith("\n"):
                    stderr.feed(b"\n")
                stderr.feed(diagnostic_text.encode("utf-8", errors="replace"))
            stdout.finish()
            stderr.finish()

            end = datetime.now().astimezone()
            await _notify_progress(ctx, thread_id, start, progress_files, "persisting RUNLOG")
            try:
                await asyncio.to_thread(
                    _append_log,
                    container=container,
                    command=command,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    start=start,
                    end=end,
                    exit_code=exit_code,
                    thread_id=thread_id,
                )
                final_state = "cancelled" if cancelled_error is not None else "finished"
                await _notify_progress(ctx, thread_id, start, progress_files, final_state)
            finally:
                await asyncio.to_thread(shutil.rmtree, temp_path, True)

    if cancelled_error is not None:
        raise cancelled_error
    if pending_error is not None:
        raise pending_error
    assert stdout is not None
    assert stderr is not None
    return _format_result(exit_code, stdout, stderr), exit_code


@dataclass(frozen=True)
class _PatchCommandResult:
    exit_code: int
    stdout: bytes
    stderr: str


@dataclass(frozen=True)
class _PreparedPatchOperation:
    action: str
    path: str
    content: bytes | None = None
    move_path: str | None = None


async def _run_patch_shell(
    container: str,
    script: str,
    path: str,
    *,
    extra_args: tuple[str, ...] = (),
    stdin: bytes | None = None,
) -> _PatchCommandResult:
    args = ["docker", "exec"]
    if stdin is not None:
        args.append("-i")
    args.extend(
        (
            container,
            "timeout",
            "--signal=TERM",
            f"--kill-after={CONTAINER_KILL_AFTER_SEC}s",
            f"{PATCH_COMMAND_TIMEOUT_SEC}s",
            "bash",
            "-c",
            script,
            "dpatch",
            path,
            *extra_args,
        )
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKING_DIR,
        )
    except OSError as exc:
        raise PatchError(
            "container_unavailable",
            f"Could not execute Docker for container {container}: {exc}",
            path=path,
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin),
            timeout=PATCH_COMMAND_TIMEOUT_SEC + HOST_TIMEOUT_OVERHEAD_SEC,
        )
    except asyncio.CancelledError:
        if process.returncode is None:
            await _terminate_process(process)
        raise
    except asyncio.TimeoutError as exc:
        if process.returncode is None:
            await _terminate_process(process)
        raise PatchError(
            "container_timeout",
            f"Container patch operation timed out after {PATCH_COMMAND_TIMEOUT_SEC}s",
            path=path,
        ) from exc

    assert process.returncode is not None
    return _PatchCommandResult(
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr.decode("utf-8", errors="replace").strip(),
    )


def _patch_failure_message(operation: str, path: str, result: _PatchCommandResult) -> str:
    detail = result.stderr or f"container command exited with code {result.exit_code}"
    return f"Failed to {operation} {path}: {detail}"


async def _read_patch_path(container: str, path: str) -> bytes | None:
    result = await _run_patch_shell(container, _PATCH_READ, path)
    if result.exit_code == 0:
        return result.stdout
    if result.exit_code == PATCH_MISSING_EXIT_CODE:
        return None
    if result.exit_code == PATCH_DIRECTORY_EXIT_CODE:
        raise PatchError("path_is_directory", result.stderr, path=path)
    raise PatchError(
        "read_failed",
        _patch_failure_message("read", path, result),
        path=path,
    )


async def _prepare_patch(
    container: str,
    operations: tuple[FileOperation, ...],
) -> tuple[_PreparedPatchOperation, ...]:
    virtual_files: dict[str, bytes | None] = {}

    async def read(path: str) -> bytes | None:
        if path not in virtual_files:
            virtual_files[path] = await _read_patch_path(container, path)
        return virtual_files[path]

    prepared: list[_PreparedPatchOperation] = []
    for operation in operations:
        path = operation.path
        if operation.action == "add":
            await read(path)
            assert operation.contents is not None
            content = operation.contents.encode("utf-8")
            prepared.append(_PreparedPatchOperation("add", path, content=content))
            virtual_files[path] = content
            continue

        if operation.action == "delete":
            if await read(path) is None:
                raise PatchError("file_not_found", "File to delete does not exist", path=path)
            prepared.append(_PreparedPatchOperation("delete", path))
            virtual_files[path] = None
            continue

        if operation.action != "update":
            raise PatchError("invalid_patch", f"Unsupported patch action: {operation.action}")

        original = await read(path)
        if original is None:
            raise PatchError("file_not_found", "File to update does not exist", path=path)
        if operation.chunks:
            try:
                original_text = original.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PatchError(
                    "invalid_utf8",
                    "File to update is not valid UTF-8 text",
                    path=path,
                ) from exc
            new_content = apply_update(path, original_text, operation.chunks).encode("utf-8")
        else:
            new_content = original

        move_path = operation.move_path
        if move_path is not None and move_path != path:
            await read(move_path)
            prepared.append(
                _PreparedPatchOperation(
                    "move",
                    path,
                    content=new_content,
                    move_path=move_path,
                )
            )
            virtual_files[path] = None
            virtual_files[move_path] = new_content
        else:
            prepared.append(_PreparedPatchOperation("update", path, content=new_content))
            virtual_files[path] = new_content

    return tuple(prepared)


async def _write_patch_path(container: str, path: str, content: bytes) -> None:
    result = await _run_patch_shell(
        container,
        _PATCH_WRITE,
        path,
        extra_args=(str(len(content)),),
        stdin=content,
    )
    if result.exit_code == 0:
        return
    code = "path_is_directory" if result.exit_code == PATCH_DIRECTORY_EXIT_CODE else "write_failed"
    raise PatchError(code, _patch_failure_message("write", path, result), path=path)


async def _delete_patch_path(container: str, path: str) -> None:
    result = await _run_patch_shell(container, _PATCH_DELETE, path)
    if result.exit_code == 0:
        return
    code = "path_is_directory" if result.exit_code == PATCH_DIRECTORY_EXIT_CODE else "delete_failed"
    raise PatchError(code, _patch_failure_message("delete", path, result), path=path)


async def _commit_patch(
    container: str,
    operations: tuple[_PreparedPatchOperation, ...],
) -> str:
    applied: list[str] = []
    try:
        for operation in operations:
            if operation.action in {"add", "update"}:
                assert operation.content is not None
                await _write_patch_path(container, operation.path, operation.content)
                applied.append(f"{'A' if operation.action == 'add' else 'M'} {operation.path}")
            elif operation.action == "delete":
                await _delete_patch_path(container, operation.path)
                applied.append(f"D {operation.path}")
            elif operation.action == "move":
                assert operation.content is not None
                assert operation.move_path is not None
                await _write_patch_path(container, operation.move_path, operation.content)
                applied.append(f"A {operation.move_path} (move destination)")
                await _delete_patch_path(container, operation.path)
                applied[-1] = f"M {operation.path} -> {operation.move_path}"
            else:
                raise PatchError(
                    "invalid_patch",
                    f"Unsupported prepared action: {operation.action}",
                    path=operation.path,
                )
    except PatchError as exc:
        if not applied:
            raise
        completed = "\n".join(f"  {entry}" for entry in applied)
        raise PatchError(
            "partial_apply",
            f"{exc.message}\nCompleted before the failure:\n{completed}",
            path=exc.path,
        ) from exc

    return "Success. Updated the following files:\n" + "\n".join(applied)


def _patch_lock_for(container: str) -> asyncio.Lock:
    lock = _patch_locks.get(container)
    if lock is None:
        lock = asyncio.Lock()
        _patch_locks[container] = lock
    return lock


@server.tool()
async def container_info(container: str) -> str:
    """Inspect isolation-relevant metadata for an allowed, currently running container."""
    selected_container = _resolve_container(container)
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            selected_container,
            "--format",
            _CONTAINER_INSPECT_FORMAT,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKING_DIR,
        )
    except OSError as exc:
        raise RuntimeError(f"Could not inspect container {selected_container}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=CONTAINER_INFO_TIMEOUT_SEC,
        )
    except asyncio.CancelledError:
        if process.returncode is None:
            await _terminate_process(process)
        raise
    except asyncio.TimeoutError as exc:
        if process.returncode is None:
            await _terminate_process(process)
        raise RuntimeError(
            f"Inspecting container {selected_container} timed out after "
            f"{CONTAINER_INFO_TIMEOUT_SEC}s"
        ) from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Could not inspect container {selected_container}: "
            f"{detail or f'docker inspect exited with code {process.returncode}'}"
        )

    metadata = stdout.decode("utf-8", errors="replace").strip()
    status = next(
        (line.removeprefix("status=") for line in metadata.splitlines() if line.startswith("status=")),
        "",
    )
    if status != "running":
        displayed_status = status or "unknown"
        raise ValueError(
            f"container {selected_container!r} is not running (status: {displayed_status})"
        )
    return metadata


@server.tool()
async def apply_patch(patch: str, container: str) -> str:
    """Apply a Codex-style patch to absolute paths in an allowed container.

    Calls targeting the same container are serialized. Every hunk is parsed and checked
    before the first write. Each file write uses a same-directory temporary file and
    atomic rename; multi-file rollback is intentionally not provided.
    """
    selected_container = _resolve_container(container)
    async with _patch_lock_for(selected_container):
        operations = parse_patch(patch)
        prepared = await _prepare_patch(selected_container, operations)
        with anyio.CancelScope(shield=True):
            return await _commit_patch(selected_container, prepared)


@server.tool()
async def dexec(
    command: str,
    container: str,
    timeout_sec: int = 120,
    stdin: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Run a command in an allowed container, with optional text stdin and a complete audit log."""
    selected_container = _resolve_container(container)
    result, _ = await _execute(
        command,
        timeout_sec=timeout_sec,
        stdin=stdin,
        container=selected_container,
        thread_id=_codex_thread_id(ctx),
        ctx=ctx,
    )
    return result


def _configure_runlog_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = WORKING_DIR / path
    return path.resolve()


def _container_argument(value: str) -> str:
    try:
        return _validate_container(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _validate_container(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError(
            "container must be a Docker container name or id using only letters, "
            "digits, underscore, period, and hyphen"
        )
    return value


def _resolve_container(value: str) -> str:
    container = _validate_container(value)
    if not ALLOWED_CONTAINERS:
        raise RuntimeError("container-mcp has no allowed containers configured")
    if container not in ALLOWED_CONTAINERS:
        allowed = ", ".join(sorted(ALLOWED_CONTAINERS))
        raise ValueError(f"container {container!r} is not allowed; allowed containers: {allowed}")
    return container


def _port_argument(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _service_name_argument(value: str) -> str:
    name = value if value.endswith(".service") else f"{value}.service"
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", name):
        raise argparse.ArgumentTypeError("service name contains unsupported characters")
    return name


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
        default=os.environ.get("CONTAINER_MCP_RUNLOG_DIR", str(DEFAULT_RUNLOG_DIR)),
        help=f"RUNLOG directory (default: {DEFAULT_RUNLOG_DIR}).",
    )
    parser.add_argument(
        "--managed-service",
        action="store_true",
        help=argparse.SUPPRESS,
    )
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
    exec_parser.add_argument(
        "command",
        help="Shell command to execute inside the selected container.",
    )
    exec_parser.add_argument(
        "stdin_source",
        nargs="?",
        choices=("-",),
        metavar="-",
        help="Pass this process's standard input to the container command.",
    )
    serve_parser = subparsers.add_parser(
        "serve",
        help="Run a persistent Streamable HTTP MCP service.",
    )
    serve_parser.add_argument(
        "--host",
        default=DEFAULT_HTTP_HOST,
        choices=("127.0.0.1", "localhost", "::1"),
        help="Loopback address to bind (default: 127.0.0.1).",
    )
    serve_parser.add_argument(
        "--port",
        default=DEFAULT_HTTP_PORT,
        type=_port_argument,
        help="HTTP port (default: 9943).",
    )
    install_parser = subparsers.add_parser(
        "install-service",
        help="Install and start the persistent systemd user service.",
    )
    install_parser.add_argument(
        "--port",
        default=DEFAULT_HTTP_PORT,
        type=_port_argument,
        help="HTTP port (default: 9943).",
    )
    install_parser.add_argument(
        "--service-name",
        default=DEFAULT_SERVICE_NAME,
        type=_service_name_argument,
        help="systemd user unit name (default: container-mcp.service).",
    )
    install_parser.add_argument(
        "--scope",
        choices=("user", "system"),
        default="user",
        help="Install a user or system unit (default: user).",
    )
    install_parser.add_argument(
        "--service-user",
        default=os.environ.get("SUDO_USER") or getpass.getuser(),
        help="Account used by a system unit (default: invoking user).",
    )
    start_parser = subparsers.add_parser(
        "start-service",
        help="Start a detached local service without systemd.",
    )
    start_parser.add_argument(
        "--port",
        default=DEFAULT_HTTP_PORT,
        type=_port_argument,
        help="HTTP port (default: 9943).",
    )
    subparsers.add_parser(
        "stop-service",
        help="Stop the detached local service.",
    )
    subparsers.add_parser(
        "status-service",
        help="Show detached local service status.",
    )
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _argument_parser().parse_args(argv)


def _manual_stdin(args: argparse.Namespace) -> str | None:
    if args.stdin_source == "-":
        return sys.stdin.read()
    return None


class _ManualContext:
    """Render bounded execution progress without requiring an MCP request context."""

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


def _systemd_quote(value: str | Path) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _server_command_prefix() -> list[str]:
    """Invoke the server as a package so relative imports work after installation."""
    return [str(Path(sys.executable).absolute()), "-m", "container_mcp.server"]


def _systemd_unit(
    *,
    service_name: str,
    port: int,
    scope: str = "user",
    service_user: str | None = None,
) -> str:
    del service_name
    command_parts: list[str | Path] = [
        *_server_command_prefix(),
        "--runlog-dir",
        RUNLOG_DIR,
    ]
    for container in sorted(ALLOWED_CONTAINERS or ()):
        command_parts.extend(("--allow-container", container))
    command_parts.extend(("serve", "--host", DEFAULT_HTTP_HOST, "--port", str(port)))
    command = " ".join(_systemd_quote(part) for part in command_parts)
    system_user = ""
    if scope == "system":
        if not service_user or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", service_user):
            raise ValueError("system service user is invalid")
        system_user = f"User={service_user}\n"
    wanted_by = "multi-user.target" if scope == "system" else "default.target"
    after = "docker.service" if scope == "system" else "default.target"
    return (
        "[Unit]\n"
        "Description=Persistent multi-container MCP service\n"
        f"After={after}\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{system_user}"
        f"WorkingDirectory={_systemd_quote(PROJECT_DIR)}\n"
        f"ExecStart={command}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "TimeoutStopSec=15\n\n"
        "[Install]\n"
        f"WantedBy={wanted_by}\n"
    )


def _install_service(
    service_name: str,
    port: int,
    *,
    scope: str,
    service_user: str,
) -> Path:
    if scope == "system" and os.geteuid() != 0:
        raise PermissionError("system service installation must run as root")
    unit_dir = (
        Path("/etc/systemd/system")
        if scope == "system"
        else Path.home() / ".config" / "systemd" / "user"
    )
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / service_name
    unit_path.write_text(
        _systemd_unit(
            service_name=service_name,
            port=port,
            scope=scope,
            service_user=service_user,
        ),
        encoding="utf-8",
    )
    systemctl = ["systemctl"] if scope == "system" else ["systemctl", "--user"]
    subprocess.run([*systemctl, "daemon-reload"], check=True)
    subprocess.run([*systemctl, "enable", "--now", service_name], check=True)
    return unit_path


def _read_service_pid() -> int | None:
    try:
        value = SERVICE_PID_PATH.read_text(encoding="utf-8").strip()
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
            with socket.create_connection((DEFAULT_HTTP_HOST, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"service did not listen on {DEFAULT_HTTP_HOST}:{port} within 15 seconds")


def _start_detached_service(port: int) -> int:
    existing_pid = _read_service_pid()
    if existing_pid is not None and _service_process_matches(existing_pid):
        raise RuntimeError(f"service is already running with pid {existing_pid}")

    SERVICE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_PID_PATH.unlink(missing_ok=True)
    command = [
        *_server_command_prefix(),
        "--runlog-dir",
        str(RUNLOG_DIR),
        "--managed-service",
    ]
    for container in sorted(ALLOWED_CONTAINERS or ()):
        command.extend(("--allow-container", container))
    command.extend(("serve", "--host", DEFAULT_HTTP_HOST, "--port", str(port)))
    with SERVICE_LOG_PATH.open("ab", buffering=0) as service_log:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=service_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    SERVICE_PID_PATH.write_text(f"{process.pid}\n", encoding="utf-8")
    try:
        _wait_for_service(port, process)
    except Exception:
        if process.poll() is None:
            process.terminate()
        SERVICE_PID_PATH.unlink(missing_ok=True)
        raise
    return process.pid


def _stop_detached_service() -> int:
    pid = _read_service_pid()
    if pid is None or not _service_process_matches(pid):
        SERVICE_PID_PATH.unlink(missing_ok=True)
        raise RuntimeError("service is not running")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not _service_process_matches(pid):
            SERVICE_PID_PATH.unlink(missing_ok=True)
            return pid
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    SERVICE_PID_PATH.unlink(missing_ok=True)
    return pid


def main(argv: list[str] | None = None) -> None:
    global ALLOWED_CONTAINERS, RUNLOG_DIR
    args = _parse_args(argv)
    ALLOWED_CONTAINERS = frozenset(args.allow_container)
    RUNLOG_DIR = _configure_runlog_dir(args.runlog_dir)
    if args.mode == "exec":
        try:
            stdin = _manual_stdin(args)
            result, exit_code = asyncio.run(
                _execute(
                    args.command,
                    timeout_sec=args.timeout_sec,
                    stdin=stdin,
                    container=args.container,
                    thread_id=MANUAL_RUN_ID,
                    ctx=_ManualContext(),
                )
            )
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"container-mcp: error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        print(result)
        raise SystemExit(_manual_exit_status(exit_code))
    service_start_modes = {None, "serve", "install-service", "start-service"}
    if args.mode in service_start_modes and not ALLOWED_CONTAINERS:
        print(
            "container-mcp: error: at least one --allow-container is required for MCP service mode",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if args.mode == "serve":
        server.settings.host = args.host
        server.settings.port = args.port
        server.run("streamable-http")
        return
    if args.mode == "install-service":
        try:
            unit_path = _install_service(
                args.service_name,
                args.port,
                scope=args.scope,
                service_user=args.service_user,
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"container-mcp: service installation failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(f"installed and started {args.service_name}: {unit_path}")
        return
    if args.mode == "start-service":
        try:
            pid = _start_detached_service(args.port)
        except (OSError, RuntimeError) as exc:
            print(f"container-mcp: service start failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(
            f"started container-mcp on {DEFAULT_HTTP_HOST}:{args.port} (pid {pid}); "
            f"log: {SERVICE_LOG_PATH}"
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
        print(f"container-mcp is running on {DEFAULT_HTTP_HOST}:{DEFAULT_HTTP_PORT} (pid {pid})")
        return
    server.run("stdio")


if __name__ == "__main__":
    main()
