from __future__ import annotations

import asyncio
import io
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anyio
import mcp.types as mcp_types
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


TEST_THREAD_ID = "019f0000-0000-7000-8000-000000000001"
PARENT_THREAD_ID = "019f0000-0000-7000-8000-000000000002"


class FakeContext:
    def __init__(self, thread_id: object = TEST_THREAD_ID) -> None:
        self.progress: list[tuple[float, str | None, float]] = []
        self.logs: list[tuple[str, str | None, float]] = []
        meta = SimpleNamespace()
        if thread_id is not None:
            setattr(meta, server.CODEX_THREAD_ID_META_KEY, thread_id)
        self.request_context = SimpleNamespace(meta=meta)

    async def report_progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        self.progress.append((progress, message, time.monotonic()))

    async def info(self, message: str, logger_name: str | None = None) -> None:
        self.logs.append((message, logger_name, time.monotonic()))


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runlog_dir = tmp_path / "runlogs"
    monkeypatch.setattr(server, "RUNLOG_DIR", runlog_dir)
    monkeypatch.setattr(server, "WORKING_DIR", tmp_path)
    monkeypatch.setattr(server, "CONTAINER", "simjoin")
    monkeypatch.setattr(server, "PROGRESS_INTERVAL_SEC", 0.02)
    monkeypatch.setattr(server, "HOST_TIMEOUT_OVERHEAD_SEC", 0.2)
    monkeypatch.setattr(server, "_codex_thread_metadata", lambda _thread_id: ("", ""))

    async def no_container_cleanup(_pidfile: str, _wait_attempts: int = 0) -> tuple[str, bool]:
        return "", False

    monkeypatch.setattr(server, "_cleanup_container_group", no_container_cleanup)
    return runlog_dir


def install_fake_docker(
    monkeypatch: pytest.MonkeyPatch,
    child_code: str,
    processes: list[asyncio.subprocess.Process] | None = None,
) -> list[tuple[str, ...]]:
    real_create_subprocess_exec = asyncio.create_subprocess_exec
    calls: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        calls.append(args)
        process = await real_create_subprocess_exec(
            sys.executable,
            "-c",
            child_code,
            args[-1],
            **kwargs,
        )
        if processes is not None:
            processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return calls


def current_log_path(runlog_dir: Path) -> Path:
    date_id = datetime.now().strftime("%y%m%d")
    return runlog_dir / f"{date_id}_{TEST_THREAD_ID}.log"


def run_dexec(
    command: str,
    *,
    stdin: str | None = None,
    timeout_sec: int = 10,
    ctx: FakeContext | None = None,
) -> str:
    request_context = ctx if ctx is not None else FakeContext()
    return asyncio.run(
        server.dexec(
            command,
            timeout_sec=timeout_sec,
            stdin=stdin,
            ctx=request_context,
        )
    )


