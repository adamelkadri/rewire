"""Comparing labelled benchmark arms, whatever it is that differs between them.

Phase 9 compares models. Phase 10 compares harness configurations. The thing
being varied is different; everything about reading the result is identical —
the same rates, the same intervals, the same paired test over the same cases,
the same refusal to call a two-case gap a difference.

So the reporting lives here once. A :class:`Contender` is one labelled column:
a benchmark arm, a name, and a note saying what makes it that column. Both
comparisons build columns and hand them to the same renderers, which is also
what guarantees they cannot drift into reporting the same numbers differently.

The renderers take the noun for what varies ("model", "configuration") so the
prose reads correctly without either caller owning a copy of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

from rewire.evals.migration_dataset import Expectation
from rewire.evals.migration_runner import ArmResult
from rewire.evals.statistics import Interval, PairedComparison, compare_paired, wilson_interval


def render_money(value: float | None) -> str:
    """Format a cost so a cheap run does not render as a free one.

    Two decimal places is right for dollars and wrong for a run that cost a third
    of a cent: ``$0.00`` reads as free, which is the opposite of the point when
    a comparison exists partly to weigh capability against price.
    """
    if value is None:
        return "unknown"
    return f"${value:.4f}" if 0 < value < 0.01 else f"${value:.2f}"


@dataclass(frozen=True, slots=True)
class Contender:
    """One labelled column in a comparison.

    Attributes:
        label: How the column is named, and the key it is paired on.
        result: The arm this column ran. ``None`` when it did not run.
        note: What makes this column what it is — a harness change, a provider.
        skipped: Why it did not run. Empty when it did.
        priced: Whether a cost figure is available for it.
    """

    label: str
    result: ArmResult | None = None
    note: str = ""
    skipped: str = ""
    priced: bool = True

    @property
    def ran(self) -> bool:
        """Whether there are results to compare."""
        return self.result is not None

    def correctness(self) -> dict[str, bool]:
        """``case_id -> handled correctly``.

        Empty for a column that did not run, which drops it out of every paired
        comparison rather than scoring it zero: an unrun arm is not a failed one.
        """
        if self.result is None:
            return {}
        return {outcome.case_id: outcome.succeeded for outcome in self.result.outcomes}

    @property
    def succeeded(self) -> int:
        """Cases handled correctly."""
        return self.result.succeeded if self.result else 0

    @property
    def total(self) -> int:
        """Cases attempted."""
        return self.result.total if self.result else 0

    @property
    def claimed(self) -> int:
        """Cases Rewire vouched for."""
        return self.result.claimed if self.result else 0

    @property
    def overclaimed(self) -> int:
        """Vouched-for patches the hidden test rejected."""
        return self.result.overclaimed if self.result else 0

    @property
    def tokens(self) -> int:
        """Tokens spent."""
        return self.result.total_tokens if self.result else 0

    @property
    def cost_usd(self) -> float | None:
        """Cost, or ``None`` when any case's price is unknown."""
        return self.result.total_cost_usd if self.result else None

    @property
    def interval(self) -> Interval:
        """95% Wilson interval on the correct rate."""
        return wilson_interval(self.succeeded, self.total)

    @property
    def overclaim_rate(self) -> float | None:
        """Overclaims as a share of what Rewire vouched for, or ``None`` if it vouched for nothing.

        The denominator is deliberately ``claimed`` rather than ``total``: this
        answers "when Rewire says a patch works, how often is it wrong", which
        is the question a user of the tool is actually asking.
        """
        return self.overclaimed / self.claimed if self.claimed else None


def ran(contenders: Sequence[Contender]) -> tuple[Contender, ...]:
    """Columns with results, in the order they were requested."""
    return tuple(c for c in contenders if c.ran)


def skipped(contenders: Sequence[Contender]) -> tuple[Contender, ...]:
    """Columns that did not run, each carrying its reason."""
    return tuple(c for c in contenders if not c.ran)


def pairs(contenders: Sequence[Contender]) -> tuple[PairedComparison, ...]:
    """Every pairwise paired comparison between columns that ran."""
    return tuple(
        compare_paired(a.label, b.label, a.correctness(), b.correctness())
        for a, b in combinations(ran(contenders), 2)
    )


def _shared(contenders: Sequence[Contender]) -> tuple[tuple[Contender, ...], set[str]]:
    columns = ran(contenders)
    if not columns:
        return (), set()
    common = set(columns[0].correctness())
    for column in columns[1:]:
        common &= set(column.correctness())
    return columns, common


def unsolved(contenders: Sequence[Contender]) -> tuple[str, ...]:
    """Cases no column handled correctly."""
    columns, common = _shared(contenders)
    return tuple(sorted(c for c in common if not any(col.correctness()[c] for col in columns)))


