"""Tests for the small-sample statistics the model comparison reports.

These exist because the failure mode they guard against is silent. An interval
that is too narrow, or a test that calls two cases out of ten a difference,
produces a report that looks rigorous and is wrong — and nothing crashes.

The expected values below are computed by hand from the closed forms rather than
from the implementation, so a rewrite that changes the answer is caught.
"""

from __future__ import annotations

import math

import pytest

from rewire.evals.statistics import (
    SIGNIFICANCE,
    Interval,
    binomial_sign_test,
    compare_paired,
    wilson_interval,
)

# ------------------------------------------------------ Wilson intervals ---


def test_wilson_matches_the_closed_form() -> None:
    """6/10 with z=1.96 is (0.3127, 0.8318), worked out by hand from the formula."""
    interval = wilson_interval(6, 10)
    assert interval.point == pytest.approx(0.6)
    assert interval.low == pytest.approx(0.3127, abs=1e-4)
    assert interval.high == pytest.approx(0.8318, abs=1e-4)


def test_wilson_stays_inside_zero_and_one_at_the_extremes() -> None:
    """The normal approximation puts bounds outside [0, 1] here. Wilson must not."""
    zero = wilson_interval(0, 10)
    perfect = wilson_interval(10, 10)
    assert zero.low == 0.0
    assert 0.0 < zero.high < 0.4
    # At p=1 the analytic upper bound is exactly 1; the shortfall is float error,
    # which is not worth distorting the formula to hide.
    assert perfect.high == pytest.approx(1.0)
    assert 0.6 < perfect.low < 1.0


def test_a_perfect_score_is_not_reported_as_certainty() -> None:
    """10/10 is not proof of 100%, and an interval that said so would be the bug."""
    assert wilson_interval(10, 10).low < 0.95


def test_more_trials_narrow_the_interval() -> None:
    small = wilson_interval(6, 10)
    large = wilson_interval(60, 100)
    assert (large.high - large.low) < (small.high - small.low)


def test_no_trials_constrain_nothing() -> None:
    assert wilson_interval(0, 0) == Interval(0.0, 0.0, 1.0)


def test_impossible_counts_are_rejected() -> None:
    with pytest.raises(ValueError, match="impossible"):
        wilson_interval(11, 10)
    with pytest.raises(ValueError, match="negative"):
        wilson_interval(-1, 10)
    with pytest.raises(ValueError, match="negative"):
        wilson_interval(1, -10)


def test_interval_renders_as_percentages() -> None:
    assert wilson_interval(6, 10).render() == "60% (31%-83%)"


# ------------------------------------------------------------ sign test ---


@pytest.mark.parametrize(
    ("wins", "losses", "expected"),
    [
        (0, 0, 1.0),  # no disagreements is no evidence, not a tie broken by luck
        (1, 1, 1.0),
        (2, 0, 0.5),  # two coin flips landing the same way is unremarkable
        (3, 0, 0.25),
        (4, 0, 0.125),
        (5, 0, 0.0625),  # still above 0.05: five is not enough
        (6, 0, 0.03125),
        (3, 1, 0.625),
    ],
)
def test_exact_binomial_p_values(wins: int, losses: int, expected: float) -> None:
    assert binomial_sign_test(wins, losses) == pytest.approx(expected)


def test_the_test_is_symmetric() -> None:
    assert binomial_sign_test(4, 1) == binomial_sign_test(1, 4)


def test_five_to_nothing_is_not_significant_but_six_is() -> None:
    """The practical threshold this dataset has to clear, stated as a test."""
    assert binomial_sign_test(5, 0) > SIGNIFICANCE
    assert binomial_sign_test(6, 0) < SIGNIFICANCE


def test_p_values_never_exceed_one() -> None:
    for wins in range(6):
        for losses in range(6):
            assert 0.0 <= binomial_sign_test(wins, losses) <= 1.0


def test_negative_counts_are_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        binomial_sign_test(-1, 2)


def test_p_value_equals_the_binomial_tail_definition() -> None:
    """Cross-check against the definition rather than against the implementation."""
    wins, losses = 5, 2
    trials = wins + losses
    tail = sum(math.comb(trials, k) for k in range(min(wins, losses) + 1)) / 2**trials
    assert binomial_sign_test(wins, losses) == pytest.approx(2 * tail)


# --------------------------------------------------- paired comparisons ---


def test_pairing_splits_cases_four_ways() -> None:
    a = {"c1": True, "c2": True, "c3": False, "c4": False}
    b = {"c1": True, "c2": False, "c3": True, "c4": False}
    pair = compare_paired("a", "b", a, b)
    assert pair.both == ("c1",)
    assert pair.a_only == ("c2",)
    assert pair.b_only == ("c3",)
    assert pair.neither == ("c4",)
    assert pair.total == 4


def test_only_disagreements_drive_the_p_value() -> None:
    """Adding cases both systems solve must not manufacture significance."""
    lean = compare_paired("a", "b", {"x": True, "y": False}, {"x": False, "y": False})
    padded = compare_paired(
        "a",
        "b",
        {"x": True, "y": False, "z": True, "w": True},
        {"x": False, "y": False, "z": True, "w": True},
    )
    assert lean.p_value == padded.p_value


def test_cases_only_one_system_ran_are_dropped_not_failed() -> None:
    """An unrun case is not a lost case."""
    pair = compare_paired("a", "b", {"x": True, "only-a": True}, {"x": True})
    assert pair.total == 1
    assert pair.a_only == ()


def test_a_level_comparison_names_no_leader() -> None:
    pair = compare_paired("a", "b", {"x": True, "y": False}, {"x": False, "y": True})
    assert pair.leader == ""
    assert not pair.is_significant


def test_the_leader_is_whichever_won_more_disagreements() -> None:
    a = dict.fromkeys(("c1", "c2", "c3"), True)
    b = dict.fromkeys(("c1", "c2", "c3"), False)
    assert compare_paired("a", "b", a, b).leader == "a"
    assert compare_paired("a", "b", b, a).leader == "b"


def test_verdict_says_it_cannot_tell_when_it_cannot() -> None:
    verdict = compare_paired(
        "a", "b", {"x": True, "y": True, "z": False}, {"x": False, "y": False, "z": False}
    ).verdict()
    assert "not distinguishable from chance" in verdict
    assert "n=3" in verdict


def test_verdict_reports_total_agreement_as_a_non_result() -> None:
    same = {"x": True, "y": False}
    verdict = compare_paired("a", "b", same, dict(same)).verdict()
    assert "does not separate them" in verdict


def test_verdict_names_a_leader_only_when_the_split_is_significant() -> None:
    cases = [f"c{i}" for i in range(6)]
    verdict = compare_paired(
        "strong",
        "weak",
        dict.fromkeys(cases, True),
        dict.fromkeys(cases, False),
    ).verdict()
    assert verdict.startswith("strong ahead")
    assert "p=0.031" in verdict
