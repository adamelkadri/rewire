"""Classification metrics for evaluating detection quality.

Deliberately small and explicit. Precision, recall and F1 are three divisions,
and hiding them behind a dependency would obscure the one thing that actually
needs care: what happens at the edges. A run that finds nothing and a run that
is asked to find nothing are different situations, and reporting 0.0 for both --
or 1.0 for both -- makes a benchmark say something untrue.

The convention used here:

* No predictions and no expectations is a *vacuous success*: precision, recall
  and F1 are all 1.0. The analyser was right that there was nothing to find.
* Predictions but no expectations gives precision 0.0 and recall 1.0. Everything
  reported was wrong; there was nothing to miss.
* Expectations but no predictions gives precision 1.0 and recall 0.0. Nothing
  reported was wrong, because nothing was reported.

Each is stated so a reader can disagree with the convention rather than be
misled by it.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable

from pydantic import BaseModel, ConfigDict


class ConfusionCounts(BaseModel):
    """Raw counts behind a set of metrics."""

    model_config = ConfigDict(frozen=True)

    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def predicted(self) -> int:
        """How many items were reported."""
        return self.true_positives + self.false_positives

    @property
    def expected(self) -> int:
        """How many items should have been reported."""
        return self.true_positives + self.false_negatives

    def __add__(self, other: ConfusionCounts) -> ConfusionCounts:
        """Combine counts, for aggregating across cases."""
        return ConfusionCounts(
            true_positives=self.true_positives + other.true_positives,
            false_positives=self.false_positives + other.false_positives,
            false_negatives=self.false_negatives + other.false_negatives,
        )


class Metrics(BaseModel):
    """Precision, recall and F1 over one comparison."""

    model_config = ConfigDict(frozen=True)

    counts: ConfusionCounts = ConfusionCounts()
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0

    @classmethod
    def from_counts(cls, counts: ConfusionCounts) -> Metrics:
        """Compute metrics from raw counts, handling the degenerate cases."""
        if counts.predicted == 0 and counts.expected == 0:
            return cls(counts=counts, precision=1.0, recall=1.0, f1=1.0)

        precision = counts.true_positives / counts.predicted if counts.predicted else 1.0
        recall = counts.true_positives / counts.expected if counts.expected else 1.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        return cls(
            counts=counts,
            precision=round(precision, 4),
            recall=round(recall, 4),
            f1=round(f1, 4),
        )

    def render(self) -> str:
        """One-line rendering for reports."""
        return (
            f"P={self.precision:.3f} R={self.recall:.3f} F1={self.f1:.3f} "
            f"(tp={self.counts.true_positives} "
            f"fp={self.counts.false_positives} fn={self.counts.false_negatives})"
        )


def compare[T: Hashable](predicted: Iterable[T], expected: Iterable[T]) -> ConfusionCounts:
    """Count true positives, false positives and false negatives between two sets."""
    predicted_set, expected_set = set(predicted), set(expected)
    return ConfusionCounts(
        true_positives=len(predicted_set & expected_set),
        false_positives=len(predicted_set - expected_set),
        false_negatives=len(expected_set - predicted_set),
    )


def evaluate[T: Hashable](predicted: Iterable[T], expected: Iterable[T]) -> Metrics:
    """Compare two sets and return metrics."""
    return Metrics.from_counts(compare(predicted, expected))


def aggregate(counts: Iterable[ConfusionCounts]) -> Metrics:
    """Micro-average metrics over several comparisons.

    Micro rather than macro: every location counts once regardless of which case
    it came from, so a case with one expected location cannot outweigh a case
    with twenty. Macro-averaging would let a trivial case dominate the headline
    number.
    """
    total = ConfusionCounts()
    for item in counts:
        total = total + item
    return Metrics.from_counts(total)


__all__ = ["ConfusionCounts", "Metrics", "aggregate", "compare", "evaluate"]
