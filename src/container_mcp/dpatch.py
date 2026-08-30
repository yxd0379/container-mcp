from __future__ import annotations

import asyncio
import posixpath
import re
from dataclasses import dataclass

import anyio

from .dexec import CONTAINER_KILL_AFTER_SEC, HOST_TIMEOUT_OVERHEAD_SEC, terminate_process


BEGIN_PATCH = "*** Begin Patch"
END_PATCH = "*** End Patch"
ADD_FILE = "*** Add File: "
DELETE_FILE = "*** Delete File: "
UPDATE_FILE = "*** Update File: "
MOVE_TO = "*** Move to: "
END_OF_FILE = "*** End of File"
FILE_MARKERS = (ADD_FILE, DELETE_FILE, UPDATE_FILE)


class PatchError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        line: int | None = None,
        path: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.line = line
        self.path = path
        super().__init__(message)

    def __str__(self) -> str:
        details = [f"code={self.code}"]
        if self.line is not None:
            details.append(f"line={self.line}")
        if self.path is not None:
            details.append(f"path={self.path}")
        return f"{self.message} ({', '.join(details)})"


@dataclass(frozen=True)
class UpdateChunk:
    change_context: str | None
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    is_end_of_file: bool = False


@dataclass(frozen=True)
class FileOperation:
    action: str
    path: str
    contents: str | None = None
    move_path: str | None = None
    chunks: tuple[UpdateChunk, ...] = ()


@dataclass
class _ChunkBuilder:
    change_context: str | None
    old_lines: list[str]
    new_lines: list[str]
    is_end_of_file: bool = False

    @classmethod
    def create(cls, change_context: str | None) -> _ChunkBuilder:
        return cls(change_context=change_context, old_lines=[], new_lines=[])

    def finish(self, line: int) -> UpdateChunk:
        if not self.old_lines and not self.new_lines:
            raise PatchError("invalid_hunk", "Update hunk is empty", line=line)
        return UpdateChunk(
            change_context=self.change_context,
            old_lines=tuple(self.old_lines),
            new_lines=tuple(self.new_lines),
            is_end_of_file=self.is_end_of_file,
        )


def _absolute_path(raw_path: str, line: int) -> str:
    path = raw_path.strip()
    if not path:
        raise PatchError("invalid_path", "Patch path must not be empty", line=line)
    if "\x00" in path:
        raise PatchError("invalid_path", "Patch path must not contain NUL", line=line)
    if not path.startswith("/"):
        raise PatchError(
            "invalid_path",
            "Patch paths must be absolute container paths",
            line=line,
            path=path,
        )
    return posixpath.normpath(path)


def _is_file_marker(line: str) -> bool:
    return line.startswith(FILE_MARKERS)


