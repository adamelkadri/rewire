"""Loading the migration benchmark: repositories, specifications, and hidden tests.

A case is a directory holding a repository, the two specifications that changed
around it, and — the part that makes the benchmark mean anything — a ``hidden/``
directory of tests the agent never sees.

**Why hidden tests.** Rewire grades a patch by running the repository's own test
suite. An agent given that suite has an obvious shortcut available: edit the
failing assertion. Some of that is legitimate — a migration genuinely has to
update tests that call the old API — which makes the shortcut impossible to
forbid by rule and impossible to detect by inspecting the diff.

So the benchmark does not try. It grades with a contract test written by the
dataset author, injected into the sandbox copy *after* the patch is applied and
never present in the workspace the agent could read. A patch that satisfies it
migrated the code; a patch that only edited the visible tests does not.

Every case also carries a written expectation and a reason, so the ground truth
can be disagreed with. Ground truth nobody can argue with is ground truth nobody
checked.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from rewire.core.errors import EvaluationError

#: Filename holding a case's metadata and expectation.
CASE_FILENAME: Final[str] = "case.json"

#: Subdirectory holding the repository the agent works on.
REPO_DIRNAME: Final[str] = "repo"

#: Subdirectory holding tests injected only at grading time.
HIDDEN_DIRNAME: Final[str] = "hidden"

OLD_SPEC: Final[str] = "old.yaml"
NEW_SPEC: Final[str] = "new.yaml"


class Expectation(StrEnum):
    """What a correct Rewire should do with a case."""

    #: A breaking change affects this repository and can be migrated.
    MIGRATE = "migrate"
    #: The specification changed but nothing here is affected. Touching the
    #: repository at all is the failure mode being measured.
    NO_OP = "no_op"


class MigrationCase(BaseModel):
    """One benchmark task."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    description: str
    expectation: Expectation
    #: Packages the API belongs to, passed to impact analysis.
    packages: tuple[str, ...] = ()
    #: Free-form labels for slicing results: change kind, repository shape,
    #: difficulty. Reported per tag so a headline number can be broken apart.
    tags: tuple[str, ...] = ()
    #: Why this expectation is correct, for a reader who wants to disagree.
    rationale: str = ""

    directory: Path = Field(exclude=True)

    @property
    def repository(self) -> Path:
        """The repository the agent is pointed at."""
        return self.directory / REPO_DIRNAME

    @property
    def old_spec(self) -> Path:
        """The specification before the change."""
        return self.directory / OLD_SPEC

    @property
    def new_spec(self) -> Path:
        """The specification after it."""
        return self.directory / NEW_SPEC

    def hidden_tests(self) -> dict[str, str]:
        """Files to inject at grading time, keyed by repository-relative path.

        Returns an empty mapping when a case has no hidden tests, which the
        runner reports rather than silently scoring as a pass.
        """
        root = self.directory / HIDDEN_DIRNAME
        if not root.is_dir():
            return {}
        overlay: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                overlay[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
        return overlay


def load_migration_case(directory: Path | str) -> MigrationCase:
    """Load one case from its directory.

    Raises:
        EvaluationError: The case is missing, malformed, or incomplete.
    """
    root = Path(directory)
    manifest = root / CASE_FILENAME
    if not manifest.is_file():
        raise EvaluationError("case manifest is missing", case=str(root), expected=CASE_FILENAME)

    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"could not read the case manifest: {exc}", case=str(root)) from exc

    for required in (REPO_DIRNAME,):
        if not (root / required).is_dir():
            raise EvaluationError("case is incomplete", case=str(root), missing=required)
    for required in (OLD_SPEC, NEW_SPEC):
        if not (root / required).is_file():
            raise EvaluationError("case is incomplete", case=str(root), missing=required)

    try:
        return MigrationCase(**payload, directory=root)
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"case manifest is invalid: {exc}", case=str(root)) from exc


def load_migration_cases(root: Path | str) -> list[MigrationCase]:
    """Load every case under a dataset directory, sorted by identifier.

    Raises:
        EvaluationError: The directory is missing or holds no cases.
    """
    base = Path(root)
    if not base.is_dir():
        raise EvaluationError("dataset directory does not exist", path=str(base))

    cases = [
        load_migration_case(entry)
        for entry in sorted(base.iterdir())
        if entry.is_dir() and (entry / CASE_FILENAME).is_file()
    ]
    if not cases:
        raise EvaluationError("dataset directory holds no cases", path=str(base))

    identifiers = [case.case_id for case in cases]
    duplicates = {name for name in identifiers if identifiers.count(name) > 1}
    if duplicates:
        raise EvaluationError("duplicate case identifiers", duplicates=sorted(duplicates))
    return sorted(cases, key=lambda case: case.case_id)


__all__ = [
    "CASE_FILENAME",
    "HIDDEN_DIRNAME",
    "NEW_SPEC",
    "OLD_SPEC",
    "REPO_DIRNAME",
    "Expectation",
    "MigrationCase",
    "load_migration_case",
    "load_migration_cases",
]
