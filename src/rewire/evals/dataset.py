"""Loading labelled impact-analysis datasets.

A dataset is a directory holding a repository, the two specifications that
changed around it, and a ``labels.json`` saying which locations a correct
analyser must report. The labels are an opinion, so every expected location
carries a written reason: a reader can disagree with the ground truth, which is
the only way ground truth stays honest.

Labels are attached to a *specific change* rather than to the dataset as a
whole. Crediting a location found for one change against another change's
expectations would inflate every metric.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from rewire.core.errors import EvaluationError

#: Filename holding a case's ground truth.
LABELS_FILENAME: Final[str] = "labels.json"

#: Subdirectory holding the repository under analysis.
REPO_DIRNAME: Final[str] = "repo"


class ExpectedLocation(BaseModel):
    """One location a correct analyser must report."""

    model_config = ConfigDict(frozen=True)

    file: str
    line: int
    #: Why this location is affected. Written for a human reviewing the labels.
    reason: str = ""

    @property
    def key(self) -> tuple[str, int]:
        """Identity used when comparing against predictions."""
        return (self.file, self.line)


class TargetChange(BaseModel):
    """A single change, with the locations it should be found to affect."""

    model_config = ConfigDict(frozen=True)

    change_type: str
    field_path: str | None = None
    endpoint: str | None = None
    expected: tuple[ExpectedLocation, ...] = ()

    @property
    def label(self) -> str:
        """Short identifier used in reports."""
        return f"{self.change_type}:{self.field_path or self.endpoint or '-'}"

    def expected_keys(self) -> set[tuple[str, int]]:
        """Positions a correct analyser must report."""
        return {location.key for location in self.expected}

    def expected_files(self) -> set[str]:
        """Files a correct analyser must report."""
        return {location.file for location in self.expected}


class ImpactCase(BaseModel):
    """A labelled evaluation case."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    #: Packages to attribute the API to. Empty means "let inference decide",
    #: which is itself worth evaluating.
    packages: tuple[str, ...] = ()
    targets: tuple[TargetChange, ...] = Field(default_factory=tuple)
    #: Absolute path to the case directory, filled in at load time.
    directory: Path

    @property
    def repository(self) -> Path:
        """Path to the repository under analysis."""
        return self.directory / REPO_DIRNAME

    @property
    def old_spec(self) -> Path:
        """Path to the previous specification."""
        return self.directory / "old.yaml"

    @property
    def new_spec(self) -> Path:
        """Path to the new specification."""
        return self.directory / "new.yaml"

    def target_for(self, change_type: str, field_path: str | None) -> TargetChange | None:
        """Find the labelled target matching a detected change, if any."""
        return next(
            (
                target
                for target in self.targets
                if target.change_type == change_type and target.field_path == field_path
            ),
            None,
        )


def load_case(directory: Path | str) -> ImpactCase:
    """Load one labelled case from a directory.

    Raises:
        EvaluationError: The directory is missing a required file, or the labels
            are malformed. Datasets are checked in and version controlled, so a
            broken one is a bug rather than a condition to work around.
    """
    path = Path(directory)
    labels = path / LABELS_FILENAME
    if not labels.is_file():
        raise EvaluationError("dataset case has no labels file", directory=str(path))

    try:
        document = json.loads(labels.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvaluationError(f"could not read labels: {exc}", path=str(labels)) from exc

    if not isinstance(document, dict):
        raise EvaluationError("labels file must contain an object", path=str(labels))

    case = ImpactCase.model_validate({**document, "directory": path})
    for required in (case.repository, case.old_spec, case.new_spec):
        if not required.exists():
            raise EvaluationError(
                "dataset case is incomplete", case=case.name, missing=required.name
            )
    return case


def load_cases(root: Path | str) -> list[ImpactCase]:
    """Load every case under ``root``, sorted by name.

    Raises:
        EvaluationError: ``root`` does not exist or contains no cases. An empty
            benchmark reporting a perfect score is worse than an error.
    """
    directory = Path(root)
    if not directory.is_dir():
        raise EvaluationError("dataset directory does not exist", path=str(directory))

    cases = [
        load_case(child)
        for child in sorted(directory.iterdir())
        if child.is_dir() and (child / LABELS_FILENAME).is_file()
    ]
    if not cases:
        raise EvaluationError("dataset directory contains no cases", path=str(directory))
    return sorted(cases, key=lambda case: case.name)


__all__ = [
    "LABELS_FILENAME",
    "REPO_DIRNAME",
    "ExpectedLocation",
    "ImpactCase",
    "TargetChange",
    "load_case",
    "load_cases",
]
