"""Running the same benchmark across several models, and comparing them honestly.

Phase 8 measured one model. That answers "how good is Rewire with gpt-4o", which
conflates two things a reader wants separated: how much of the result is the
harness, and how much is the model behind it.

Running identical cases across models separates them. The interesting output is
not the ranking — with ten cases there is barely a ranking to be had — but the
**agreement structure**:

* Cases *no* model solves are the harness's ceiling. A better model will not fix
  them, and reporting them as model failures would point improvement work at the
  wrong place.
* Cases *every* model solves carry no information about model choice and are
  excluded from the paired test for that reason.
* Only the disagreements say anything about which model is better, and there
  are few of them, which is why every comparison here carries a p-value and is
  willing to conclude "not distinguishable".

**Overclaim rate is compared too, and it is the number that transfers.** If a
weak model overclaims and a strong one does not, the fix is a better model. If
every model overclaims at a similar rate, the fix is Rewire's verification, and
no amount of model shopping will help.

A model with no configured credential is recorded as skipped **with its reason**
and kept in the report. Silently dropping it would leave a comparison that reads
as complete while missing a provider.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from rewire.core.config import Settings
from rewire.core.errors import ConfigurationError, EvaluationError
from rewire.core.logging import get_logger
from rewire.evals.comparison import (
    Contender,
    render_agreement,
    render_headline,
    render_matrix,
    render_money,
    render_pairs,
    render_skipped,
)
from rewire.evals.comparison import pairs as paired_comparisons
from rewire.evals.comparison import solved_by_all as agreed_solved
from rewire.evals.comparison import unsolved as agreed_unsolved
from rewire.evals.migration_dataset import MigrationCase
from rewire.evals.migration_runner import (
    DEFAULT_ARMS,
    ArmConfig,
    BenchmarkConfig,
    BenchmarkResult,
    run_benchmark,
)
from rewire.evals.statistics import Interval, PairedComparison
from rewire.llm.base import LLMProvider
from rewire.llm.pricing import PRICING_SNAPSHOT_DATE, pricing_for
from rewire.llm.registry import SUPPORTED_PROVIDERS, build_provider_for, credential_for
from rewire.services.repair import VerifyCallable

logger = get_logger(__name__)

#: The arm every model is compared under: the benchmark's own repair arm, taken
#: from ``DEFAULT_ARMS`` rather than restated. Repair on is Rewire's real
#: configuration, so comparing models with it off would measure a product nobody
#: runs — and a restated copy would let a later phase change the shipped budget
#: while the comparison quietly kept the old one.
DEFAULT_COMPARISON_ARM: ArmConfig = DEFAULT_ARMS[-1]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One model to compare, as ``provider:model``."""

    provider: str
    model: str

    @property
    def label(self) -> str:
        """Stable identifier used as a column heading and a dictionary key."""
        return f"{self.provider}:{self.model}"

    @classmethod
    def parse(cls, text: str) -> ModelSpec:
        """Parse ``provider:model``.

        The model half may itself contain colons — some hosted identifiers do —
        so only the first is a separator.

        Raises:
            ConfigurationError: The text is not ``provider:model``, or names a
                provider with no adapter.
        """
        provider, separator, model = text.partition(":")
        if not separator or not provider or not model:
            raise ConfigurationError(
                f"{text!r} is not a valid model specification",
                expected="provider:model, e.g. openai:gpt-4o",
            )
        if provider not in SUPPORTED_PROVIDERS:
            raise ConfigurationError(
                f"unknown provider {provider!r}",
                supported=sorted(SUPPORTED_PROVIDERS),
            )
        return cls(provider=provider, model=model)


