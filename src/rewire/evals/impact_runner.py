"""Running impact analysis against labelled datasets and scoring the result.

Two granularities are reported, because they answer different questions:

* **Location** — did the analyser point at the right *lines*? This is what the
  agent needs; a file-level answer would make it read the whole file.
* **File** — did it point at the right *files*? More forgiving, and the number
  that matters for "which files does this migration touch".

Location metrics are always the stricter of the two, and reporting only the
flattering one would be dishonest, so both are always emitted.

Results are written to ``evals/results/`` as JSON and Markdown, with the
configuration that produced them recorded alongside. A benchmark number without
the settings that produced it cannot be reproduced or compared.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from rewire.analyzers import build_index
from rewire.changes import diff_specs, load_spec
from rewire.core.errors import EvaluationError
from rewire.core.logging import get_logger
from rewire.evals.dataset import ImpactCase, TargetChange, load_cases
from rewire.evals.metrics import ConfusionCounts, Metrics, aggregate, compare
from rewire.impact import DEFAULT_MIN_CONFIDENCE, analyse_impact
from rewire.impact.models import ImpactReport

logger = get_logger(__name__)

#: Where labelled cases live by default.
DEFAULT_DATASET_DIR: Final[Path] = Path("evals/datasets/impact")

#: Where results are written by default.
DEFAULT_RESULTS_DIR: Final[Path] = Path("evals/results")


class MissedLocation(BaseModel):
    """An expected location the analyser failed to report."""

    model_config = ConfigDict(frozen=True)

    file: str
    line: int
    reason: str = ""


class SpuriousLocation(BaseModel):
    """A location the analyser reported that ground truth does not expect."""

    model_config = ConfigDict(frozen=True)

    file: str
    line: int
    confidence: float
    #: The dominant signal, so a false positive can be diagnosed from the report
    #: without re-running the analysis.
    top_signal: str = ""


class TargetResult(BaseModel):
    """Scores for one labelled change within one case."""

    model_config = ConfigDict(frozen=True)

    target: str
    #: False when the change report did not contain the labelled change at all,
    #: which is a Phase 1 failure rather than a Phase 3 one and must not be
    #: silently scored as a Phase 3 miss.
    change_detected: bool = True
    location_metrics: Metrics = Field(default_factory=Metrics)
    file_metrics: Metrics = Field(default_factory=Metrics)
    missed: tuple[MissedLocation, ...] = ()
    spurious: tuple[SpuriousLocation, ...] = ()


class CaseResult(BaseModel):
    """Scores for one dataset case."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    packages: tuple[str, ...] = ()
    targets: tuple[TargetResult, ...] = ()
    duration_seconds: float = 0.0

    @property
    def location_counts(self) -> list[ConfusionCounts]:
        """Per-target location counts, for aggregation."""
        return [target.location_metrics.counts for target in self.targets]

    @property
    def file_counts(self) -> list[ConfusionCounts]:
        """Per-target file counts, for aggregation."""
        return [target.file_metrics.counts for target in self.targets]


class EvaluationConfig(BaseModel):
    """The settings a result was produced under.

    Recorded in every result file. Comparing two benchmark numbers produced
    under different thresholds is meaningless, and the only defence is to make
    the threshold impossible to lose.
    """

    model_config = ConfigDict(frozen=True)

    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    dataset_dir: str = str(DEFAULT_DATASET_DIR)
    rewire_version: str = ""


class EvaluationResult(BaseModel):
    """The complete outcome of an evaluation run."""

    model_config = ConfigDict(frozen=True)

    config: EvaluationConfig
    cases: tuple[CaseResult, ...] = ()
    location_metrics: Metrics = Field(default_factory=Metrics)
    file_metrics: Metrics = Field(default_factory=Metrics)
    duration_seconds: float = 0.0

    @property
    def undetected_changes(self) -> list[str]:
        """Labelled changes the differ never produced, so impact could not score."""
        return [
            f"{case.name}/{target.target}"
            for case in self.cases
            for target in case.targets
            if not target.change_detected
        ]


def evaluate_case(case: ImpactCase, *, min_confidence: float) -> CaseResult:
    """Run impact analysis for one case and score it against its labels."""
    started = time.perf_counter()
    changes = diff_specs(load_spec(case.old_spec), load_spec(case.new_spec))
    index = build_index(case.repository)
    report = analyse_impact(changes, index, packages=case.packages, min_confidence=min_confidence)

    results = tuple(_score_target(target, report) for target in case.targets)
    return CaseResult(
        name=case.name,
        description=case.description,
        packages=case.packages,
        targets=results,
        duration_seconds=time.perf_counter() - started,
    )


