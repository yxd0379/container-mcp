from __future__ import annotations

import asyncio
import codecs
import fcntl
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import TextIO

import anyio
import mcp.types as mcp_types
from mcp.server.fastmcp import Context


CODEX_THREAD_ID_META_KEY = "threadId"

MAX_RETURN_CHARS = 60_000
PROGRESS_TAIL_CHARS = 1_000
STDIN_LOG_CHAR_LIMIT = 200_000
IO_CHUNK_SIZE = 64 * 1024
MAX_FILTERED_LINE_BYTES = 4 * 1024

PROGRESS_INTERVAL_SEC = 1.0
CONTAINER_KILL_AFTER_SEC = 5
HOST_TIMEOUT_OVERHEAD_SEC = CONTAINER_KILL_AFTER_SEC + 3
PROCESS_TERMINATE_GRACE_SEC = 5
CONTAINER_CLEANUP_TIMEOUT_SEC = 8
STDIN_CLOSE_TIMEOUT_SEC = 5

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
bash -lic "$command"
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
            return source.read(PROGRESS_TAIL_CHARS * 4).decode("utf-8", errors="replace")[
                -PROGRESS_TAIL_CHARS:
            ]


def _safe_thread_id(thread_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", thread_id)


def _yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _log_file(thread_id: str, runlog_dir: Path) -> Path:
    runlog_dir.mkdir(parents=True, exist_ok=True)
    date_id = datetime.now().strftime("%y%m%d")
    return runlog_dir / f"{date_id}_{_safe_thread_id(thread_id)}.log"


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
    fh.write(f"host-cwd: {_yaml_quote(str(Path.cwd()))}\n")
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
    runlog_dir: Path,
) -> None:
    duration_sec = int((end - start).total_seconds())
    path = _log_file(thread_id, runlog_dir)
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
        fh.write(f"executor: {_yaml_quote(f'docker exec {container} timeout ... bash -lic')}\n")
        fh.write("```bash\n")
        fh.write(command)
        if not command.endswith("\n"):
            fh.write("\n")
        fh.write("```\n")
        if stdin is not None:
            stdin_bytes = _stdin_byte_count(stdin)
            if not stdin:
                fh.write("stdin: 0 bytes\n")
            elif len(stdin) <= STDIN_LOG_CHAR_LIMIT and all(
                char.isprintable() or char.isspace() for char in stdin
            ):
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


def set_logging_level(level: mcp_types.LoggingLevel) -> None:
    global _mcp_log_level
    _mcp_log_level = str(level)


def codex_thread_id(ctx: Context | None) -> str:
    """Read the request-scoped Codex thread id without trusting process environment."""
    try:
        meta = ctx.request_context.meta if ctx is not None else None
    except (AttributeError, ValueError):
        meta = None
    raw_thread_id = (
        getattr(meta, CODEX_THREAD_ID_META_KEY, None) if meta is not None else None
    )
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
        raise ValueError(
            "stdin must contain valid UTF-8 text without isolated surrogate code points"
        ) from exc


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
        latest_line = next(
            (line.strip() for line in reversed(latest.splitlines()) if line.strip()), ""
        )
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


async def terminate_process(process: asyncio.subprocess.Process) -> None:
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
        )
    except OSError as exc:
        return f"Container cleanup could not start: {exc}", False
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=CONTAINER_CLEANUP_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        await terminate_process(process)
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


async def execute(
    command: str,
    timeout_sec: int = 120,
    stdin: str | None = None,
    *,
    container: str,
    thread_id: str,
    runlog_dir: Path,
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
                process.wait(), timeout=timeout_sec + HOST_TIMEOUT_OVERHEAD_SEC
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
        with anyio.CancelScope(shield=True):
            needs_container_cleanup = (
                cancelled_error is not None or host_timed_out or pending_error is not None
            )
            container_group_found = False
            if needs_container_cleanup:
                wait_attempts = 20 if process is not None and not host_timed_out else 0
                cleanup_error, container_group_found = await _cleanup_container_group(
                    container, pidfile, wait_attempts
                )
                if cleanup_error:
                    diagnostics.append(cleanup_error)
            if process is not None and process.returncode is None:
                await terminate_process(process)
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
                diagnostics.append(
                    f"Command timed out after {timeout_sec}s and required host-side cleanup."
                )
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
                    runlog_dir=runlog_dir,
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


async def dexec(
    command: str,
    container: str,
    timeout_sec: int = 120,
    stdin: str | None = None,
    ctx: Context | None = None,
) -> str:
    """
    Run a command through a login, interactive Bash shell in an allowed container.
    Supports optional stdin, returns a bounded output preview.
    """
    from . import server as runtime

    selected_container = runtime.resolve_container(container)
    result, _ = await execute(
        command,
        timeout_sec=timeout_sec,
        stdin=stdin,
        container=selected_container,
        thread_id=codex_thread_id(ctx),
        runlog_dir=runtime.RUNLOG_DIR,
        ctx=ctx,
    )
    return result
