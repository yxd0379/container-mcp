from __future__ import annotations

import os
import shlex
import uuid
from datetime import timedelta

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


URL = os.environ.get("CONTAINER_MCP_URL", "http://127.0.0.1:9943/mcp")
CONTAINER = os.environ.get("CONTAINER_MCP_TEST_CONTAINER", "simjoin")

pytestmark = pytest.mark.skipif(
    os.environ.get("CONTAINER_MCP_LIVE") != "1",
    reason="set CONTAINER_MCP_LIVE=1 to run the real MCP/Docker smoke test",
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
    run_id = str(uuid.uuid4())
    path = f"/tmp/container-mcp-live-{run_id}.txt"
    meta = {"threadId": run_id}

    async with httpx.AsyncClient(trust_env=False) as client:
        async with streamable_http_client(URL, http_client=client) as streams:
            read_stream, write_stream, _ = streams
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