class ModelRun(BaseModel):
    """What happened when one model ran the benchmark."""

    model_config = ConfigDict(frozen=True)

    label: str
    provider: str
    model: str
    #: ``None`` when the model was skipped.
    result: BenchmarkResult | None = None
    #: Why the model did not run. Empty when it did.
    skipped: str = ""
    #: Whether this model's price is in the pricing snapshot. A model that is
    #: not cannot have its cost compared, and saying so beats printing "unknown"
    #: in a column and hoping the reader wonders why.
    priced: bool = True

    @property
    def contender(self) -> Contender:
        """This run as a comparison column."""
        arm = self.result.arms[0] if self.result and self.result.arms else None
        return Contender(
            label=self.label,
            result=arm,
            note=f"{self.provider} / {self.model}",
            skipped=self.skipped,
            priced=self.priced,
        )

    @property
    def ran(self) -> bool:
        """Whether there are results to compare."""
        return self.contender.ran

    def correctness(self) -> dict[str, bool]:
        """``case_id -> handled correctly``, over the single compared arm."""
        return self.contender.correctness()

    @property
    def succeeded(self) -> int:
        """Cases handled correctly."""
        return self.contender.succeeded

    @property
    def total(self) -> int:
        """Cases attempted."""
        return self.contender.total

    @property
    def claimed(self) -> int:
        """Cases this model's patches were vouched for by Rewire."""
        return self.contender.claimed

    @property
    def overclaimed(self) -> int:
        """Vouched-for patches the hidden test rejected."""
        return self.contender.overclaimed

    @property
    def tokens(self) -> int:
        """Tokens spent."""
        return self.contender.tokens

    @property
    def cost_usd(self) -> float | None:
        """Cost, or ``None`` when any case's price is unknown."""
        return self.contender.cost_usd

    @property
    def interval(self) -> Interval:
        """95% Wilson interval on the correct rate."""
        return self.contender.interval

    @property
    def overclaim_rate(self) -> float | None:
        """Overclaims as a share of what Rewire vouched for."""
        return self.contender.overclaim_rate


class ModelComparison(BaseModel):
    """Every model's run, plus the paired comparisons between them."""

    model_config = ConfigDict(frozen=True)

    runs: tuple[ModelRun, ...] = ()
    arm: str = ""
    dataset: str = ""
    cases: int = 0
    generated_at: str = ""
    duration_seconds: float = 0.0
    #: Date of the price table used for every cost figure here.
    pricing_snapshot: str = PRICING_SNAPSHOT_DATE

    @property
    def compared(self) -> tuple[ModelRun, ...]:
        """Runs with results, in the order they were requested."""
        return tuple(run for run in self.runs if run.ran)

    @property
    def skipped(self) -> tuple[ModelRun, ...]:
        """Runs that did not happen, each carrying its reason."""
        return tuple(run for run in self.runs if not run.ran)

    @property
    def contenders(self) -> tuple[Contender, ...]:
        """Every run as a comparison column, skipped ones included."""
        return tuple(run.contender for run in self.runs)

    def pairs(self) -> tuple[PairedComparison, ...]:
        """Every pairwise paired comparison between models that ran."""
        return paired_comparisons(self.contenders)

    def unsolved(self) -> tuple[str, ...]:
        """Cases no model handled correctly — the harness's ceiling, not the model's."""
        return agreed_unsolved(self.contenders)

    def solved_by_all(self) -> tuple[str, ...]:
        """Cases every model handled correctly, which separate nothing."""
        return agreed_solved(self.contenders)


@dataclass(slots=True)
class ComparisonConfig:
    """How to run the model comparison."""

    dataset: Path
    models: tuple[ModelSpec, ...]
    arm: ArmConfig = DEFAULT_COMPARISON_ARM
    only: tuple[str, ...] = ()
    limit: int = 0
    results_dir: Path = Path("evals/results")
    incremental: bool = True
    progress: Callable[[str], None] | None = field(default=None)
    #: How a provider is built for a model. Bound late and substitutable, so the
    #: comparison can be exercised without spending money. Defaulting to the
    #: real registry function *as an argument* would make it unsubstitutable,
    #: which is how an earlier phase silently ran the whole suite against live
    #: Docker.
    provider_factory: Callable[[Settings, ModelSpec], LLMProvider] | None = field(default=None)


def _build_provider(settings: Settings, spec: ModelSpec) -> LLMProvider:
    """Construct the real provider for ``spec``."""
    return build_provider_for(settings.llm, provider=spec.provider, model=spec.model)


def _prefixed(sink: Callable[[str], None], label: str) -> Callable[[str], None]:
    """Tag each progress message with the model it belongs to."""

    def forward(message: str) -> None:
        sink(f"{label}: {message}")

    return forward


