"""Typed models for impact analysis.

An impact report answers "which lines does this API change break, and why?".
The "why" is not decoration: every location carries the individual signals that
produced its score, so a ranking can be audited, a bad weight can be found, and
Phase 4's agent can be told what the evidence was rather than just a number.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from rewire.changes.models import ApiChange, Severity


class SignalKind(StrEnum):
    """An independent piece of evidence about whether a location is affected."""

    #: How the name appears in the source: keyword argument, dict key, and so on.
    REFERENCE_KIND = "reference_kind"
    #: The reference is an argument to a call that resolves into the SDK.
    SDK_CALL_TARGET = "sdk_call_target"
    #: The file imports the package the changed API belongs to.
    PACKAGE_IMPORTED = "package_imported"
    #: The file does not import it, despite naming the field.
    PACKAGE_NOT_IMPORTED = "package_not_imported"
    #: The file imports a module of this repository that does use the SDK, so it
    #: is one hop from the API even though it never imports the package itself.
    IMPORTS_SDK_MODULE = "imports_sdk_module"
    #: The package is a declared dependency of the repository.
    PACKAGE_DECLARED = "package_declared"
    #: The repository shows no sign of the package anywhere: not declared, not
    #: imported by any file.
    PACKAGE_ABSENT = "package_absent"
    #: The changed endpoint's path appears literally in the file.
    ENDPOINT_MENTIONED = "endpoint_mentioned"
    #: The enclosing function also calls the SDK, so the name is in context.
    ENCLOSING_SDK_USAGE = "enclosing_sdk_usage"
    #: The location is in test code.
    TEST_CODE = "test_code"
    #: The call forwards **kwargs, hiding arguments from static analysis.
    OPAQUE_ARGUMENTS = "opaque_arguments"
    #: The way the name is used agrees with the direction the field travels.
    DIRECTION_CONSISTENT = "direction_consistent"
    #: It disagrees: a response field cannot be set by constructing a dict.
    DIRECTION_INCONSISTENT = "direction_inconsistent"


class Signal(BaseModel):
    """One piece of evidence, with the weight it contributed.

    Weights are in log-odds, so they add. Positive values argue the location is
    affected, negative values argue it is not.
    """

    model_config = ConfigDict(frozen=True)

    kind: SignalKind
    weight: float
    detail: str = ""


class MatchStrategy(StrEnum):
    """How a candidate location was proposed in the first place."""

    #: The changed field's name occurs at this location.
    FIELD_REFERENCE = "field_reference"
    #: The changed endpoint's path occurs at this location.
    ENDPOINT_PATH = "endpoint_path"
    #: The location calls into the SDK operation the change belongs to.
    SDK_CALL = "sdk_call"


class AffectedLocation(BaseModel):
    """One place in the repository a change is believed to affect."""

    model_config = ConfigDict(frozen=True)

    file: str
    line: int
    column: int = 0
    #: Qualified name of the enclosing function or class, when there is one.
    symbol: str | None = None
    #: Confidence in ``[0, 1]``, the sigmoid of the summed signal weights.
    confidence: float = Field(ge=0.0, le=1.0)
    strategy: MatchStrategy
    #: The evidence behind ``confidence``, strongest contribution first.
    signals: tuple[Signal, ...] = ()
    #: Source line, for display. Never parsed.
    snippet: str | None = None
    is_test: bool = False

    @property
    def logit(self) -> float:
        """Summed signal weight, before the sigmoid."""
        return sum(signal.weight for signal in self.signals)

    @property
    def location(self) -> str:
        """``path:line``, the form editors and terminals link on."""
        return f"{self.file}:{self.line}"

    @property
    def sort_key(self) -> tuple[float, str, int]:
        """Most confident first, then stable by position."""
        return (-self.confidence, self.file, self.line)


class ChangeImpact(BaseModel):
    """Everything believed to be affected by a single API change."""

    model_config = ConfigDict(frozen=True)

    change: ApiChange
    locations: tuple[AffectedLocation, ...] = ()
    #: Packages the change was attributed to, if any could be inferred.
    packages: tuple[str, ...] = ()

    @property
    def files(self) -> list[str]:
        """Distinct files touched, ordered by their best location."""
        seen: dict[str, None] = {}
        for location in self.locations:
            seen.setdefault(location.file, None)
        return list(seen)

    @property
    def best_confidence(self) -> float:
        """Confidence of the strongest location, or zero when there are none."""
        return max((location.confidence for location in self.locations), default=0.0)


class ImpactSummary(BaseModel):
    """Aggregate counts over an impact report."""

    model_config = ConfigDict(frozen=True)

    changes_analysed: int = 0
    changes_with_impact: int = 0
    locations: int = 0
    files_affected: int = 0
    test_locations: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    duration_seconds: float = 0.0

    @classmethod
    def from_impacts(cls, impacts: list[ChangeImpact], *, duration: float) -> ImpactSummary:
        """Compute a summary over per-change impacts."""
        with_impact = [impact for impact in impacts if impact.locations]
        locations = [loc for impact in impacts for loc in impact.locations]
        return cls(
            changes_analysed=len(impacts),
            changes_with_impact=len(with_impact),
            locations=len(locations),
            files_affected=len({location.file for location in locations}),
            test_locations=sum(1 for location in locations if location.is_test),
            by_severity=dict(sorted(Counter(i.change.severity.value for i in with_impact).items())),
            duration_seconds=duration,
        )


class ImpactReport(BaseModel):
    """The result of joining an API change report to a repository index.

    Analysis only: Phase 3 never modifies a file. The report is the input to the
    agent in Phase 4, and the thing measured against ground truth in Phase 8.
    """

    model_config = ConfigDict(frozen=True)

    repository: str
    impacts: tuple[ChangeImpact, ...] = ()
    summary: ImpactSummary = Field(default_factory=ImpactSummary)
    #: Confidence below which candidates were discarded.
    min_confidence: float = 0.0

    @classmethod
    def build(
        cls,
        repository: str,
        impacts: list[ChangeImpact],
        *,
        min_confidence: float,
        duration: float,
    ) -> ImpactReport:
        """Assemble a report, ordering impacts by severity then confidence."""
        ordered = sorted(
            impacts,
            key=lambda impact: (
                impact.change.severity.rank,
                -impact.best_confidence,
                impact.change.endpoint or "",
                impact.change.type.value,
            ),
        )
        return cls(
            repository=repository,
            impacts=tuple(ordered),
            summary=ImpactSummary.from_impacts(ordered, duration=duration),
            min_confidence=min_confidence,
        )

    @property
    def affected_files(self) -> list[str]:
        """Every file with at least one affected location, sorted."""
        return sorted({location.file for impact in self.impacts for location in impact.locations})

    def breaking_impacts(self) -> list[ChangeImpact]:
        """Impacts of changes that definitely break client code."""
        return [
            impact
            for impact in self.impacts
            if impact.change.severity is Severity.BREAKING and impact.locations
        ]

    def locations_in(self, file: str) -> list[AffectedLocation]:
        """Every affected location in one file, most confident first."""
        return sorted(
            (
                location
                for impact in self.impacts
                for location in impact.locations
                if location.file == file
            ),
            key=lambda location: location.sort_key,
        )


#: Locations at or above this confidence are reported by default. Chosen so that
#: a bare name match in a file that does not import the SDK falls below it,
#: while a keyword argument at an SDK call site sits far above. Phase 8 should
#: replace it with a value picked from a precision/recall curve.
DEFAULT_MIN_CONFIDENCE: Final[float] = 0.35


__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "AffectedLocation",
    "ChangeImpact",
    "ImpactReport",
    "ImpactSummary",
    "MatchStrategy",
    "Signal",
    "SignalKind",
]
