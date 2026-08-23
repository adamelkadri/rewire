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

__all__ = [
    "CaseResult",
    "ConfusionCounts",
    "EvaluationConfig",
    "EvaluationResult",
    "ExpectedLocation",
    "ImpactCase",
    "Metrics",
    "TargetChange",
    "TargetResult",
    "aggregate",
    "compare",
    "evaluate",
    "evaluate_case",
    "load_case",
    "load_cases",
    "render_markdown",
    "run_evaluation",
    "write_results",
]