def parse_patch(patch: str) -> tuple[FileOperation, ...]:
    try:
        patch.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PatchError("invalid_patch", "Patch must be valid UTF-8 text") from exc

    lines = patch.strip().splitlines()
    if not lines or lines[0].strip() != BEGIN_PATCH:
        raise PatchError("invalid_patch", f"The first line must be '{BEGIN_PATCH}'", line=1)
    if len(lines) < 2 or lines[-1].strip() != END_PATCH:
        raise PatchError(
            "invalid_patch",
            f"The last line must be '{END_PATCH}'",
            line=max(1, len(lines)),
        )

    operations: list[FileOperation] = []
    index = 1
    end_index = len(lines) - 1
    while index < end_index:
        line = lines[index]
        line_number = index + 1
        if line.startswith(ADD_FILE):
            path = _absolute_path(line[len(ADD_FILE) :], line_number)
            index += 1
            content_lines: list[str] = []
            while index < end_index and not _is_file_marker(lines[index]):
                value = lines[index]
                if not value.startswith("+"):
                    raise PatchError(
                        "invalid_hunk",
                        "Every Add File content line must start with '+'",
                        line=index + 1,
                        path=path,
                    )
                content_lines.append(value[1:])
                index += 1
            if not content_lines:
                raise PatchError(
                    "invalid_hunk",
                    "Add File requires at least one '+' content line",
                    line=line_number,
                    path=path,
                )
            operations.append(
                FileOperation(action="add", path=path, contents="\n".join(content_lines) + "\n")
            )
            continue

        if line.startswith(DELETE_FILE):
            path = _absolute_path(line[len(DELETE_FILE) :], line_number)
            operations.append(FileOperation(action="delete", path=path))
            index += 1
            continue

        if line.startswith(UPDATE_FILE):
            path = _absolute_path(line[len(UPDATE_FILE) :], line_number)
            index += 1
            move_path: str | None = None
            if index < end_index and lines[index].startswith(MOVE_TO):
                move_path = _absolute_path(lines[index][len(MOVE_TO) :], index + 1)
                index += 1

            chunks: list[UpdateChunk] = []
            current: _ChunkBuilder | None = None
            while index < end_index and not _is_file_marker(lines[index]):
                value = lines[index]
                current_line = index + 1
                if value == "@@" or value.startswith("@@ "):
                    if current is not None:
                        chunks.append(current.finish(current_line - 1))
                    context = None if value == "@@" else value[3:]
                    if context is not None and re.match(r"-\d+(?:,\d+)?\s+\+\d+", context):
                        raise PatchError(
                            "invalid_hunk",
                            "Codex patch '@@' headers use context text, not unified-diff line numbers",
                            line=current_line,
                            path=path,
                        )
                    current = _ChunkBuilder.create(context)
                    index += 1
                    continue
                if value == END_OF_FILE:
                    if current is None:
                        raise PatchError(
                            "invalid_hunk",
                            "End of File must follow an update hunk",
                            line=current_line,
                            path=path,
                        )
                    current.is_end_of_file = True
                    index += 1
                    if index < end_index and not _is_file_marker(lines[index]):
                        raise PatchError(
                            "invalid_hunk",
                            "End of File must be the last line of a file update",
                            line=index + 1,
                            path=path,
                        )
                    continue
                if not value or value[0] not in " +-":
                    raise PatchError(
                        "invalid_hunk",
                        "Update lines must start with space, '+' or '-'",
                        line=current_line,
                        path=path,
                    )
                if current is None:
                    current = _ChunkBuilder.create(None)
                marker, text = value[0], value[1:]
                if marker in " -":
                    current.old_lines.append(text)
                if marker in " +":
                    current.new_lines.append(text)
                index += 1

            if current is not None:
                chunks.append(current.finish(index))
            if not chunks and move_path is None:
                raise PatchError(
                    "invalid_hunk",
                    "Update File requires a hunk or Move to",
                    line=line_number,
                    path=path,
                )
            operations.append(
                FileOperation(
                    action="update",
                    path=path,
                    move_path=move_path,
                    chunks=tuple(chunks),
                )
            )
            continue

        raise PatchError(
            "invalid_patch",
            "Expected Add File, Delete File or Update File marker",
            line=line_number,
        )

    if not operations:
        raise PatchError("invalid_patch", "No files were modified")
    return tuple(operations)


_UNICODE_TRANSLATION = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "\u00a0": " ",
        "\u2002": " ",
        "\u2003": " ",
        "\u2004": " ",
        "\u2005": " ",
        "\u2006": " ",
        "\u2007": " ",
        "\u2008": " ",
        "\u2009": " ",
        "\u200a": " ",
        "\u202f": " ",
        "\u205f": " ",
        "\u3000": " ",
    }
)


def _seek_sequence(
    lines: list[str],
    pattern: tuple[str, ...] | list[str],
    start: int,
    *,
    eof: bool,
) -> int | None:
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    search_start = len(lines) - len(pattern) if eof else start
    if search_start > len(lines) - len(pattern):
        return None

    transforms = (
        lambda value: value,
        lambda value: value.rstrip(),
        lambda value: value.strip(),
        lambda value: value.strip().translate(_UNICODE_TRANSLATION),
    )
    for transform in transforms:
        transformed_pattern = [transform(value) for value in pattern]
        for index in range(search_start, len(lines) - len(pattern) + 1):
            if [transform(value) for value in lines[index : index + len(pattern)]] == transformed_pattern:
                return index
    return None


def apply_update(path: str, original: str, chunks: tuple[UpdateChunk, ...]) -> str:
    original_lines = original.split("\n")
    if original_lines and original_lines[-1] == "":
        original_lines.pop()

    replacements: list[tuple[int, int, list[str]]] = []
    line_index = 0
    for chunk in chunks:
        if chunk.change_context is not None:
            context_index = _seek_sequence(
                original_lines,
                (chunk.change_context,),
                line_index,
                eof=False,
            )
            if context_index is None:
                raise PatchError(
                    "context_mismatch",
                    f"Failed to find context '{chunk.change_context}'",
                    path=path,
                )
            line_index = context_index + 1

        if not chunk.old_lines:
            replacements.append((len(original_lines), 0, list(chunk.new_lines)))
            continue

        pattern = chunk.old_lines
        new_lines = chunk.new_lines
        match_index = _seek_sequence(
            original_lines,
            pattern,
            line_index,
            eof=chunk.is_end_of_file,
        )
        if match_index is None and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_lines and new_lines[-1] == "":
                new_lines = new_lines[:-1]
            match_index = _seek_sequence(
                original_lines,
                pattern,
                line_index,
                eof=chunk.is_end_of_file,
            )
        if match_index is None:
            expected = "\n".join(chunk.old_lines)
            raise PatchError(
                "context_mismatch",
                f"Failed to find expected lines:\n{expected}",
                path=path,
            )
        replacements.append((match_index, len(pattern), list(new_lines)))
        line_index = match_index + len(pattern)

    for start, old_length, new_segment in sorted(replacements, reverse=True):
        original_lines[start : start + old_length] = new_segment
    if not original_lines or original_lines[-1] != "":
        original_lines.append("")
    return "\n".join(original_lines)


