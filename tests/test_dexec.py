from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import container_mcp.dexec as dexec_module
import container_mcp.server as runtime


THREAD_ID = "019f0000-0000-7000-8000-000000000001"


class FakeContext:
    def __init__(self, thread_id: str | None = THREAD_ID) -> None:
        meta = SimpleNamespace()
        if thread_id is not None:
            meta.threadId = thread_id
        self.request_context = SimpleNamespace(meta=meta)

    async def report_progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        return None

    async def info(self, message: str, logger_name: str | None = None) -> None:
        return None


class FakeWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


class FakeProcess:
    def __init__(self, exit_code: int, writer: FakeWriter | None) -> None:
        self._exit_code = exit_code
        self.stdin = writer
        self.returncode: int | None = None

    async def wait(self) -> int:
        while self.stdin is not None and not self.stdin.closed:
            await asyncio.sleep(0)
        self.returncode = self._exit_code
        return self._exit_code

    def terminate(self) -> None:
        self.returncode = self._exit_code

    def kill(self) -> None:
        self.returncode = self._exit_code


class FakeDocker:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.calls: list[tuple[str, ...]] = []
        self.writers: list[FakeWriter] = []

    async def create(self, *args: str, **kwargs: object) -> FakeProcess:
        self.calls.append(args)
        stdout_file = kwargs["stdout"]
        stderr_file = kwargs["stderr"]
        stdout_file.write(self.stdout)  # type: ignore[union-attr]
        marker = args[-1].encode()
        stderr_file.write(self.stderr + b"\n" + marker + str(self.exit_code).encode() + b"\n")  # type: ignore[union-attr]
        writer = FakeWriter() if kwargs["stdin"] == asyncio.subprocess.PIPE else None
        if writer is not None:
            self.writers.append(writer)
        return FakeProcess(self.exit_code, writer)


def install_fake_docker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    exit_code: int = 0,
) -> FakeDocker:
    fake = FakeDocker(stdout=stdout, stderr=stderr, exit_code=exit_code)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create)
    monkeypatch.setattr(dexec_module, "_codex_thread_metadata", lambda _thread_id: ("", ""))
    return fake


def run_execute(runlog_dir: Path, **kwargs: object) -> tuple[str, int | str]:
    return asyncio.run(
        dexec_module.execute(
            "printf hello",
            container="simjoin",
            thread_id=THREAD_ID,
            runlog_dir=runlog_dir,
            **kwargs,
        )
    )


def test_success_writes_runlog_and_uses_login_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_docker(monkeypatch, stdout=b"hello\n")

    result, exit_code = run_execute(tmp_path)

    assert exit_code == 0
    assert "status: ok" in result
    assert "hello" in result
    assert any('bash -lic "$command"' in argument for argument in fake.calls[0])
    log = next(tmp_path.glob("*.log")).read_text(encoding="utf-8")
    assert f"codex-thread-id: '{THREAD_ID}'" in log
    assert "container: simjoin" in log
    assert "printf hello" in log
    assert "hello" in log


def test_stdin_is_forwarded_and_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_docker(monkeypatch, stdout=b"received\n")

    result, exit_code = run_execute(tmp_path, stdin="first\n第二行")

    assert exit_code == 0
    assert "received" in result
    assert fake.calls[0][:3] == ("docker", "exec", "-i")
    assert bytes(fake.writers[0].data) == "first\n第二行".encode()
    log = next(tmp_path.glob("*.log")).read_text(encoding="utf-8")
    assert "first\n第二行" in log


def test_nonzero_exit_returns_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_docker(monkeypatch, stderr=b"command failed\n", exit_code=7)

    result, exit_code = run_execute(tmp_path)

    assert exit_code == 7
    assert "status: failed" in result
    assert "exit_code: 7" in result
    assert "command failed" in result


def test_large_output_is_truncated_only_in_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete = b"x" * (dexec_module.MAX_RETURN_CHARS + 17)
    install_fake_docker(monkeypatch, stdout=complete)

    result, _ = run_execute(tmp_path)

    assert "truncated 17 chars" in result
    log = next(tmp_path.glob("*.log")).read_text(encoding="utf-8")
    assert "x" * len(complete) in log


def test_thread_id_comes_only_from_request_metadata() -> None:
    assert dexec_module.codex_thread_id(FakeContext()) == THREAD_ID
    with pytest.raises(RuntimeError, match=r"missing _meta\.threadId"):
        dexec_module.codex_thread_id(FakeContext(None))


def test_tool_wrapper_resolves_container_and_uses_runtime_runlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_docker(monkeypatch, stdout=b"tool result\n")
    seen: list[str] = []

    def resolve_container(container: str) -> str:
        seen.append(container)
        return "resolved-container"

    monkeypatch.setattr(runtime, "resolve_container", resolve_container, raising=False)
    monkeypatch.setattr(runtime, "RUNLOG_DIR", tmp_path, raising=False)

    result = asyncio.run(dexec_module.dexec("true", "alias", ctx=FakeContext()))

    assert seen == ["alias"]
    assert fake.calls[0][:3] == ("docker", "exec", "resolved-container")
    assert "tool result" in result
    assert next(tmp_path.glob("*.log")).is_file()


def test_tool_wrapper_rejects_before_starting_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_docker(monkeypatch)

    def reject(_container: str) -> str:
        raise ValueError("container is not allowed")

    monkeypatch.setattr(runtime, "resolve_container", reject, raising=False)
    monkeypatch.setattr(runtime, "RUNLOG_DIR", tmp_path, raising=False)

    with pytest.raises(ValueError, match="not allowed"):
        asyncio.run(dexec_module.dexec("true", "blocked", ctx=FakeContext()))

    assert fake.calls == []
