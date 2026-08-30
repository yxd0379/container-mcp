from __future__ import annotations

import asyncio

import pytest

from container_mcp import dpatch


def test_parse_all_operations_and_apply_update() -> None:
    operations = dpatch.parse_patch(
        """*** Begin Patch
*** Add File: /tmp/new.txt
+new
*** Delete File: /tmp/old.txt
*** Update File: /tmp/source.py
*** Move to: /tmp/destination.py
@@ def value():
-    return 1
+    return 2
*** End Patch"""
    )

    assert [operation.action for operation in operations] == ["add", "delete", "update"]
    assert operations[0].contents == "new\n"
    assert operations[2].move_path == "/tmp/destination.py"
    assert dpatch.apply_update(
        operations[2].path,
        "def value():\n    return 1\n",
        operations[2].chunks,
    ) == "def value():\n    return 2\n"


def test_parse_patch_rejects_relative_paths() -> None:
    with pytest.raises(dpatch.PatchError, match="absolute container paths") as error:
        dpatch.parse_patch(
            """*** Begin Patch
*** Add File: relative.txt
+content
*** End Patch"""
        )

    assert error.value.code == "invalid_path"
    assert error.value.line == 2


def test_apply_patch_preflights_every_hunk_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {"/tmp/existing.txt": b"present\n"}
    writes: list[str] = []

    async def read(_container: str, path: str) -> bytes | None:
        return files.get(path)

    async def write(_container: str, path: str, _content: bytes) -> None:
        writes.append(path)

    monkeypatch.setattr(dpatch, "_read_patch_path", read)
    monkeypatch.setattr(dpatch, "_write_patch_path", write)
    monkeypatch.setattr(dpatch, "_patch_locks", {})

    async def run() -> None:
        with pytest.raises(dpatch.PatchError, match="Failed to find expected lines"):
            await dpatch.apply_patch(
                """*** Begin Patch
*** Add File: /tmp/new.txt
+new
*** Update File: /tmp/existing.txt
@@
-missing
+changed
*** End Patch""",
                container="simjoin",
            )

    asyncio.run(run())
    assert writes == []


def test_apply_patch_serializes_calls_for_the_same_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    maximum_active = 0

    async def prepare(
        _container: str,
        _operations: tuple[dpatch.FileOperation, ...],
    ) -> tuple[dpatch._PreparedPatchOperation, ...]:
        return (dpatch._PreparedPatchOperation("add", "/tmp/file", b"content"),)

    async def commit(
        _container: str,
        _operations: tuple[dpatch._PreparedPatchOperation, ...],
    ) -> str:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    monkeypatch.setattr(dpatch, "_prepare_patch", prepare)
    monkeypatch.setattr(dpatch, "_commit_patch", commit)
    monkeypatch.setattr(dpatch, "_patch_locks", {})

    async def run() -> list[str]:
        patches = [
            f"*** Begin Patch\n*** Add File: /tmp/{index}.txt\n+value\n*** End Patch"
            for index in range(3)
        ]
        return await asyncio.gather(
            *(dpatch.apply_patch(patch, "simjoin") for patch in patches)
        )

    assert asyncio.run(run()) == ["ok", "ok", "ok"]
    assert maximum_active == 1


def test_commit_reports_operations_completed_before_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def write(_container: str, path: str, _content: bytes) -> None:
        if path.endswith("second.txt"):
            raise dpatch.PatchError("write_failed", "permission denied", path=path)

    monkeypatch.setattr(dpatch, "_write_patch_path", write)
    operations = (
        dpatch._PreparedPatchOperation("add", "/tmp/first.txt", b"first"),
        dpatch._PreparedPatchOperation("add", "/tmp/second.txt", b"second"),
    )

    with pytest.raises(dpatch.PatchError, match="Completed before the failure") as error:
        asyncio.run(dpatch._commit_patch("simjoin", operations))

    assert error.value.code == "partial_apply"
    assert error.value.path == "/tmp/second.txt"
    assert "A /tmp/first.txt" in str(error.value)