PATCH_COMMAND_TIMEOUT_SEC = 30
PATCH_MISSING_EXIT_CODE = 44
PATCH_DIRECTORY_EXIT_CODE = 45

_PATCH_READ = """
export LC_ALL=C
path=$1
if [ -d "$path" ]; then
    printf 'path is a directory: %s\n' "$path" >&2
    exit 45
fi
if [ ! -e "$path" ] && [ ! -L "$path" ]; then
    exit 44
fi
cat -- "$path"
""".strip()

_PATCH_WRITE = """
set -eu
export LC_ALL=C
path=$1
expected_size=$2
parent=${path%/*}
if [ -z "$parent" ]; then
    parent=/
fi
if [ -d "$path" ]; then
    printf 'path is a directory: %s\n' "$path" >&2
    exit 45
fi
mkdir -p -- "$parent"
tmp=$(mktemp "$parent/.dpatch.XXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
cat > "$tmp"
actual_size=$(wc -c < "$tmp")
if [ "$actual_size" -ne "$expected_size" ]; then
    printf 'incomplete stdin: expected %s bytes, received %s\n' "$expected_size" "$actual_size" >&2
    exit 46
fi
if [ -e "$path" ] || [ -L "$path" ]; then
    chmod --reference="$path" "$tmp"
else
    chmod 0644 "$tmp"
fi
mv -fT -- "$tmp" "$path"
trap - EXIT HUP INT TERM
""".strip()

_PATCH_DELETE = """
set -eu
export LC_ALL=C
path=$1
if [ -d "$path" ]; then
    printf 'path is a directory: %s\n' "$path" >&2
    exit 45
fi
rm -- "$path"
""".strip()

_patch_locks: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class _PatchCommandResult:
    exit_code: int
    stdout: bytes
    stderr: str


@dataclass(frozen=True)
class _PreparedPatchOperation:
    action: str
    path: str
    content: bytes | None = None
    move_path: str | None = None


async def _run_patch_shell(
    container: str,
    script: str,
    path: str,
    *,
    extra_args: tuple[str, ...] = (),
    stdin: bytes | None = None,
) -> _PatchCommandResult:
    args = ["docker", "exec"]
    if stdin is not None:
        args.append("-i")
    args.extend(
        (
            container,
            "timeout",
            "--signal=TERM",
            f"--kill-after={CONTAINER_KILL_AFTER_SEC}s",
            f"{PATCH_COMMAND_TIMEOUT_SEC}s",
            "bash",
            "-c",
            script,
            "dpatch",
            path,
            *extra_args,
        )
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise PatchError(
            "container_unavailable",
            f"Could not execute Docker for container {container}: {exc}",
            path=path,
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin),
            timeout=PATCH_COMMAND_TIMEOUT_SEC + HOST_TIMEOUT_OVERHEAD_SEC,
        )
    except asyncio.CancelledError:
        if process.returncode is None:
            await terminate_process(process)
        raise
    except asyncio.TimeoutError as exc:
        if process.returncode is None:
            await terminate_process(process)
        raise PatchError(
            "container_timeout",
            f"Container patch operation timed out after {PATCH_COMMAND_TIMEOUT_SEC}s",
            path=path,
        ) from exc

    assert process.returncode is not None
    return _PatchCommandResult(
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr.decode("utf-8", errors="replace").strip(),
    )


def _patch_failure_message(operation: str, path: str, result: _PatchCommandResult) -> str:
    detail = result.stderr or f"container command exited with code {result.exit_code}"
    return f"Failed to {operation} {path}: {detail}"


async def _read_patch_path(container: str, path: str) -> bytes | None:
    result = await _run_patch_shell(container, _PATCH_READ, path)
    if result.exit_code == 0:
        return result.stdout
    if result.exit_code == PATCH_MISSING_EXIT_CODE:
        return None
    if result.exit_code == PATCH_DIRECTORY_EXIT_CODE:
        raise PatchError("path_is_directory", result.stderr, path=path)
    raise PatchError("read_failed", _patch_failure_message("read", path, result), path=path)