def test_dexec_runs_command_and_writes_runlog(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_docker(monkeypatch, "print('hello-from-mcp')")

    result = run_dexec("emit hello")

    assert "status: ok" in result
    assert "exit_code: 0" in result
    assert "hello-from-mcp" in result
    log_text = current_log_path(isolated_runtime).read_text(encoding="utf-8")
    assert f"codex-thread-id: '{TEST_THREAD_ID}'" in log_text
    assert "container: simjoin" in log_text
    assert "emit hello" in log_text
    assert "hello-from-mcp" in log_text


def test_dexec_reports_nonzero_and_filters_job_control_noise(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_docker(
        monkeypatch,
        "import sys; "
        "sys.stderr.write('bash: no job control in this shell\\nuseful stderr\\n'); "
        "raise SystemExit(7)",
    )

    result = run_dexec("fail")

    assert "status: failed" in result
    assert "exit_code: 7" in result
    assert "useful stderr" in result
    assert "no job control" not in result
    log_text = current_log_path(isolated_runtime).read_text(encoding="utf-8")
    assert "useful stderr" in log_text
    assert "no job control" not in log_text


def test_stdin_none_does_not_add_interactive_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_fake_docker(monkeypatch, "print('done')")

    run_dexec("no stdin")

    assert calls[0][:2] == ("docker", "exec")
    assert "-i" not in calls[0]


@pytest.mark.parametrize("stdin", ["", "line one\n第二行", "no-final-newline"])
def test_text_stdin_is_forwarded_and_logged(
    stdin: str,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_fake_docker(
        monkeypatch,
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    )

    result = run_dexec("consume stdin", stdin=stdin)

    assert calls[0][:3] == ("docker", "exec", "-i")
    if stdin:
        assert stdin in result
    else:
        assert result == "status: ok\nexit_code: 0"
    log_text = current_log_path(isolated_runtime).read_text(encoding="utf-8")
    if stdin:
        assert stdin in log_text
    else:
        assert "stdin: 0 bytes" in log_text


def test_large_output_is_bounded_in_result_but_complete_in_runlog(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_chars = server.MAX_RETURN_CHARS + 137
    install_fake_docker(monkeypatch, f"print('x' * {output_chars}, end='')")

    result = run_dexec("large output")

    assert "...[truncated 137 chars; complete output is in RUNLOG]" in result
    assert len(result) < output_chars + 200
    log_text = current_log_path(isolated_runtime).read_text(encoding="utf-8")
    assert "x" * output_chars in log_text


def test_capture_handles_split_and_invalid_utf8_without_unbounded_preview(tmp_path: Path) -> None:
    capture = server._TextCapture(tmp_path / "capture", preview_limit=3)
    capture.feed("你好".encode("utf-8")[:4])
    capture.feed("你好".encode("utf-8")[4:] + b"\xff")
    capture.finish()

    assert capture.result_text().startswith("你好�")
    assert "truncated" not in capture.result_text()
    assert capture.path.read_text(encoding="utf-8") == "你好�"


def test_stderr_filter_handles_split_crlf_and_long_lines(tmp_path: Path) -> None:
    capture = server._TextCapture(tmp_path / "stderr")
    stderr_filter = server._StderrFilter(capture)
    stderr_filter.feed(b"bash: cannot set terminal process group 12: Inappro")
    stderr_filter.feed(b"priate ioctl for device\r\n")
    stderr_filter.feed(b"bash: [123: 2 (255)] tcsetattr: Inappropriate ioctl for device\n")
    stderr_filter.feed(b"z" * (server.MAX_FILTERED_LINE_BYTES + 10))
    assert len(stderr_filter._pending) <= server.MAX_FILTERED_LINE_BYTES
    stderr_filter.feed(b"\nuseful")
    stderr_filter.finish()
    capture.finish()

    text = capture.path.read_text(encoding="utf-8")
    assert "cannot set terminal process group" not in text
    assert "tcsetattr" not in text
    assert "z" * (server.MAX_FILTERED_LINE_BYTES + 10) in text
    assert text.endswith("useful")


def test_completion_marker_drops_only_its_separator_blank(tmp_path: Path) -> None:
    capture = server._TextCapture(tmp_path / "stderr")
    stderr_filter = server._StderrFilter(capture, "__complete__=")
    stderr_filter.feed(b"\n__complete__=0\n")
    stderr_filter.finish()
    capture.finish()

    assert stderr_filter.completion_exit_code == 0
    assert capture.result_text() == ""


def test_progress_notifications_arrive_before_final_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_docker(
        monkeypatch,
        "import sys, time; print('phase-one', flush=True); time.sleep(0.12); print('done')",
    )
    ctx = FakeContext()
    finished_at = 0.0

    result = run_dexec("slow build", ctx=ctx)
    finished_at = time.monotonic()

    assert "done" in result
    running_logs = [event for event in ctx.logs if " running " in event[0]]
    assert running_logs
    assert all(event[2] < finished_at for event in running_logs)
    assert any("phase-one" in event[0] for event in running_logs)
    assert ctx.progress
    assert all(event[1] == "simjoin-dexec" for event in ctx.logs)


def test_completion_marker_never_leaks_to_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_docker(
        monkeypatch,
        "import sys; print('real output', flush=True); "
        "sys.stderr.write('\\n' + sys.argv[1] + '0\\n')",
    )
    ctx = FakeContext()

    result = run_dexec("marker notifications", ctx=ctx)

    assert "real output" in result
    messages = [message or "" for _, message, _ in ctx.progress]
    messages.extend(message for message, _, _ in ctx.logs)
    assert messages
    assert all("__MCP_DEXEC_COMPLETE_" not in message for message in messages)


def test_timeout_terminates_process_and_preserves_output(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_docker(
        monkeypatch,
        "import sys, time; print('before-timeout', flush=True); time.sleep(5)",
    )

    started = time.monotonic()
    result = run_dexec("timeout", timeout_sec=1)

    assert time.monotonic() - started < 3
    assert "status: failed" in result
    assert "exit_code: timeout" in result
    assert "before-timeout" in result
    assert "Command timed out after 1s" in result
    assert "before-timeout" in current_log_path(isolated_runtime).read_text(encoding="utf-8")


def test_natural_exit_124_is_not_misreported_as_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_docker(
        monkeypatch,
        "import sys; print(sys.argv[1] + '124', file=sys.stderr); raise SystemExit(124)",
    )

    result = run_dexec("natural 124")

    assert "exit_code: 124" in result
    assert "timed out" not in result
    assert "__MCP_DEXEC_COMPLETE_" not in result


def test_cancellation_reaps_process_and_persists_audit_log(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[asyncio.subprocess.Process] = []
    cleanup_calls: list[int] = []

    async def record_cleanup(_pidfile: str, wait_attempts: int = 0) -> tuple[str, bool]:
        cleanup_calls.append(wait_attempts)
        return "", False

    monkeypatch.setattr(server, "_cleanup_container_group", record_cleanup)
    install_fake_docker(
        monkeypatch,
        "import time; print('before-cancel', flush=True); time.sleep(5)",
        processes,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            server.dexec("cancel me", timeout_sec=10, ctx=FakeContext())
        )
        await asyncio.sleep(0.08)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert processes
    assert all(process.returncode is not None for process in processes)
    assert cleanup_calls == [20, 20]
    log_text = current_log_path(isolated_runtime).read_text(encoding="utf-8")
    assert "exit-code: cancelled" in log_text
    assert "before-cancel" in log_text
    assert "Command cancelled by the MCP client" in log_text


def test_anyio_level_cancellation_cannot_interrupt_cleanup(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[asyncio.subprocess.Process] = []
    install_fake_docker(
        monkeypatch,
        "import time; print('anyio-cancel', flush=True); time.sleep(5)",
        processes,
    )

    async def scenario() -> None:
        async def invoke() -> None:
            await server.dexec("anyio cancel", timeout_sec=10, ctx=FakeContext())

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(invoke)
            await anyio.sleep(0.08)
            task_group.cancel_scope.cancel()

    anyio.run(scenario, backend="asyncio")

    assert processes
    assert all(process.returncode is not None for process in processes)
    log_text = current_log_path(isolated_runtime).read_text(encoding="utf-8")
    assert "exit-code: cancelled" in log_text
    assert "anyio-cancel" in log_text


def test_inputs_are_validated_before_starting_process(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_process(*args: object, **kwargs: object) -> None:
        raise AssertionError("process should not start")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_process)

    with pytest.raises(ValueError, match="command must not be empty"):
        run_dexec("   ")
    with pytest.raises(RuntimeError, match=r"missing _meta\.threadId"):
        asyncio.run(server.dexec("true", ctx=FakeContext(None)))
    with pytest.raises(RuntimeError, match="not a valid UUID"):
        asyncio.run(server.dexec("true", ctx=FakeContext("parent-thread")))
    with pytest.raises(RuntimeError, match="not a canonical UUID"):
        asyncio.run(server.dexec("true", ctx=FakeContext("{019f0000-0000-7000-8000-000000000001}")))
    with pytest.raises(ValueError, match="timeout_sec must be between"):
        run_dexec("true", timeout_sec=0)
    with pytest.raises(ValueError, match="valid UTF-8 text"):
        run_dexec("true", stdin="bad\ud800")


def test_request_metadata_overrides_inherited_parent_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", PARENT_THREAD_ID)
    meta = mcp_types.RequestParams.Meta(**{server.CODEX_THREAD_ID_META_KEY: TEST_THREAD_ID})
    ctx = SimpleNamespace(request_context=SimpleNamespace(meta=meta))

    assert server._codex_thread_id(ctx) == TEST_THREAD_ID


def test_legacy_thread_argument_cannot_spoof_request_metadata(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_docker(monkeypatch, "print('legacy-client')")
    tool = server.server._tool_manager.get_tool("dexec")
    assert tool is not None

    result = asyncio.run(
        tool.run(
            {
                "command": "legacy call",
                "thread_id": PARENT_THREAD_ID,
            },
            context=FakeContext(),
        )
    )

    assert "legacy-client" in result
    assert current_log_path(isolated_runtime).is_file()
    parent_log = isolated_runtime / f"{datetime.now().strftime('%y%m%d')}_{PARENT_THREAD_ID}.log"
    assert not parent_log.exists()


def test_startup_arguments_configure_container_and_runlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "WORKING_DIR", tmp_path)
    monkeypatch.setenv("MCP_DEXEC_CONTAINER", "from-env")

    defaults = server._parse_args([])
    args = server._parse_args(
        ["--container", "from-cli", "--runlog-dir", "custom/logs"]
    )
    manual_args = server._parse_args(
        [
            "--container",
            "manual-container",
            "--runlog-dir",
            "manual/logs",
            "exec",
            "--timeout-sec",
            "17",
            "read value; printf '%s\\n' \"$value\"",
            "-",
        ]
    )
    serve_args = server._parse_args(["serve", "--port", "9943"])
    install_args = server._parse_args(
        ["install-service", "--port", "9943", "--service-name", "custom-dexec"]
    )

    assert defaults.container == "from-env"
    assert defaults.mode is None
    assert args.container == "from-cli"
    assert args.mode is None
    assert server._configure_runlog_dir(args.runlog_dir) == (tmp_path / "custom/logs").resolve()
    absolute = tmp_path / "absolute"
    assert server._configure_runlog_dir(str(absolute)) == absolute.resolve()
    assert manual_args.mode == "exec"
    assert manual_args.container == "manual-container"
    assert manual_args.runlog_dir == "manual/logs"
    assert manual_args.timeout_sec == 17
    assert manual_args.command == "read value; printf '%s\\n' \"$value\""
    assert manual_args.stdin_source == "-"
    assert serve_args.mode == "serve"
    assert serve_args.host == "127.0.0.1"
    assert serve_args.port == 9943
    assert install_args.mode == "install-service"
    assert install_args.port == 9943
    assert install_args.service_name == "custom-dexec.service"
    assert install_args.scope == "user"

    with pytest.raises(SystemExit):
        server._parse_args(["--container", "--not-a-container"])
    with pytest.raises(SystemExit):
        server._parse_args(["--container", "bad name"])


def test_default_cli_mode_still_starts_stdio_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(server.server, "run", calls.append)

    server.main(["--container", "simjoin", "--runlog-dir", "logs"])

    assert calls == ["stdio"]


def test_serve_cli_starts_streamable_http_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(server.server, "run", calls.append)

    server.main(["serve", "--host", "127.0.0.1", "--port", "9943"])

    assert calls == ["streamable-http"]
    assert server.server.settings.host == "127.0.0.1"
    assert server.server.settings.port == 9943


def test_systemd_unit_uses_current_python_and_central_runlog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(server, "RUNLOG_DIR", tmp_path / "central-runlog")

    unit = server._systemd_unit(service_name="simjoin-dexec.service", port=9943)

    assert "Type=simple" in unit
    assert f'ExecStart="{Path(sys.executable)}"' in unit
    assert f'"{tmp_path / "central-runlog"}"' in unit
    assert '"serve" "--host" "127.0.0.1" "--port" "9943"' in unit
    assert "Restart=on-failure" in unit

    system_unit = server._systemd_unit(
        service_name="simjoin-dexec.service",
        port=9943,
        scope="system",
        service_user="yuxd",
    )
    assert "User=yuxd" in system_unit
    assert "After=docker.service" in system_unit
    assert "WantedBy=multi-user.target" in system_unit


def test_manual_cli_exec_uses_stable_run_id_stdin_and_command_exit_code(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = install_fake_docker(
        monkeypatch,
        "import sys; data = sys.stdin.read(); print('manual=' + data); raise SystemExit(7)",
    )

    def unexpected_thread_lookup(_ctx: object) -> str:
        raise AssertionError("manual execution must not require Codex request metadata")

    monkeypatch.setattr(server, "_codex_thread_id", unexpected_thread_lookup)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello-from-human"))

    with pytest.raises(SystemExit) as stopped:
        server.main(
            [
                "--container",
                "simjoin",
                "--runlog-dir",
                str(isolated_runtime),
                "exec",
                "--timeout-sec",
                "9",
                "consume input",
                "-",
            ]
        )

    assert stopped.value.code == 7
    assert calls[0][:3] == ("docker", "exec", "-i")
    captured = capsys.readouterr()
    assert "status: failed" in captured.out
    assert "exit_code: 7" in captured.out
    assert "manual=hello-from-human" in captured.out
    assert "[dexec:manual]" in captured.err

    date_id = datetime.now().strftime("%y%m%d")
    manual_log = isolated_runtime / f"{date_id}_{server.MANUAL_RUN_ID}.log"
    log_text = manual_log.read_text(encoding="utf-8")
    assert "codex-thread-id: 'manual'" in log_text
    assert "consume input" in log_text
    assert "hello-from-human" in log_text


def test_manual_stdin_dash_reads_process_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = server._parse_args(["exec", "consume input", "-"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped input\n"))

    assert server._manual_stdin(args) == "piped input\n"


def test_manual_stdin_is_omitted_without_dash() -> None:
    args = server._parse_args(["exec", "consume input"])

    assert server._manual_stdin(args) is None


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(0, 0), (23, 23), ("timeout", 124), ("cancelled", 130), ("error", 1)],
)
def test_manual_exit_status(exit_code: int | str, expected: int) -> None:
    assert server._manual_exit_status(exit_code) == expected


def test_selected_container_is_used_for_execution_and_runlog(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "CONTAINER", "portable-container_2")
    calls = install_fake_docker(monkeypatch, "print('container-selected')")

    result = run_dexec("container probe")

    assert "container-selected" in result
    assert calls[0][:3] == ("docker", "exec", "portable-container_2")
    log_text = current_log_path(isolated_runtime).read_text(encoding="utf-8")
    assert "container: portable-container_2" in log_text


def test_tool_schema_exposes_stdin_but_not_context_or_thread_id() -> None:
    tool = server.server._tool_manager.get_tool("dexec")

    assert tool is not None
    properties = tool.parameters["properties"]
    assert "stdin" in properties
    assert "ctx" not in properties
    assert "thread_id" not in properties
    assert tool.parameters["required"] == ["command"]


def test_server_advertises_logging_capability() -> None:
    options = server.server._mcp_server.create_initialization_options()

    assert options.capabilities.logging is not None


def test_project_configs_use_shared_http_service_and_central_runlog() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    project_names = ("feature_retrieval", "IndexSDK", "play-IndexSDK", "play-op")
    config_paths = [
        repo_root / "projects" / name / ".codex" / "config.toml"
        for name in project_names
    ]

    assert not (repo_root / ".codex" / "config.toml").exists()
    assert (repo_root / "RUNLOG").is_dir()
    assert not (repo_root / "wiki").exists()
    assert not (repo_root / "scripts").exists()
    assert not (repo_root / "mcp_dexec" / ".codex").exists()
    assert not (repo_root / "mcp_dexec" / "RUNLOG").exists()
    for project_name in project_names:
        project_root = repo_root / "projects" / project_name
        assert not (project_root / "RUNLOG").exists()
        assert all((project_root / name).is_dir() for name in ("wiki", "notes", "scripts"))
    for config_path in config_paths:
        config_text = config_path.read_text(encoding="utf-8")
        assert 'url = "http://127.0.0.1:9943/mcp"' in config_text
        assert "required =" not in config_text
        assert 'command = "uv"' not in config_text
        assert "--no-cache" not in config_text
        assert "cwd =" not in config_text
        assert 'env_vars = ["CODEX_THREAD_ID"]' not in config_text
        assert "startup_timeout_sec = 60" in config_text
        assert "tool_timeout_sec = 3700" in config_text

    active_files = [
        *config_paths,
        repo_root / "AGENTS.md",
        repo_root / "mcp_dexec" / "readme.md",
    ]
    legacy_repo = "play-feature" + "_retrieve"
    assert all(legacy_repo not in path.read_text(encoding="utf-8") for path in active_files)
