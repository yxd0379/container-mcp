from __future__ import annotations

import os
import shlex
import uuid
from datetime import timedelta

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


SERVICE_URL = os.environ.get("MCP_DEXEC_URL", "http://127.0.0.1:9943/mcp")


pytestmark = pytest.mark.skipif(
    os.environ.get("MCP_DEXEC_LIVE") != "1",
    reason="set MCP_DEXEC_LIVE=1 to run the container patch integration through MCP",
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
async def test_live_apply_patch_uses_absolute_paths_and_preflights() -> None:
    run_id = str(uuid.uuid4())
    base = f"/tmp/mcp-dpatch-live-{run_id}"
    source = f"{base}/source.txt"
    removed = f"{base}/removed.txt"
    destination = f"{base}/moved file.txt"
    should_not_exist = f"{base}/should-not-exist.txt"
    request_meta = {"threadId": run_id}

    async with httpx.AsyncClient(trust_env=False) as http_client:
        async with streamable_http_client(
            SERVICE_URL,
            http_client=http_client,
        ) as (read_stream, write_stream, _), ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=30),
        ) as session:
            await session.initialize()
            tools = await session.list_tools()
            patch_tool = next(tool for tool in tools.tools if tool.name == "apply_patch")
            assert patch_tool.inputSchema["required"] == ["patch"]
            assert set(patch_tool.inputSchema["properties"]) == {"patch"}

            try:
                result = await session.call_tool(
                    "apply_patch",
                    arguments={
                        "patch": (
                            "*** Begin Patch\n"
                            f"*** Add File: {source}\n"
                            "+alpha\n"
                            f"*** Add File: {removed}\n"
                            "+remove me\n"
                            f"*** Update File: {source}\n"
                            f"*** Move to: {destination}\n"
                            "@@\n"
                            "-alpha\n"
                            "+beta\n"
                            f"*** Delete File: {removed}\n"
                            "*** End Patch"
                        )
                    },
                    meta=request_meta,
                )
                assert not result.isError, _text(result)
                assert f"M {source} -> {destination}" in _text(result)

                quoted_destination = shlex.quote(destination)
                quoted_source = shlex.quote(source)
                quoted_removed = shlex.quote(removed)
                verify = await session.call_tool(
                    "dexec",
                    arguments={
                        "command": (
                            f"test ! -e {quoted_source} && "
                            f"test ! -e {quoted_removed} && "
                            f"cat -- {quoted_destination}"
                        ),
                        "timeout_sec": 10,
                    },
                    meta=request_meta,
                )
                assert not verify.isError
                assert "status: ok" in _text(verify)
                assert "beta" in _text(verify)

                failed = await session.call_tool(
                    "apply_patch",
                    arguments={
                        "patch": (
                            "*** Begin Patch\n"
                            f"*** Add File: {should_not_exist}\n"
                            "+must not be written\n"
                            f"*** Update File: {destination}\n"
                            "@@\n"
                            "-missing context\n"
                            "+replacement\n"
                            "*** End Patch"
                        )
                    },
                    meta=request_meta,
                )
                assert failed.isError
                assert "context_mismatch" in _text(failed)

                quoted_should_not_exist = shlex.quote(should_not_exist)
                no_partial_write = await session.call_tool(
                    "dexec",
                    arguments={
                        "command": f"test ! -e {quoted_should_not_exist}",
                        "timeout_sec": 10,
                    },
                    meta=request_meta,
                )
                assert "status: ok" in _text(no_partial_write)

                read_only_path = f"/sys/mcp-dpatch-live-{run_id}.txt"
                write_failure = await session.call_tool(
                    "apply_patch",
                    arguments={
                        "patch": (
                            "*** Begin Patch\n"
                            f"*** Add File: {read_only_path}\n"
                            "+must fail\n"
                            "*** End Patch"
                        )
                    },
                    meta=request_meta,
                )
                assert write_failure.isError
                assert "write_failed" in _text(write_failure)
            finally:
                quoted_base = shlex.quote(base)
                await session.call_tool(
                    "dexec",
                    arguments={"command": f"rm -rf -- {quoted_base}", "timeout_sec": 10},
                    meta=request_meta,
                )
