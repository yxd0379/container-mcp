from __future__ import annotations

import asyncio
import os
import shlex
from datetime import timedelta

import httpx
import pytest
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError


SERVICE_URL = os.environ.get("CONTAINER_MCP_URL", "http://127.0.0.1:9943/mcp")


pytestmark = pytest.mark.skipif(
    os.environ.get("CONTAINER_MCP_LIVE") != "1",
    reason="set CONTAINER_MCP_LIVE=1 to run the simjoin integration through MCP",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_streamable_http_accepts_concurrent_clients() -> None:
    async def initialize_client() -> None:
        async with httpx.AsyncClient(trust_env=False) as http_client:
            async with streamable_http_client(
                SERVICE_URL,
                http_client=http_client,
            ) as (read_stream, write_stream, _):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=15),
                ) as session:
                    initialization = await session.initialize()
                    assert initialization.serverInfo.name == "container-mcp"
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {"apply_patch", "dexec"}

    await asyncio.gather(*(initialize_client() for _ in range(4)))


@pytest.mark.anyio
async def test_live_streamable_http_tool_supports_stdin_and_progress() -> None:
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    assert thread_id, "CODEX_THREAD_ID is required for the live MCP probe"

    logs: list[str] = []
    progress_messages: list[str] = []

    async def receive_log(params: object) -> None:
        logs.append(str(getattr(params, "data", "")))

    async def receive_progress(
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        del progress, total
        progress_messages.append(message or "")

    async with httpx.AsyncClient(trust_env=False) as http_client:
        async with streamable_http_client(
            SERVICE_URL,
            http_client=http_client,
        ) as (read_stream, write_stream, _), ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=15),
            logging_callback=receive_log,
        ) as session:
            initialization = await session.initialize()
            assert initialization.capabilities.logging is not None
            tools = await session.list_tools()
            dexec_tool = next(tool for tool in tools.tools if tool.name == "dexec")
            assert "stdin" in dexec_tool.inputSchema["properties"]
            assert "thread_id" not in dexec_tool.inputSchema["properties"]
            request_meta = {"threadId": thread_id}

            result = await session.call_tool(
                "dexec",
                arguments={
                    "command": (
                        "read -r value; printf 'stdin=%s\\n' \"$value\"; "
                        "printf 'phase-one\\n'; sleep 1.2; printf 'phase-two\\n'"
                    ),
                    "timeout_sec": 10,
                    "stdin": "hello-through-mcp\n",
                },
                progress_callback=receive_progress,
                meta=request_meta,
            )

            sentinel = f"/tmp/container-mcp-live-{thread_id}.sentinel"
            quoted_sentinel = shlex.quote(sentinel)
            timeout_result = await session.call_tool(
                "dexec",
                arguments={
                    "command": (
                        f"rm -f -- {quoted_sentinel}; sleep 3; "
                        f"printf 'late write\\n' > {quoted_sentinel}"
                    ),
                    "timeout_sec": 1,
                },
                progress_callback=receive_progress,
                meta=request_meta,
            )
            natural_124_result = await session.call_tool(
                "dexec",
                arguments={
                    "command": "printf 'natural 124\\n'; exit 124",
                    "timeout_sec": 5,
                },
                meta=request_meta,
            )
            cancel_sentinel = f"/tmp/container-mcp-live-cancel-{thread_id}.sentinel"
            quoted_cancel_sentinel = shlex.quote(cancel_sentinel)
            cancel_started = asyncio.Event()

            async def receive_cancel_progress(
                progress: float,
                total: float | None,
                message: str | None,
            ) -> None:
                del progress, total, message
                cancel_started.set()

            cancel_request_id = session._request_id
            cancel_task = asyncio.create_task(
                session.call_tool(
                    "dexec",
                    arguments={
                        "command": (
                            f"rm -f -- {quoted_cancel_sentinel}; sleep 3; "
                            f"printf 'late cancel write\\n' > {quoted_cancel_sentinel}"
                        ),
                        "timeout_sec": 10,
                    },
                    progress_callback=receive_cancel_progress,
                    meta=request_meta,
                )
            )
            await asyncio.wait_for(cancel_started.wait(), timeout=3)
            await session.send_notification(
                types.ClientNotification(
                    types.CancelledNotification(
                        params=types.CancelledNotificationParams(
                            requestId=cancel_request_id,
                            reason="live cancellation cleanup probe",
                        )
                    )
                )
            )
            with pytest.raises(McpError, match="Request cancelled"):
                await cancel_task

            cancel_verify_result = await session.call_tool(
                "dexec",
                arguments={
                    "command": (
                        "sleep 4; "
                        f"if [ -e {quoted_cancel_sentinel} ]; then "
                        f"rm -f -- {quoted_cancel_sentinel}; printf 'residual cancel process\\n'; exit 9; "
                        "else printf 'no residual cancel process\\n'; fi"
                    ),
                    "timeout_sec": 7,
                },
                meta=request_meta,
            )
            verify_result = await session.call_tool(
                "dexec",
                arguments={
                    "command": (
                        "sleep 2; "
                        f"if [ -e {quoted_sentinel} ]; then "
                        f"rm -f -- {quoted_sentinel}; printf 'residual process\\n'; exit 9; "
                        "else printf 'no residual process\\n'; fi"
                    ),
                    "timeout_sec": 5,
                },
                meta=request_meta,
            )

    text = "\n".join(str(getattr(content, "text", "")) for content in result.content)
    timeout_text = "\n".join(str(getattr(content, "text", "")) for content in timeout_result.content)
    natural_124_text = "\n".join(
        str(getattr(content, "text", "")) for content in natural_124_result.content
    )
    cancel_verify_text = "\n".join(
        str(getattr(content, "text", "")) for content in cancel_verify_result.content
    )
    verify_text = "\n".join(str(getattr(content, "text", "")) for content in verify_result.content)
    assert not result.isError
    assert "stdin=hello-through-mcp" in text
    assert "phase-one" in text
    assert "phase-two" in text
    assert "stderr:" not in text
    assert any("running" in message for message in progress_messages)
    assert any("running" in message for message in logs)
    assert "exit_code: timeout" in timeout_text
    assert "exit_code: 124" in natural_124_text
    assert "timed out" not in natural_124_text
    assert "no residual cancel process" in cancel_verify_text
    assert "exit_code: 0" in cancel_verify_text
    assert "no residual process" in verify_text
    assert "exit_code: 0" in verify_text
