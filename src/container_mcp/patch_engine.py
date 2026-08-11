from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass


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
