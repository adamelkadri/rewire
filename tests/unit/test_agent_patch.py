"""Tests for candidate patch construction and diff rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.agents.patch import (
    MAX_EDIT_CHARS,
    CandidatePatch,
    FileChange,
    FileEdit,
    PatchBuilder,
    write_patch,
)
from rewire.core.errors import PatchError

SOURCE = "def ask(prompt, limit=10):\n    return create(max_tokens=limit)\n"


def builder(source: str = SOURCE) -> PatchBuilder:
    return PatchBuilder(read_file=lambda _path: source)


def test_an_edit_replaces_exactly_once() -> None:
    assert FileEdit(file="a.py", old_text="max_tokens", new_text="max_out").apply_to(
        SOURCE
    ) == SOURCE.replace("max_tokens", "max_out")


def test_missing_text_is_reported() -> None:
    with pytest.raises(PatchError, match="was not found"):
        FileEdit(file="a.py", old_text="absent", new_text="x").apply_to(SOURCE)


def test_ambiguous_text_is_refused_not_guessed() -> None:
    """Editing the wrong occurrence is the failure mode with no signal."""
    with pytest.raises(PatchError, match="ambiguous"):
        FileEdit(file="a.py", old_text="limit", new_text="cap").apply_to(SOURCE)


def test_the_error_previews_the_offending_text() -> None:
    with pytest.raises(PatchError) as info:
        FileEdit(file="a.py", old_text="absent", new_text="x").apply_to(SOURCE)
    assert info.value.details["old_text"] == "absent"


def test_edits_to_one_file_compose() -> None:
    patch_builder = builder()
    patch_builder.add(FileEdit(file="a.py", old_text="max_tokens", new_text="max_out"))
    patch_builder.add(FileEdit(file="a.py", old_text="prompt", new_text="text"))
    after = patch_builder.build().changes[0].after
    assert "max_out" in after
    assert "text" in after


def test_a_no_op_edit_is_refused() -> None:
    with pytest.raises(PatchError, match="change nothing"):
        builder().add(FileEdit(file="a.py", old_text="x", new_text="x"))


def test_oversized_edits_are_refused() -> None:
    huge = "y" * (MAX_EDIT_CHARS + 1)
    with pytest.raises(PatchError, match="too large"):
        builder().add(FileEdit(file="a.py", old_text="max_tokens", new_text=huge))


def test_edited_files_are_tracked() -> None:
    patch_builder = builder()
    patch_builder.add(FileEdit(file="a.py", old_text="max_tokens", new_text="max_out"))
    assert patch_builder.edited_files == ["a.py"]


# ------------------------------------------------------------------- diffs ---


def test_diff_describes_the_change() -> None:
    patch_builder = builder()
    patch_builder.add(FileEdit(file="a.py", old_text="max_tokens", new_text="max_out"))
    diff = patch_builder.build().unified_diff()
    assert "--- a/a.py" in diff
    assert "-    return create(max_tokens=limit)" in diff
    assert "+    return create(max_out=limit)" in diff


def test_diff_always_ends_with_a_newline() -> None:
    """Without this, consecutive file diffs concatenate into one broken line."""
    patch_builder = PatchBuilder(read_file=lambda _p: "x = 1")
    patch_builder.add(FileEdit(file="a.py", old_text="x = 1", new_text="x = 2"))
    assert patch_builder.build().unified_diff().endswith("\n")


def test_files_without_a_trailing_newline_get_the_git_marker() -> None:
    patch_builder = PatchBuilder(read_file=lambda _p: "x = 1")
    patch_builder.add(FileEdit(file="a.py", old_text="x = 1", new_text="x = 2"))
    assert "\\ No newline at end of file" in patch_builder.build().unified_diff()


def test_files_with_a_trailing_newline_do_not_get_the_marker() -> None:
    """Claiming a missing newline that is present makes `git apply` reject the patch."""
    patch_builder = PatchBuilder(read_file=lambda _p: "x = 1\n")
    patch_builder.add(FileEdit(file="a.py", old_text="x = 1", new_text="x = 2"))
    assert "No newline at end of file" not in patch_builder.build().unified_diff()


def test_line_counts() -> None:
    change = FileChange(file="a.py", before="a\nb\n", after="a\nc\nd\n")
    assert change.added_lines == 2
    assert change.removed_lines == 1


def test_patch_statistics() -> None:
    patch_builder = builder()
    patch_builder.add(FileEdit(file="a.py", old_text="max_tokens", new_text="max_out"))
    files, added, removed = patch_builder.build().stats()
    assert (files, added, removed) == (1, 1, 1)


def test_an_empty_patch_reports_itself() -> None:
    empty = CandidatePatch()
    assert empty.is_empty
    assert empty.files == []
    assert empty.unified_diff() == ""


def test_unchanged_files_are_excluded_from_the_diff() -> None:
    patch = CandidatePatch(changes=(FileChange(file="a.py", before="x", after="x"),))
    assert patch.is_empty
    assert patch.files == []


# ------------------------------------------------------------------ writing --


def test_writing_applies_only_changed_files(tmp_path: Path) -> None:
    patch = CandidatePatch(
        changes=(
            FileChange(file="a.py", before="x = 1\n", after="x = 2\n"),
            FileChange(file="b.py", before="same\n", after="same\n"),
        )
    )
    written = write_patch(patch, tmp_path)
    assert written == ["a.py"]
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    assert not (tmp_path / "b.py").exists()


def test_writing_creates_missing_directories(tmp_path: Path) -> None:
    patch = CandidatePatch(
        changes=(FileChange(file="deep/nested/a.py", before="", after="x = 1\n"),)
    )
    write_patch(patch, tmp_path)
    assert (tmp_path / "deep" / "nested" / "a.py").is_file()


def test_write_failures_are_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "a.py"
    blocker.mkdir()
    patch = CandidatePatch(changes=(FileChange(file="a.py", before="", after="x\n"),))
    with pytest.raises(PatchError, match="could not write"):
        write_patch(patch, tmp_path)


def test_writing_is_not_reachable_from_a_tool() -> None:
    """No agent tool may write. `write_patch` is called only by the sandbox."""
    from rewire.agents import tools

    source = Path(tools.__file__).read_text(encoding="utf-8")
    assert "write_patch" not in source
