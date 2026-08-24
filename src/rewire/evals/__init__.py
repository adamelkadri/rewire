"""Benchmark datasets, runners and metrics.

Evaluation is a first-class feature rather than an afterthought: every claim
Rewire makes about its own accuracy is produced here, against labelled data that
lives in the repository and can be disagreed with.
"""

from rewire.evals.dataset import ExpectedLocation, ImpactCase, TargetChange, load_case, load_cases
from rewire.evals.impact_runner import (
    CaseResult,
    EvaluationConfig,
    EvaluationResult,
    TargetResult,
    evaluate_case,
    render_markdown,
    run_evaluation,
    write_results,
)
from rewire.evals.metrics import ConfusionCounts, Metrics, aggregate, compare, evaluate
from rewire.evals.migration_dataset import (
    Expectation,
    MigrationCase,
    load_migration_case,
    load_migration_cases,
)
from rewire.evals.migration_runner import (
    DEFAULT_ARMS,
    ArmConfig,
    ArmResult,
    BenchmarkConfig,
    BenchmarkResult,
    CaseOutcome,
    run_benchmark,
)

__all__ = [
    "DEFAULT_ARMS",
    "ArmConfig",
    "ArmResult",
    "BenchmarkConfig",
    "BenchmarkResult",
    "CaseOutcome",
    "CaseResult",
    "ConfusionCounts",
    "EvaluationConfig",
    "EvaluationResult",
    "Expectation",
    "ExpectedLocation",
    "ImpactCase",
    "Metrics",
    "MigrationCase",
    "TargetChange",
    "TargetResult",
    "aggregate",
    "compare",
    "evaluate",
    "evaluate_case",
    "load_case",
    "load_cases",
    "load_migration_case",
    "load_migration_cases",
    "render_markdown",
    "run_benchmark",
    "run_evaluation",
    "write_results",
]