def compare_models(
    config: ComparisonConfig,
    cases: Sequence[MigrationCase],
    *,
    settings: Settings,
    verifier: VerifyCallable | None = None,
) -> ModelComparison:
    """Run every model over every case under one arm, and compare them.

    A model whose provider has no credential is skipped with that reason rather
    than raising: a missing Anthropic key should not throw away a completed
    OpenAI run that cost real money.

    Raises:
        EvaluationError: No models were requested.
    """
    if not config.models:
        raise EvaluationError("no models to compare")

    started = time.perf_counter()
    runs: list[ModelRun] = []
    build = config.provider_factory or _build_provider

    for spec in config.models:
        if credential_for(settings.llm, spec.provider) is None:
            logger.info("model_skipped_no_credential", model=spec.label)
            runs.append(
                ModelRun(
                    label=spec.label,
                    provider=spec.provider,
                    model=spec.model,
                    skipped=(
                        f"no API key for {spec.provider}; "
                        f"set REWIRE_LLM__{spec.provider.upper()}_API_KEY to include it"
                    ),
                )
            )
            continue

        if config.progress:
            config.progress(f"{spec.label}: starting")
        try:
            provider = build(settings, spec)
            result = run_benchmark(
                BenchmarkConfig(
                    dataset=config.dataset,
                    arms=(config.arm,),
                    only=config.only,
                    limit=config.limit,
                    results_dir=config.results_dir,
                    incremental=config.incremental,
                    progress=(_prefixed(config.progress, spec.label) if config.progress else None),
                ),
                cases,
                provider=provider,
                settings=settings,
                verifier=verifier,
            )
        except Exception as exc:
            logger.warning("model_run_failed", model=spec.label, error=str(exc))
            runs.append(
                ModelRun(
                    label=spec.label,
                    provider=spec.provider,
                    model=spec.model,
                    skipped=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        runs.append(
            ModelRun(
                label=spec.label,
                provider=spec.provider,
                model=spec.model,
                result=result,
                priced=pricing_for(spec.model) is not None,
            )
        )
        if config.incremental:
            _write_partial(config, runs, started)

    return _assemble(config, runs, started)


def _assemble(
    config: ComparisonConfig, runs: Sequence[ModelRun], started: float
) -> ModelComparison:
    return ModelComparison(
        runs=tuple(runs),
        arm=config.arm.name,
        dataset=str(config.dataset),
        cases=max((run.total for run in runs), default=0),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        duration_seconds=round(time.perf_counter() - started, 1),
    )


def _write_partial(config: ComparisonConfig, runs: Sequence[ModelRun], started: float) -> None:
    """Persist after each model, so an interrupted comparison keeps what it paid for."""
    try:
        config.results_dir.mkdir(parents=True, exist_ok=True)
        (config.results_dir / "models-partial.json").write_text(
            _assemble(config, runs, started).model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - results dir is writable by then
        logger.warning("comparison_partial_not_written", error=str(exc))


def render_markdown(comparison: ModelComparison) -> str:
    """Render the comparison as a report that states what it cannot conclude."""
    columns = comparison.contenders
    lines = [
        "# Model comparison",
        "",
        f"- dataset: `{comparison.dataset}` ({comparison.cases} case(s))",
        f"- arm: `{comparison.arm}` — identical for every model",
        f"- generated: {comparison.generated_at}",
        f"- wall clock: {comparison.duration_seconds:.0f}s",
        f"- prices as of: {comparison.pricing_snapshot}",
        "",
        "Every model ran the same cases with the same prompts, tools and repair budget,",
        "and every patch was graded by the same hidden contract tests. **Correct** is what",
        "the hidden test found; **overclaim rate** is how often Rewire vouched for a patch",
        "that test rejected — the number that says whether verification, rather than the",
        "model, is the thing to fix.",
        "",
        *render_headline(columns, heading="Model"),
    ]

    unpriced = [run.label for run in comparison.compared if not run.priced]
    if unpriced:
        lines += [
            "",
            "Cost is unknown for "
            + ", ".join(f"`{label}`" for label in unpriced)
            + ": the model is absent from the pricing snapshot, so no figure is invented for it.",
        ]

    lines += render_pairs(columns, subject="model")
    lines += render_agreement(
        columns,
        subject="model",
        ceiling=(
            "These are Rewire's ceiling rather than the model's — a stronger model did not "
            "move them, so the improvement to make is in the harness."
        ),
    )
    lines += render_matrix(columns)
    lines += render_skipped(columns, subject="model")
    return "\n".join(lines) + "\n"


def write_results(comparison: ModelComparison, directory: Path | str) -> tuple[Path, Path]:
    """Write the comparison as JSON and Markdown, returning both paths."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "models.json"
    markdown_path = root / "models.md"
    json_path.write_text(
        json.dumps(comparison.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(comparison), encoding="utf-8")
    return json_path, markdown_path


__all__ = [
    "DEFAULT_COMPARISON_ARM",
    "ComparisonConfig",
    "ModelComparison",
    "ModelRun",
    "ModelSpec",
    "compare_models",
    "render_markdown",
    "render_money",
    "write_results",
]
