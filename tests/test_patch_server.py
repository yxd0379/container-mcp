from __future__ import annotations

import asyncio

import pytest

import server
from patch_engine import PatchError, parse_patch


def test_prepare_patch_simulates_earlier_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[str] = []

    async def missing(path: str) -> bytes | None:
        reads.append(path)
        return None

    monkeypatch.setattr(server, "_read_patch_path", missing)
    operations = parse_patch(
        """*** Begin Patch
*** Add File: /tmp/new.txt
+before
*** Update File: /tmp/new.txt
@@
-before
+after
*** End Patch"""
    )

    prepared = asyncio.run(server._prepare_patch(operations))

    assert reads == ["/tmp/new.txt"]
    assert prepared[0].content == b"before\n"
    assert prepared[1].content == b"after\n"


def test_apply_patch_preflights_every_hunk_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {"/tmp/existing.txt": b"present\n"}
    writes: list[str] = []

    async def read(path: str) -> bytes | None:
        return files.get(path)

    async def write(path: str, content: bytes) -> None:
        del content
        writes.append(path)

    monkeypatch.setattr(server, "_read_patch_path", read)
    monkeypatch.setattr(server, "_write_patch_path", write)

    async def run() -> None:
        monkeypatch.setattr(server, "_patch_lock", asyncio.Lock())
        with pytest.raises(PatchError, match="Failed to find expected lines"):
            await server.apply_patch(
                """*** Begin Patch
*** Add File: /tmp/new.txt
+new
*** Update File: /tmp/existing.txt
@@
-missing
+changed
*** End Patch"""
            )

    asyncio.run(run())
    assert writes == []


def test_apply_patch_uses_one_process_global_serial_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    maximum_active = 0

    async def prepare(_operations: object) -> tuple[server._PreparedPatchOperation, ...]:
        return (server._PreparedPatchOperation("add", "/tmp/file", b"content"),)

    async def commit(_operations: object) -> str:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "ok"

    monkeypatch.setattr(server, "_prepare_patch", prepare)
    monkeypatch.setattr(server, "_commit_patch", commit)

    async def run() -> None:
        monkeypatch.setattr(server, "_patch_lock", asyncio.Lock())
        patches = [
            f"*** Begin Patch\n*** Add File: /tmp/{index}.txt\n+value\n*** End Patch"
            for index in range(3)
        ]
        assert await asyncio.gather(*(server.apply_patch(patch) for patch in patches)) == [
            "ok",
            "ok",
            "ok",
        ]

    asyncio.run(run())
    assert maximum_active == 1


def test_commit_failure_reports_files_already_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    async def write(path: str, _content: bytes) -> None:
        if path.endswith("second.txt"):
            raise PatchError("write_failed", "permission denied", path=path)

    monkeypatch.setattr(server, "_write_patch_path", write)
    operations = (
        server._PreparedPatchOperation("add", "/tmp/first.txt", b"first"),
        server._PreparedPatchOperation("add", "/tmp/second.txt", b"second"),
    )

    with pytest.raises(PatchError, match="Completed before the failure") as error:
        asyncio.run(server._commit_patch(operations))

    assert error.value.code == "partial_apply"
    assert "A /tmp/first.txt" in str(error.value)


def test_apply_patch_tool_schema_has_only_patch_argument() -> None:
    tool = server.server._tool_manager.get_tool("apply_patch")

    assert tool is not None
    assert tool.parameters["required"] == ["patch"]
    assert set(tool.parameters["properties"]) == {"patch"}