async def _prepare_patch(
    container: str,
    operations: tuple[FileOperation, ...],
) -> tuple[_PreparedPatchOperation, ...]:
    virtual_files: dict[str, bytes | None] = {}

    async def read(path: str) -> bytes | None:
        if path not in virtual_files:
            virtual_files[path] = await _read_patch_path(container, path)
        return virtual_files[path]

    prepared: list[_PreparedPatchOperation] = []
    for operation in operations:
        path = operation.path
        if operation.action == "add":
            await read(path)
            assert operation.contents is not None
            content = operation.contents.encode("utf-8")
            prepared.append(_PreparedPatchOperation("add", path, content=content))
            virtual_files[path] = content
            continue

        if operation.action == "delete":
            if await read(path) is None:
                raise PatchError("file_not_found", "File to delete does not exist", path=path)
            prepared.append(_PreparedPatchOperation("delete", path))
            virtual_files[path] = None
            continue

        if operation.action != "update":
            raise PatchError("invalid_patch", f"Unsupported patch action: {operation.action}")

        original = await read(path)
        if original is None:
            raise PatchError("file_not_found", "File to update does not exist", path=path)
        if operation.chunks:
            try:
                original_text = original.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PatchError(
                    "invalid_utf8",
                    "File to update is not valid UTF-8 text",
                    path=path,
                ) from exc
            new_content = apply_update(path, original_text, operation.chunks).encode("utf-8")
        else:
            new_content = original

        move_path = operation.move_path
        if move_path is not None and move_path != path:
            await read(move_path)
            prepared.append(
                _PreparedPatchOperation(
                    "move",
                    path,
                    content=new_content,
                    move_path=move_path,
                )
            )
            virtual_files[path] = None
            virtual_files[move_path] = new_content
        else:
            prepared.append(_PreparedPatchOperation("update", path, content=new_content))
            virtual_files[path] = new_content

    return tuple(prepared)


async def _write_patch_path(container: str, path: str, content: bytes) -> None:
    result = await _run_patch_shell(
        container,
        _PATCH_WRITE,
        path,
        extra_args=(str(len(content)),),
        stdin=content,
    )
    if result.exit_code == 0:
        return
    code = "path_is_directory" if result.exit_code == PATCH_DIRECTORY_EXIT_CODE else "write_failed"
    raise PatchError(code, _patch_failure_message("write", path, result), path=path)


async def _delete_patch_path(container: str, path: str) -> None:
    result = await _run_patch_shell(container, _PATCH_DELETE, path)
    if result.exit_code == 0:
        return
    code = "path_is_directory" if result.exit_code == PATCH_DIRECTORY_EXIT_CODE else "delete_failed"
    raise PatchError(code, _patch_failure_message("delete", path, result), path=path)


async def _commit_patch(
    container: str,
    operations: tuple[_PreparedPatchOperation, ...],
) -> str:
    applied: list[str] = []
    try:
        for operation in operations:
            if operation.action in {"add", "update"}:
                assert operation.content is not None
                await _write_patch_path(container, operation.path, operation.content)
                applied.append(f"{'A' if operation.action == 'add' else 'M'} {operation.path}")
            elif operation.action == "delete":
                await _delete_patch_path(container, operation.path)
                applied.append(f"D {operation.path}")
            elif operation.action == "move":
                assert operation.content is not None
                assert operation.move_path is not None
                await _write_patch_path(container, operation.move_path, operation.content)
                applied.append(f"A {operation.move_path} (move destination)")
                await _delete_patch_path(container, operation.path)
                applied[-1] = f"M {operation.path} -> {operation.move_path}"
            else:
                raise PatchError(
                    "invalid_patch",
                    f"Unsupported prepared action: {operation.action}",
                    path=operation.path,
                )
    except PatchError as exc:
        if not applied:
            raise
        completed = "\n".join(f"  {entry}" for entry in applied)
        raise PatchError(
            "partial_apply",
            f"{exc.message}\nCompleted before the failure:\n{completed}",
            path=exc.path,
        ) from exc

    return "Success. Updated the following files:\n" + "\n".join(applied)


def _patch_lock_for(container: str) -> asyncio.Lock:
    lock = _patch_locks.get(container)
    if lock is None:
        lock = asyncio.Lock()
        _patch_locks[container] = lock
    return lock


async def apply_patch(patch: str, container: str) -> str:
    """Apply a patch to an already validated container."""
    async with _patch_lock_for(container):
        operations = parse_patch(patch)
        prepared = await _prepare_patch(container, operations)
        with anyio.CancelScope(shield=True):
            return await _commit_patch(container, prepared)


async def dpatch(patch: str, container: str) -> str:
    """Resolve the allowed container and apply a preflighted Codex patch."""
    from . import server as runtime

    return await apply_patch(patch, runtime.resolve_container(container))
