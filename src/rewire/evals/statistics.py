"""Small-sample statistics, so a benchmark cannot claim more than it measured.

Phase 9 compares models on ten cases. At that size the arithmetic is the easy
part and the inference is where benchmarks go wrong: 6/10 versus 4/10 looks like
a fifty percent improvement and is two cases.

Two tools are enough to keep that honest.

**Wilson score intervals** rather than the normal approximation. At *n* = 10 the
textbook ``p ± z·sqrt(p(1-p)/n)`` interval is wrong in the direction that
flatters — it is too narrow near the extremes and happily produces bounds below
zero or above one. Wilson is well behaved at small *n* and at ``p = 0`` or
``p = 1``, which is exactly where these results sit.

**An exact paired sign test** rather than a two-proportion test. Every model
runs the *same* cases, so the results are paired, and the pairing carries most
of the information: cases every model solves and cases none solves say nothing
about which is better. Only the disagreements do, and with a handful of those
an exact binomial test is the honest instrument — a chi-square approximation
needs counts this data does not have.

Neither routine turns ten cases into a confident ranking. That is the point:
they exist to say "this difference is not distinguishable from noise" out loud,
in the report, where a reader will see it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

#: z for a two-sided 95% interval. The only confidence level used here, so it is
#: a constant rather than a parameter nobody would vary.
Z_95: Final[float] = 1.959963984540054

#: Below this, the paired difference is reported as unlikely to be chance. Named
#: rather than inlined so the threshold is arguable instead of buried.
SIGNIFICANCE: Final[float] = 0.05


@dataclass(frozen=True, slots=True)
class Interval:
    """A confidence interval on a proportion."""

    point: float
    low: float
    high: float

    def render(self) -> str:
        """As a percentage range, e.g. ``60% (31-83%)``."""
        return f"{self.point:.0%} ({self.low:.0%}-{self.high:.0%})"


def wilson_interval(successes: int, total: int, *, z: float = Z_95) -> Interval:
    """Wilson score interval for ``successes`` out of ``total``.

    Args:
        successes: Successful trials.
        total: Trials attempted. Zero yields the whole ``[0, 1]`` range, since
            no trials constrain the rate to nothing.
        z: Standard-normal quantile for the desired confidence.

    Raises:
        ValueError: ``successes`` is negative or exceeds ``total``.
    """
    if successes < 0 or total < 0:
        raise ValueError("counts must not be negative")
    if successes > total:
        raise ValueError(f"{successes} successes out of {total} trials is impossible")
    if total == 0:
        return Interval(0.0, 0.0, 1.0)

    proportion = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    centre = (proportion + z2 / (2 * total)) / denominator
    variance = proportion * (1 - proportion) / total + z2 / (4 * total * total)
    margin = z / denominator * math.sqrt(variance)
    return Interval(proportion, max(0.0, centre - margin), min(1.0, centre + margin))


def binomial_sign_test(wins: int, losses: int) -> float:
    """Two-sided exact binomial p-value for ``wins`` versus ``losses``.

    The null hypothesis is that a disagreement falls either way with equal
    probability — that the two systems are equally good and the split is chance.
    Ties are excluded before calling, per the sign test: a case both systems
    solve is not evidence about which is better.

    Returns 1.0 when there are no disagreements at all, which is the correct
    answer rather than a degenerate one: no evidence of a difference.
    """
    if wins < 0 or losses < 0:
        raise ValueError("counts must not be negative")
    trials = wins + losses
    if trials == 0:
        return 1.0

    tail = min(wins, losses)
    cumulative = sum(math.comb(trials, k) for k in range(tail + 1))
    # ``1 << trials`` is the exact number of equally likely sign sequences.
    return min(1.0, 2 * cumulative / (1 << trials))


@dataclass(frozen=True, slots=True)
class PairedComparison:
    """Two systems judged on the same cases, compared case by case.

    Attributes:
        a: Label of the first system.
        b: Label of the second system.
        a_only: Cases only ``a`` handled correctly.
        b_only: Cases only ``b`` handled correctly.
        both: Cases both handled correctly.
        neither: Cases neither handled correctly.
        p_value: Exact two-sided sign-test p-value over the disagreements.
    """

    a: str
    b: str
    a_only: tuple[str, ...]
    b_only: tuple[str, ...]
    both: tuple[str, ...]
    neither: tuple[str, ...]
    p_value: float

    @property
    def total(self) -> int:
        """Cases compared."""
        return len(self.a_only) + len(self.b_only) + len(self.both) + len(self.neither)

    @property
    def is_significant(self) -> bool:
        """Whether the split is unlikely enough to be chance to be worth naming."""
        return self.p_value < SIGNIFICANCE

    @property
    def leader(self) -> str:
        """The system ahead, or an empty string when they are level."""
        if len(self.a_only) > len(self.b_only):
            return self.a
        if len(self.b_only) > len(self.a_only):
            return self.b
        return ""

    def verdict(self) -> str:
        """One sentence a reader can act on, including when the answer is "we cannot tell"."""
        disagreements = len(self.a_only) + len(self.b_only)
        if disagreements == 0:
            return (
                f"{self.a} and {self.b} agreed on all {self.total} cases; "
                "this dataset does not separate them"
            )
        split = f"{len(self.a_only)}-{len(self.b_only)} on {disagreements} disagreement(s)"
        if not self.is_significant:
            return (
                f"{split}, p={self.p_value:.2f} - not distinguishable from chance at n={self.total}"
            )
        return f"{self.leader} ahead, {split}, p={self.p_value:.3f}"


def compare_paired(
    a: str,
    b: str,
    a_correct: dict[str, bool],
    b_correct: dict[str, bool],
) -> PairedComparison:
    """Compare two systems over the cases they both ran.

    Cases missing from either mapping are dropped rather than counted as
    failures: an unrun case is not a lost case, and scoring it as one would
    penalise whichever system was interrupted.
    """
    shared = sorted(set(a_correct) & set(b_correct))
    a_only = tuple(case for case in shared if a_correct[case] and not b_correct[case])
    b_only = tuple(case for case in shared if b_correct[case] and not a_correct[case])
    both = tuple(case for case in shared if a_correct[case] and b_correct[case])
    neither = tuple(case for case in shared if not a_correct[case] and not b_correct[case])
    return PairedComparison(
        a=a,
        b=b,
        a_only=a_only,
        b_only=b_only,
        both=both,
        neither=neither,
        p_value=binomial_sign_test(len(a_only), len(b_only)),
    )


__all__ = [
    "SIGNIFICANCE",
    "Z_95",
    "Interval",
    "PairedComparison",
    "binomial_sign_test",
    "compare_paired",
    "wilson_interval",
]
