from __future__ import annotations

import io
import socket
import sys
from pathlib import Path

import pytest

from container_mcp import cli, dexec, server as runtime


def test_foreground_service_uses_configured_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "container-mcp.sock"
    calls: list[Path] = []
    monkeypatch.setattr(runtime, "RUNLOG_DIR", tmp_path)
    monkeypatch.setattr(runtime, "ALLOWED_CONTAINERS", frozenset())
    monkeypatch.setattr(runtime, "run_uds_server", calls.append)

    cli.main(
        [
            "--allow-container",
            "simjoin",
            "--runlog-dir",
            str(tmp_path),
            "--socket-path",
            str(socket_path),
            "serve",
        ]
    )

    assert calls == [socket_path]
    assert runtime.ALLOWED_CONTAINERS == frozenset({"simjoin"})
    assert runtime.RUNLOG_DIR == tmp_path.resolve()


def test_default_mode_runs_stdio_proxy_without_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "container-mcp.sock"
    calls: list[Path] = []
    monkeypatch.setattr(runtime, "ALLOWED_CONTAINERS", frozenset())
    monkeypatch.setattr(runtime, "RUNLOG_DIR", runtime.DEFAULT_RUNLOG_DIR)
    monkeypatch.setattr(cli, "run_stdio_proxy", calls.append)

    cli.main(["--socket-path", str(socket_path)])

    assert calls == [socket_path]


def test_stale_socket_is_removed(tmp_path: Path) -> None:
    socket_path = tmp_path / "container-mcp.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(socket_path))

    cli._remove_stale_socket(socket_path)

    assert not socket_path.exists()


def test_manual_exec_forwards_stdin_and_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    async def execute(_command: str, **kwargs: object) -> tuple[str, int]:
        calls.append(kwargs)
        return "status: failed\nexit_code: 7", 7

    monkeypatch.setattr(dexec, "execute", execute)
    monkeypatch.setattr(sys, "stdin", io.StringIO("input\n"))
    monkeypatch.setattr(runtime, "RUNLOG_DIR", runtime.DEFAULT_RUNLOG_DIR)
    monkeypatch.setattr(runtime, "ALLOWED_CONTAINERS", frozenset())

    with pytest.raises(SystemExit) as stopped:
        cli.main(
            [
                "--runlog-dir",
                str(tmp_path),
                "exec",
                "--container",
                "simjoin",
                "consume",
                "-",
            ]
        )

    assert stopped.value.code == 7
    assert calls[0]["stdin"] == "input\n"
    assert calls[0]["runlog_dir"] == tmp_path.resolve()
    assert "exit_code: 7" in capsys.readouterr().out


def test_detached_service_writes_repo_tmp_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command: list[str] = []

    class Process:
        pid = 4321
        returncode = None

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    def popen(argv: list[str], **_kwargs: object) -> Process:
        command.extend(argv)
        return Process()

    runtime_tmp = tmp_path / "tmp"
    monkeypatch.setattr(runtime, "TMP_DIR", runtime_tmp)
    monkeypatch.setattr(runtime, "SERVICE_LOG_PATH", runtime_tmp / "container-mcp.log")
    monkeypatch.setattr(runtime, "SERVICE_PID_PATH", runtime_tmp / "container-mcp.pid")
    monkeypatch.setattr(runtime, "ALLOWED_CONTAINERS", frozenset({"simjoin"}))
    monkeypatch.setattr(runtime, "RUNLOG_DIR", tmp_path / "RUNLOG")
    monkeypatch.setattr(cli, "_read_service_pid", lambda: None)
    monkeypatch.setattr(cli, "_wait_for_service", lambda _socket, _process: None)
    monkeypatch.setattr(cli.subprocess, "Popen", popen)

    socket_path = runtime_tmp / "container-mcp.sock"
    assert cli._start_detached_service(socket_path) == 4321
    assert runtime.SERVICE_PID_PATH.read_text(encoding="utf-8") == "4321\n"
    assert command[:2] == [sys.executable, str(runtime.PROJECT_DIR / "run.py")]
    assert command[-3:] == ["--allow-container", "simjoin", "serve"]
    assert command[command.index("--socket-path") + 1] == str(socket_path)
