from __future__ import annotations

import pytest

from patch_engine import PatchError, apply_update, parse_patch


def test_parse_patch_supports_all_codex_file_operations() -> None:
    operations = parse_patch(
        """*** Begin Patch
*** Add File: /tmp/new file.txt
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
    assert operations[0].path == "/tmp/new file.txt"
    assert operations[0].contents == "new\n"
    assert operations[2].move_path == "/tmp/destination.py"
    assert operations[2].chunks[0].change_context == "def value():"


def test_parse_patch_requires_absolute_container_paths() -> None:
    with pytest.raises(PatchError, match="absolute container paths") as error:
        parse_patch(
            """*** Begin Patch
*** Add File: relative.txt
+content
*** End Patch"""
        )

    assert error.value.code == "invalid_path"
    assert error.value.line == 2


def test_parse_patch_explains_unified_diff_line_numbers() -> None:
    with pytest.raises(PatchError, match="not unified-diff line numbers") as error:
        parse_patch(
            """*** Begin Patch
*** Update File: /tmp/file.txt
@@ -1,2 +1,2 @@
-old
+new
*** End Patch"""
        )

    assert error.value.code == "invalid_hunk"
    assert error.value.line == 3


def test_apply_update_matches_context_and_whitespace_like_codex() -> None:
    operation = parse_patch(
        """*** Begin Patch
*** Update File: /tmp/file.py
@@ class Example:
     def value(self):
-        return 1
+        return 2
*** End Patch"""
    )[0]

    result = apply_update(
        operation.path,
        "class Example:\n  def value(self):   \n    return 1\n",
        operation.chunks,
    )

    assert result == "class Example:\n    def value(self):\n        return 2\n"


def test_apply_update_preserves_order_across_multiple_hunks() -> None:
    operation = parse_patch(
        """*** Begin Patch
*** Update File: /tmp/file.txt
@@
-one
+ONE
 two
@@
 three
-four
+FOUR
*** End of File
*** End Patch"""
    )[0]

    result = apply_update(
        operation.path,
        "one\ntwo\nthree\nfour\n",
        operation.chunks,
    )

    assert result == "ONE\ntwo\nthree\nFOUR\n"


def test_apply_update_returns_precise_context_error() -> None:
    operation = parse_patch(
        """*** Begin Patch
*** Update File: /tmp/file.txt
@@
-missing
+new
*** End Patch"""
    )[0]

    with pytest.raises(PatchError, match="Failed to find expected lines") as error:
        apply_update(operation.path, "present\n", operation.chunks)

    assert error.value.code == "context_mismatch"
    assert error.value.path == "/tmp/file.txt"