def solved_by_all(contenders: Sequence[Contender]) -> tuple[str, ...]:
    """Cases every column handled correctly, which separate nothing."""
    columns, common = _shared(contenders)
    return tuple(sorted(c for c in common if all(col.correctness()[c] for col in columns)))


def cell(contender: Contender, case_id: str) -> str:
    """How one column did on one case, in the per-case matrix."""
    if contender.result is None:
        return "-"
    outcome = next((o for o in contender.result.outcomes if o.case_id == case_id), None)
    if outcome is None:
        return "-"
    if outcome.succeeded:
        return "ok"
    if outcome.overclaimed:
        return "**overclaim**"
    if outcome.error:
        return "error"
    if outcome.expectation is Expectation.NO_OP:
        return "**spurious**"
    return "miss"


def render_headline(contenders: Sequence[Contender], *, heading: str) -> list[str]:
    """The rates table, with an interval and an overclaim rate on every row."""
    lines = [
        f"| {heading} | Correct | 95% CI | Verified | Overclaimed |"
        " Overclaim rate | Tokens | Cost |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for column in ran(contenders):
        rate = f"{column.overclaim_rate:.0%}" if column.overclaim_rate is not None else "-"
        lines.append(
            f"| `{column.label}` | **{column.succeeded}/{column.total}** | "
            f"{column.interval.render()} | {column.claimed} | {column.overclaimed} | "
            f"{rate} | {column.tokens} | {render_money(column.cost_usd)} |"
        )
    if not ran(contenders):
        lines += ["", f"**No {heading.lower()} ran.** Nothing here is a measurement."]
    return lines


def render_pairs(contenders: Sequence[Contender], *, subject: str) -> list[str]:
    """The significance section, which is willing to conclude nothing."""
    comparisons = pairs(contenders)
    if not comparisons:
        return []
    return [
        "",
        "## Is the difference real?",
        "",
        "Each pair is compared on the cases both ran, by an exact paired sign test over the",
        f"cases they disagreed on. Cases both {subject}s handled the same way carry no",
        "information about which is better and are excluded. At this sample size most",
        "differences are not separable from chance, and the test is here to say so rather",
        "than to award a winner.",
        "",
        *[f"- {comparison.verdict()}" for comparison in comparisons],
    ]


def render_agreement(contenders: Sequence[Contender], *, subject: str, ceiling: str) -> list[str]:
    """What every column agreed on, which is where the useful information is."""
    if len(ran(contenders)) < 2:
        return []
    nothing_solved = unsolved(contenders)
    everyone = solved_by_all(contenders)
    lines = ["", f"## What the {subject}s agree on", ""]
    if nothing_solved:
        lines += [
            f"**{len(nothing_solved)} case(s) no {subject} solved:** "
            + ", ".join(f"`{case}`" for case in nothing_solved)
            + f". {ceiling}",
            "",
        ]
    else:
        lines += [f"Every case was solved by at least one {subject}.", ""]
    if everyone:
        lines += [
            f"**{len(everyone)} case(s) every {subject} solved:** "
            + ", ".join(f"`{case}`" for case in everyone)
            + f". They contribute nothing to a comparison between {subject}s.",
        ]
    return lines


def render_matrix(contenders: Sequence[Contender]) -> list[str]:
    """One row per case, one column per contender."""
    columns = ran(contenders)
    if not columns:
        return []
    case_ids: list[str] = []
    for column in columns:
        for case_id in column.correctness():
            if case_id not in case_ids:
                case_ids.append(case_id)

    lines = [
        "",
        "## Case by case",
        "",
        "| Case | " + " | ".join(f"`{column.label}`" for column in columns) + " |",
        "|---|" + "---|" * len(columns),
    ]
    for case_id in case_ids:
        lines.append(
            f"| `{case_id}` | " + " | ".join(cell(column, case_id) for column in columns) + " |"
        )
    return lines


def render_skipped(contenders: Sequence[Contender], *, subject: str) -> list[str]:
    """Columns that were requested and produced nothing, with their reasons."""
    missing = skipped(contenders)
    if not missing:
        return []
    return [
        "",
        "## Not run",
        "",
        f"These {subject}s were requested and did not produce results. They are listed rather",
        "than dropped, so the comparison cannot read as more complete than it is.",
        "",
        *[f"- `{column.label}`: {column.skipped}" for column in missing],
    ]


__all__ = [
    "Contender",
    "cell",
    "pairs",
    "ran",
    "render_agreement",
    "render_headline",
    "render_matrix",
    "render_money",
    "render_pairs",
    "render_skipped",
    "skipped",
    "solved_by_all",
    "unsolved",
]
