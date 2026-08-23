"""Tests for classification metrics, especially their degenerate cases."""

from __future__ import annotations

import pytest

from rewire.evals.metrics import ConfusionCounts, Metrics, aggregate, compare, evaluate


def test_perfect_agreement() -> None:
    metrics = evaluate({"a", "b"}, {"a", "b"})
    assert (metrics.precision, metrics.recall, metrics.f1) == (1.0, 1.0, 1.0)


def test_counts_are_computed_from_set_difference() -> None:
    counts = compare({"a", "b", "c"}, {"b", "c", "d"})
    assert counts.true_positives == 2
    assert counts.false_positives == 1
    assert counts.false_negatives == 1


def test_precision_and_recall_are_distinct() -> None:
    over_eager = evaluate({"a", "b", "c"}, {"a"})
    assert over_eager.recall == 1.0
    # Metrics are rounded to four places so the stored JSON stays readable.
    assert over_eager.precision == pytest.approx(1 / 3, abs=1e-4)


def test_f1_is_the_harmonic_mean() -> None:
    metrics = evaluate({"a", "b"}, {"b", "c"})
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.f1 == 0.5


def test_nothing_expected_and_nothing_found_is_a_success() -> None:
    """The analyser was right that there was nothing to find."""
    metrics = evaluate(set[str](), set[str]())
    assert (metrics.precision, metrics.recall, metrics.f1) == (1.0, 1.0, 1.0)


def test_finding_something_where_nothing_was_expected() -> None:
    metrics = evaluate({"a"}, set[str]())
    assert metrics.precision == 0.0
    assert metrics.recall == 1.0


def test_finding_nothing_where_something_was_expected() -> None:
    metrics = evaluate(set[str](), {"a"})
    assert metrics.precision == 1.0
    assert metrics.recall == 0.0
    assert metrics.f1 == 0.0


def test_counts_add() -> None:
    total = ConfusionCounts(true_positives=1, false_positives=2) + ConfusionCounts(
        true_positives=3, false_negatives=4
    )
    assert total.true_positives == 4
    assert total.false_positives == 2
    assert total.false_negatives == 4


def test_aggregate_is_micro_averaged() -> None:
    """A one-item case must not outweigh a twenty-item case."""
    tiny = ConfusionCounts(true_positives=1)
    large = ConfusionCounts(true_positives=10, false_positives=10)
    micro = aggregate([tiny, large])
    assert micro.precision == pytest.approx(11 / 21, abs=1e-4)
    # A macro average would give (1.0 + 0.5) / 2 = 0.75 instead.
    assert micro.precision != pytest.approx(0.75, abs=1e-4)


def test_aggregate_of_nothing_is_vacuously_perfect() -> None:
    assert aggregate([]).f1 == 1.0


def test_predicted_and_expected_totals() -> None:
    counts = ConfusionCounts(true_positives=2, false_positives=3, false_negatives=4)
    assert counts.predicted == 5
    assert counts.expected == 6


def test_render_includes_the_raw_counts() -> None:
    """A rate without its counts hides a benchmark that is too small to mean anything."""
    rendered = Metrics.from_counts(ConfusionCounts(true_positives=1, false_positives=1)).render()
    assert "tp=1" in rendered
    assert "fp=1" in rendered
