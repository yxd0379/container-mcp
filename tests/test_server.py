from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from container_mcp import server as runtime


def test_server_exposes_three_described_tools() -> None:
    tools = {tool.name: tool for tool in runtime.server._tool_manager.list_tools()}

    assert set(tools) == {"dexec", "dpatch", "dinspect"}
    assert set(tools["dexec"].parameters["required"]) == {"command", "container"}
    assert set(tools["dexec"].parameters["properties"]) == {
        "command",
        "container",
        "timeout_sec",
        "stdin",
    }
    assert tools["dpatch"].parameters["required"] == ["patch", "container"]
    assert tools["dinspect"].parameters["required"] == ["container"]
    assert "login, interactive Bash" in tools["dexec"].description


def test_container_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "ALLOWED_CONTAINERS", frozenset({"simjoin"}))

    assert runtime.resolve_container("simjoin") == "simjoin"
    with pytest.raises(ValueError, match="not allowed"):
        runtime.resolve_container("other")
    with pytest.raises(ValueError, match="letters"):
        runtime.resolve_container("bad/name")


def test_tool_lifecycle_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mcp = runtime.ContainerMCP("test")

    @mcp.tool()
    async def succeed(container: str) -> str:
        return container

    @mcp.tool()
    async def fail(container: str) -> str:
        raise ValueError("diagnostic")

    monkeypatch.setattr(runtime, "ALLOWED_CONTAINERS", frozenset({"simjoin"}))
    monkeypatch.setattr(
        runtime.ContainerMCP,
        "get_context",
        lambda _self: SimpleNamespace(request_id="request-1"),
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    asyncio.run(mcp.call_tool("succeed", {"container": "simjoin"}))
    with pytest.raises(Exception):
        asyncio.run(mcp.call_tool("fail", {"container": "simjoin"}))

    messages = [record.getMessage() for record in caplog.records]
    assert any("tool.start request_id=request-1 tool=succeed" in line for line in messages)
    assert any("tool.finish request_id=request-1 tool=succeed" in line for line in messages)
    assert any("tool.failed request_id=request-1 tool=fail" in line for line in messages)


def test_uvicorn_logs_include_local_timestamp() -> None:
    log_config = runtime.uvicorn_log_config()

    for formatter in log_config["formatters"].values():
        assert formatter["datefmt"] == "%Y-%m-%d %H:%M:%S %z"
        assert formatter["fmt"].startswith("%(asctime)s ")
    assert log_config["root"] == {"handlers": ["default"], "level": "INFO"}
