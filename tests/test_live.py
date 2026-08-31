from __future__ import annotations

import os
import shlex
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_DIR = Path(__file__).resolve().parents[1]
SOCKET_PATH = os.environ.get("CONTAINER_MCP_TEST_SOCKET")
RUNLOG_DIR = os.environ.get("CONTAINER_MCP_TEST_RUNLOG_DIR")
CONTAINER = os.environ.get("CONTAINER_MCP_TEST_CONTAINER", "simjoin")

pytestmark = pytest.mark.skipif(
    os.environ.get("CONTAINER_MCP_LIVE") != "1" or not SOCKET_PATH or not RUNLOG_DIR,
    reason="set the live flag, isolated socket, and isolated RUNLOG directory",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _text(result: object) -> str:
    return "\n".join(
        str(getattr(content, "text", ""))
        for content in getattr(result, "content", [])
    )


@pytest.mark.anyio
async def test_live_tools() -> None:
    assert SOCKET_PATH is not None
    assert RUNLOG_DIR is not None
    run_id = str(uuid.uuid4())
    path = f"/tmp/container-mcp-live-{run_id}.txt"
    meta = {"threadId": run_id}
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_DIR / "run.py"), "--socket-path", SOCKET_PATH],
        cwd=PROJECT_DIR,
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=30),
        ) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "dexec",
                "dpatch",
                "dinspect",
            }

            inspected = await session.call_tool(
                "dinspect",
                {"container": CONTAINER},
                meta=meta,
            )
            assert not inspected.isError
            assert "status=running" in _text(inspected)

            executed = await session.call_tool(
                "dexec",
                {"container": CONTAINER, "command": "printf 'dexec-ok\\n'"},
                meta=meta,
            )
            assert not executed.isError
            assert "dexec-ok" in _text(executed)

            try:
                patched = await session.call_tool(
                    "dpatch",
                    {
                        "container": CONTAINER,
                        "patch": (
                            "*** Begin Patch\n"
                            f"*** Add File: {path}\n"
                            "+dpatch-ok\n"
                            "*** End Patch"
                        ),
                    },
                    meta=meta,
                )
                assert not patched.isError, _text(patched)

                verified = await session.call_tool(
                    "dexec",
                    {"container": CONTAINER, "command": f"cat -- {shlex.quote(path)}"},
                    meta=meta,
                )
                assert "dpatch-ok" in _text(verified)
            finally:
                await session.call_tool(
                    "dexec",
                    {"container": CONTAINER, "command": f"rm -f -- {shlex.quote(path)}"},
                    meta=meta,
                )

    assert list(Path(RUNLOG_DIR).glob(f"*_{run_id}.log"))