def _score_target(target: TargetChange, report: ImpactReport) -> TargetResult:
    matching = [
        impact
        for impact in report.impacts
        if impact.change.type.value == target.change_type
        and impact.change.field_path == target.field_path
    ]
    if not matching:
        return TargetResult(
            target=target.label,
            change_detected=False,
            location_metrics=Metrics.from_counts(
                ConfusionCounts(false_negatives=len(target.expected))
            ),
            file_metrics=Metrics.from_counts(
                ConfusionCounts(false_negatives=len(target.expected_files()))
            ),
            missed=tuple(
                MissedLocation(file=item.file, line=item.line, reason=item.reason)
                for item in target.expected
            ),
        )

    locations = [location for impact in matching for location in impact.locations]
    predicted = {(location.file, location.line) for location in locations}
    expected = target.expected_keys()

    return TargetResult(
        target=target.label,
        location_metrics=Metrics.from_counts(compare(predicted, expected)),
        file_metrics=Metrics.from_counts(
            compare({file for file, _ in predicted}, target.expected_files())
        ),
        missed=tuple(
            MissedLocation(file=item.file, line=item.line, reason=item.reason)
            for item in target.expected
            if item.key not in predicted
        ),
        spurious=tuple(
            sorted(
                (
                    SpuriousLocation(
                        file=location.file,
                        line=location.line,
                        confidence=round(location.confidence, 4),
                        top_signal=location.signals[0].detail if location.signals else "",
                    )
                    for location in locations
                    if (location.file, location.line) not in expected
                ),
                key=lambda item: (-item.confidence, item.file, item.line),
            )
        ),
    )


def run_evaluation(
    dataset_dir: Path | str = DEFAULT_DATASET_DIR,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    version: str = "",
) -> EvaluationResult:
    """Evaluate impact analysis across every labelled case.

    Raises:
        EvaluationError: The dataset directory is missing or contains no cases.
    """
    started = time.perf_counter()
    cases = load_cases(dataset_dir)
    results = [evaluate_case(case, min_confidence=min_confidence) for case in cases]

    result = EvaluationResult(
        config=EvaluationConfig(
            min_confidence=min_confidence,
            dataset_dir=str(dataset_dir),
            rewire_version=version,
        ),
        cases=tuple(results),
        location_metrics=aggregate(counts for case in results for counts in case.location_counts),
        file_metrics=aggregate(counts for case in results for counts in case.file_counts),
        duration_seconds=time.perf_counter() - started,
    )
    logger.info(
        "impact_evaluation_complete",
        cases=len(results),
        location_f1=result.location_metrics.f1,
        file_f1=result.file_metrics.f1,
        min_confidence=min_confidence,
    )
    return result


def render_markdown(result: EvaluationResult) -> str:
    """Render an evaluation result as a Markdown report."""
    lines = [
        "# Impact analysis evaluation",
        "",
        f"- Rewire version: `{result.config.rewire_version or 'unknown'}`",
        f"- Minimum confidence: `{result.config.min_confidence}`",
        f"- Dataset: `{result.config.dataset_dir}`",
        f"- Duration: {result.duration_seconds:.2f}s",
        "",
        "## Overall",
        "",
        "| Granularity | Precision | Recall | F1 | TP | FP | FN |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, metrics in (
        ("location", result.location_metrics),
        ("file", result.file_metrics),
    ):
        counts = metrics.counts
        lines.append(
            f"| {label} | {metrics.precision:.3f} | {metrics.recall:.3f} | "
            f"{metrics.f1:.3f} | {counts.true_positives} | "
            f"{counts.false_positives} | {counts.false_negatives} |"
        )

    if result.undetected_changes:
        lines += [
            "",
            "> **Labelled changes the differ never reported**, so impact analysis "
            "could not be scored on them: "
            + ", ".join(f"`{name}`" for name in result.undetected_changes),
        ]

    lines += ["", "## Cases", ""]
    for case in result.cases:
        lines += [f"### `{case.name}`", "", case.description, ""]
        for target in case.targets:
            lines.append(f"**{target.target}** — {target.location_metrics.render()}")
            lines.append("")
            for missed in target.missed:
                lines.append(f"- missed `{missed.file}:{missed.line}` — {missed.reason}")
            for spurious in target.spurious:
                lines.append(
                    f"- spurious `{spurious.file}:{spurious.line}` "
                    f"(confidence {spurious.confidence:.3f}, {spurious.top_signal})"
                )
            if target.missed or target.spurious:
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_results(
    result: EvaluationResult, directory: Path | str = DEFAULT_RESULTS_DIR
) -> tuple[Path, Path]:
    """Write ``latest.json`` and ``latest.md`` into ``directory``.

    Returns:
        The two paths written.

    Raises:
        EvaluationError: The directory could not be created or written to.
    """
    target = Path(directory)
    try:
        target.mkdir(parents=True, exist_ok=True)
        json_path = target / "latest.json"
        markdown_path = target / "latest.md"
        json_path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
        markdown_path.write_text(render_markdown(result), encoding="utf-8")
    except OSError as exc:
        raise EvaluationError(f"could not write results: {exc}", path=str(target)) from exc
    return json_path, markdown_path


__all__ = [
    "DEFAULT_DATASET_DIR",
    "DEFAULT_RESULTS_DIR",
    "CaseResult",
    "EvaluationConfig",
    "EvaluationResult",
    "TargetResult",
    "evaluate_case",
    "render_markdown",
    "run_evaluation",
    "write_results",
]
