"""Representing and applying a candidate migration patch.

Edits are **exact string replacements**, not model-generated unified diffs.
Asking a model to emit a diff means asking it to count lines and reproduce
context exactly; it gets that wrong often enough that a large share of any such
agent's failures are malformed hunks rather than bad reasoning. A replacement
carries no line numbers and no context to miscount, so it either matches or it
does not — and when it does not, the model gets a precise error it can act on.

The unified diff a human reviews is computed by Rewire from the before and after
text, so it is always well formed.

Ambiguity is an error, not a coin flip: an ``old_text`` occurring more than once
is rejected rather than resolved by picking the first. Silently editing the
wrong occurrence is the failure mode with no signal.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from rewire.core.errors import PatchError

#: Longest replacement string accepted, to stop a model pasting a whole file.
MAX_EDIT_CHARS = 20_000


class FileEdit(BaseModel):
    """One exact-string replacement within one file."""

    model_config = ConfigDict(frozen=True)

    file: str
    #: Text to find. Must occur exactly once in the file.
    old_text: str
    new_text: str
    #: Why the agent made this edit, for the human reviewing the patch.
    rationale: str = ""

    def apply_to(self, source: str) -> str:
        """Return ``source`` with this edit applied.

        Raises:
            PatchError: The text is absent, or occurs more than once.
        """
        occurrences = source.count(self.old_text)
        if occurrences == 0:
            raise PatchError(
                "text to replace was not found in the file",
                file=self.file,
                old_text=_preview(self.old_text),
            )
        if occurrences > 1:
            raise PatchError(
                "text to replace is ambiguous; include surrounding lines to make it unique",
                file=self.file,
                occurrences=occurrences,
                old_text=_preview(self.old_text),
            )
        return source.replace(self.old_text, self.new_text, 1)


def _preview(text: str, limit: int = 120) -> str:
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else f"{flattened[:limit]}..."


class FileChange(BaseModel):
    """The complete before and after of one changed file."""

    model_config = ConfigDict(frozen=True)

    file: str
    before: str
    after: str
    edits: tuple[FileEdit, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether the file's content actually differs."""
        return self.before != self.after

    def unified_diff(self, *, context: int = 3) -> str:
        r"""Render this change as a unified diff.

        Computed here rather than authored by the model, so the diff is always
        well formed and always describes what would really happen.

        A file whose final line has no trailing newline gets git's
        ``\\ No newline at end of file`` marker. Without it the last ``-`` and
        ``+`` lines concatenate into one unreadable line, and consecutive file
        diffs run together — which is exactly what the first live agent run
        produced. The marker also keeps the output applicable by ``git apply``,
        which Phase 11 depends on.
        """
        lines: list[str] = []
        for line in difflib.unified_diff(
            self.before.splitlines(keepends=True),
            self.after.splitlines(keepends=True),
            fromfile=f"a/{self.file}",
            tofile=f"b/{self.file}",
            n=context,
        ):
            lines.append(line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n")
        return "".join(lines)

    @property
    def added_lines(self) -> int:
        """Lines added by this change."""
        return sum(
            1
            for line in self.unified_diff().splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )

    @property
    def removed_lines(self) -> int:
        """Lines removed by this change."""
        return sum(
            1
            for line in self.unified_diff().splitlines()
            if line.startswith("-") and not line.startswith("---")
        )


class CandidatePatch(BaseModel):
    """A proposed migration.

    Deliberately named *candidate*. Phase 4 has no way to establish that a patch
    is correct — that requires executing it, which is Phase 5. Anything here is
    a proposal awaiting evidence, and the type name is the reminder.
    """

    model_config = ConfigDict(frozen=True)

    changes: tuple[FileChange, ...] = ()
    #: The agent's own summary. Explanatory only; it grants no assurance.
    summary: str = ""

    @property
    def files(self) -> list[str]:
        """Files this patch changes, sorted."""
        return sorted(change.file for change in self.changes if change.changed)

    @property
    def is_empty(self) -> bool:
        """Whether the patch would change nothing."""
        return not any(change.changed for change in self.changes)

    def unified_diff(self) -> str:
        """The whole patch as one unified diff."""
        return "".join(change.unified_diff() for change in self.changes if change.changed)

    def stats(self) -> tuple[int, int, int]:
        """Return ``(files, lines added, lines removed)``."""
        changed = [change for change in self.changes if change.changed]
        return (
            len(changed),
            sum(change.added_lines for change in changed),
            sum(change.removed_lines for change in changed),
        )


class PatchBuilder:
    """Accumulates edits per file and produces a :class:`CandidatePatch`.

    Holds the working copy of each file in memory, so several edits to one file
    compose without touching disk. Nothing is written by this class at all.
    """

    def __init__(self, read_file: Callable[[str], str]) -> None:
        self._read_file = read_file
        self._original: dict[str, str] = {}
        self._current: dict[str, str] = {}
        self._edits: dict[str, list[FileEdit]] = {}

    def add(self, edit: FileEdit) -> None:
        """Apply an edit to the in-memory working copy.

        Raises:
            PatchError: The edit is oversized, or its text is missing or
                ambiguous in the file.
        """
        if len(edit.old_text) > MAX_EDIT_CHARS or len(edit.new_text) > MAX_EDIT_CHARS:
            raise PatchError("edit is too large", file=edit.file, limit_chars=MAX_EDIT_CHARS)
        if edit.old_text == edit.new_text:
            raise PatchError("edit would change nothing", file=edit.file)

        if edit.file not in self._original:
            source = self._read_file(edit.file)
            self._original[edit.file] = source
            self._current[edit.file] = source

        self._current[edit.file] = edit.apply_to(self._current[edit.file])
        self._edits.setdefault(edit.file, []).append(edit)

    @property
    def edited_files(self) -> list[str]:
        """Files with at least one applied edit."""
        return sorted(self._edits)

    def build(self, summary: str = "") -> CandidatePatch:
        """Assemble the accumulated edits into a candidate patch."""
        return CandidatePatch(
            changes=tuple(
                FileChange(
                    file=path,
                    before=self._original[path],
                    after=self._current[path],
                    edits=tuple(self._edits.get(path, ())),
                )
                for path in sorted(self._current)
            ),
            summary=summary,
        )


def write_patch(patch: CandidatePatch, root: Path | str) -> list[str]:
    """Write a candidate patch into a working tree.

    Kept out of :class:`PatchBuilder` and out of the tool layer on purpose: an
    agent tool must never be able to reach this. Phase 5 calls it against a
    disposable copy inside a sandbox, never against the user's checkout.

    Returns:
        The files written, repository-relative.

    Raises:
        PatchError: A file could not be written.
    """
    base = Path(root)
    written: list[str] = []
    for change in patch.changes:
        if not change.changed:
            continue
        target = base / change.file
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.after, encoding="utf-8")
        except OSError as exc:
            raise PatchError(f"could not write file: {exc}", file=change.file) from exc
        written.append(change.file)
    return written


__all__ = [
    "MAX_EDIT_CHARS",
    "CandidatePatch",
    "FileChange",
    "FileEdit",
    "PatchBuilder",
    "write_patch",
]
