"""Taking pieces of the harness away, to find out what they were worth.

Rewire's founding claim is that deterministic analysis before the model makes
the model better: parse the specs, index the repository, rank the affected
locations, and hand the agent findings rather than a search problem. Phase 9
established that swapping the model barely moves the result, which makes this
claim the next thing worth testing — and it has never been tested, only asserted.

An ablation tests it the only way a claim like that can be tested: by removing
the thing and running the same benchmark. Four arms, differing only in what the
agent is given:

* **full** — the shipped configuration, and the control.
* **no-impact-locations** — the agent is told exactly which API fields changed
  and *not* where they are used. It keeps every tool, so it can still find the
  code; what it loses is being told the answer.
* **no-impact** — the same, and the pipeline no longer stops when impact
  analysis finds nothing. "It can tell you there is nothing to do" is part of
  what impact analysis is worth, and an arm that keeps that gate is not
  measuring the analysis, only the prompt.
* **no-search** — the mirror image: the ranked locations are given, and the
  tools for looking beyond them are taken away. Phase 6 failed live on a file
  the analysis did not rank, so this arm has a specific hypothesis to test.

Together the middle two arms decompose where the agent's information comes from.
If **no-impact-locations** matches **full**, the ranked locations are decoration
and the search tools were doing the work. If it collapses, the deterministic
analysis is carrying the system. Either answer is worth having; only one of them
is comfortable.

The same statistics apply as everywhere else in this project. Ten cases will not
separate four arms, and the report says so rather than ranking them.
"""

from __future__ import annotations

import json
from pathlib import Path

from rewire.agents.config import SEARCH_TOOLS
from rewire.evals.comparison import (
    Contender,
    render_agreement,
    render_headline,
    render_matrix,
    render_pairs,
)
from rewire.evals.migration_runner import ArmConfig, BenchmarkResult

#: Repair budget every arm gets. Held constant so the only thing that differs
#: between arms is the information the agent is given -- Phase 8 already
#: measured the repair budget, and varying two things at once would measure
#: neither.
ABLATION_ATTEMPTS: int = 3

#: The four arms, and the control they are compared against.
DEFAULT_ABLATIONS: tuple[ArmConfig, ...] = (
    ArmConfig(
        name="full",
        max_attempts=ABLATION_ATTEMPTS,
        description="the shipped configuration: ranked locations and every tool",
    ),
    ArmConfig(
        name="no-impact-locations",
        max_attempts=ABLATION_ATTEMPTS,
        include_impact_locations=False,
        description="told which fields changed, not where they are used; every tool kept",
    ),
    ArmConfig(
        name="no-impact",
        max_attempts=ABLATION_ATTEMPTS,
        include_impact_locations=False,
        require_affected_code=False,
        description="impact analysis withheld entirely, including its power to say 'nothing here'",
    ),
    ArmConfig(
        name="no-search",
        max_attempts=ABLATION_ATTEMPTS,
        withheld_tools=tuple(sorted(SEARCH_TOOLS)),
        description="given the ranked locations, denied the tools to look beyond them",
    ),
)


def contenders(result: BenchmarkResult) -> tuple[Contender, ...]:
    """Each arm of a benchmark run as a comparison column."""
    return tuple(Contender(label=arm.arm, result=arm, note=arm.harness) for arm in result.arms)


def render_markdown(result: BenchmarkResult) -> str:
    """Render the ablation as a report that names what each arm lost."""
    columns = contenders(result)
    lines = [
        "# Agent ablations",
        "",
        f"- dataset: `{result.dataset}` ({result.cases} case(s))",
        f"- model: `{result.provider}` / `{result.model}` — identical for every arm",
        f"- repair budget: {ABLATION_ATTEMPTS} attempts — identical for every arm",
        f"- generated: {result.generated_at}",
        f"- wall clock: {result.duration_seconds:.0f}s",
        "",
        "Every arm ran the same cases against the same model with the same repair budget,",
        "and every patch was graded by the same hidden contract tests. The only thing that",
        "differs between arms is what the agent was given.",
        "",
        "| Arm | What it lost |",
        "|---|---|",
    ]
    for arm in result.arms:
        lines.append(f"| `{arm.arm}` | {arm.description} |")
    lines += ["", *render_headline(columns, heading="Arm")]

    if result.ungraded_cases:
        lines += [
            "",
            f"**{len(result.ungraded_cases)} case(s) ship no hidden test** and are graded on "
            "Rewire's own word: " + ", ".join(f"`{name}`" for name in result.ungraded_cases) + ".",
        ]

    lines += render_pairs(columns, subject="arm")
    lines += render_agreement(
        columns,
        subject="arm",
        ceiling=(
            "No configuration of the harness reached them, so they are not a question of "
            "what the agent was given."
        ),
    )
    lines += render_matrix(columns)
    return "\n".join(lines) + "\n"


def write_results(result: BenchmarkResult, directory: Path | str) -> tuple[Path, Path]:
    """Write the ablation as JSON and Markdown, returning both paths."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "ablation.json"
    markdown_path = root / "ablation.md"
    json_path.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, markdown_path


__all__ = [
    "ABLATION_ATTEMPTS",
    "DEFAULT_ABLATIONS",
    "contenders",
    "render_markdown",
    "write_results",
]
