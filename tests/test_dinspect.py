from __future__ import annotations

import asyncio

import pytest

from container_mcp import dinspect as dinspect_module
from container_mcp import server


class _InspectProcess:
    def __init__(self, stdout: bytes, *, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def test_dinspect_running_container_uses_safe_ports_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    monkeypatch.setattr(server, "resolve_container", lambda container: container, raising=False)

    async def fake_create(*args: str, **kwargs: object) -> _InspectProcess:
        calls.append((args, kwargs))
        return _InspectProcess(b"name=/simjoin\nstatus=running\nports=null\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    result = asyncio.run(dinspect_module.dinspect("simjoin"))

    args, kwargs = calls[0]
    assert args[:4] == ("docker", "inspect", "simjoin", "--format")
    assert '(index .Config "ExposedPorts")' in args[4]
    assert "cwd" not in kwargs
    assert "status=running" in result


def test_dinspect_rejects_stopped_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "resolve_container", lambda container: container, raising=False)

    async def fake_create(*_args: str, **_kwargs: object) -> _InspectProcess:
        return _InspectProcess(b"name=/simjoin\nstatus=exited\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(ValueError, match=r"not running \(status: exited\)"):
        asyncio.run(dinspect_module.dinspect("simjoin"))
