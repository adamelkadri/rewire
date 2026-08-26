"""Command-line entry point for Rewire.

Only commands that are genuinely implemented are exposed. Commands for later
phases are deliberately absent rather than stubbed, so that ``rewire --help``
never advertises behaviour that does not exist.
"""

from __future__ import annotations

import json
import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, assert_never

import typer
from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table

from rewire.__version__ import __version__
from rewire.agents import (
    AgentBudget,
    CandidatePatch,
    MigrationAgent,
    MigrationResult,
    Workspace,
)
from rewire.analyzers import (
    DiscoveryLimits,
    Reference,
    ReferenceKind,
    RepositoryIndex,
    TextMatch,
    build_index,
    get_backend,
    resolve_repository_root,
)
from rewire.changes import ApiChange, ChangeReport, Severity, diff_specs, load_spec
from rewire.core.config import LogLevel, Settings, get_settings
from rewire.core.doctor import CheckStatus, DoctorReport, run_checks
from rewire.core.errors import ConfigurationError, RewireError
from rewire.core.logging import configure_from_settings
from rewire.evals import render_markdown, run_evaluation, write_results
from rewire.evals.ablation import DEFAULT_ABLATIONS
from rewire.evals.ablation import contenders as ablation_contenders
from rewire.evals.ablation import render_markdown as render_ablation
from rewire.evals.ablation import write_results as write_ablation
from rewire.evals.comparison import pairs as comparison_pairs
from rewire.evals.comparison import unsolved as comparison_unsolved
from rewire.evals.migration_dataset import load_migration_cases
from rewire.evals.migration_runner import (
    DEFAULT_ARMS,
    BenchmarkConfig,
    BenchmarkResult,
    run_benchmark,
)
from rewire.evals.migration_runner import render_markdown as render_benchmark
from rewire.evals.migration_runner import write_results as write_benchmark
from rewire.evals.model_matrix import (
    DEFAULT_COMPARISON_ARM,
    ComparisonConfig,
    ModelComparison,
    ModelSpec,
    compare_models,
    render_money,
)
from rewire.evals.model_matrix import render_markdown as render_comparison
from rewire.evals.model_matrix import write_results as write_comparison
from rewire.impact import (
    DEFAULT_MIN_CONFIDENCE,
    ImpactReport,
    analyse_impact,
    attach_snippets,
)
from rewire.llm import build_provider
from rewire.llm.base import LLMProvider
from rewire.sandbox import (
    CheckResult,
    Verdict,
    VerificationReport,
    VerificationRequest,
    verify,
)
from rewire.sandbox import CheckStatus as SandboxCheckStatus
from rewire.services import (
    MigrationOutcome,
    MigrationPolicy,
    MigrationRequest,
    MigrationStatus,
    MigrationTask,
    PublishOutcome,
    PublishRequest,
    PublishStatus,
    RepairOutcome,
    RepairPolicy,
    check_publishable,
    migrate_with_repair,
    publish,
    run_migration,
)
from rewire.watch import CheckOutcome as WatchOutcome
from rewire.watch import CheckStatus as WatchCheckStatus
from rewire.watch import Watch, WatchAction, WatchStore
from rewire.watch.monitor import (
    CheckPolicy,
    check_all,
    exit_code_for,
)
from rewire.watch.monitor import (
    accept as accept_watch,
)
from rewire.watch.store import validate_name

app = typer.Typer(
    name="rewire",
    help="Rewire - autonomous API migration and code-maintenance agent.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)

console = Console()
error_console = Console(stderr=True)

#: Exit code used when a preflight check or command fails in an expected way.
EXIT_FAILURE = 1

_STATUS_MARKUP: dict[CheckStatus, str] = {
    CheckStatus.OK: "[green]ok[/green]",
    CheckStatus.WARN: "[yellow]warn[/yellow]",
    CheckStatus.FAIL: "[red]fail[/red]",
}

_SEVERITY_MARKUP: dict[Severity, str] = {
    Severity.BREAKING: "[red]breaking[/red]",
    Severity.POTENTIALLY_BREAKING: "[yellow]potentially[/yellow]",
    Severity.NON_BREAKING: "[green]non-breaking[/green]",
}


class FailOn(StrEnum):
    """Threshold at which ``api-diff`` exits non-zero, for use in CI."""

    NEVER = "never"
    BREAKING = "breaking"
    POTENTIALLY_BREAKING = "potentially-breaking"
    ANY = "any"


class SearchMode(StrEnum):
    """Which search strategies ``search`` should use."""

    AST = "ast"
    TEXT = "text"
    BOTH = "both"


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"rewire {__version__}")
        raise typer.Exit


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the Rewire version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Emit DEBUG-level structured logs."),
    ] = False,
) -> None:
    """Configure logging before any subcommand runs."""
    settings = get_settings()
    if verbose:
        settings = settings.model_copy(update={"log_level": LogLevel.DEBUG})
    configure_from_settings(settings)


def _render_report(report: DoctorReport) -> None:
    table = Table(title="Rewire environment", title_justify="left", header_style="bold")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    for result in report.results:
        # Details and remedies quote paths, flags and versions, any of which
        # may contain square brackets that Rich would interpret as markup.
        table.add_row(escape(result.name), _STATUS_MARKUP[result.status], escape(result.detail))

    console.print(table)

    remedies = [r for r in report.results if r.status is not CheckStatus.OK and r.remedy]
    if remedies:
        console.print()
        console.print("[bold]Suggested fixes[/bold]")
        for result in remedies:
            console.print(f"  - {escape(result.name)}: {escape(result.remedy or '')}")


@app.command()
def doctor(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the report as JSON instead of a table."),
    ] = False,
) -> None:
    """Verify that the local environment can run Rewire.

    Checks the Python version, Git, the Docker daemon, ripgrep, LLM credentials
    and the writability of the data directory. Exits non-zero if any required
    dependency is unusable; warnings alone do not fail the command.
    """
    settings = get_settings()
    report = run_checks(settings)

    if as_json:
        console.print_json(json.dumps(report.model_dump(mode="json")))
    else:
        _render_report(report)

    if not report.ok:
        raise typer.Exit(code=EXIT_FAILURE)


def _should_fail(report: ChangeReport, fail_on: FailOn) -> bool:
    match fail_on:
        case FailOn.NEVER:
            return False
        case FailOn.BREAKING:
            return report.summary.breaking > 0
        case FailOn.POTENTIALLY_BREAKING:
            return report.summary.breaking + report.summary.potentially_breaking > 0
        case FailOn.ANY:
            return report.summary.total > 0
        case _:  # pragma: no cover - mypy proves the match above is exhaustive
            assert_never(fail_on)


def _render_change_report(report: ChangeReport, changes: list[ApiChange]) -> None:
    if not changes:
        console.print("[green]No changes detected at the requested severity.[/green]")
        return

    # One table per endpoint. Endpoint paths are the widest column by far and
    # repeat on every row, which squeezes the change type down to an ellipsis in
    # a normal-width terminal.
    grouped: dict[str, list[ApiChange]] = {}
    for change in changes:
        grouped.setdefault(change.endpoint or "(specification)", []).append(change)

    for endpoint, endpoint_changes in grouped.items():
        table = Table(title=endpoint, title_justify="left", title_style="bold cyan")
        table.add_column("Severity", no_wrap=True)
        table.add_column("Change", style="bold", no_wrap=True)
        table.add_column("Field", overflow="fold")

        for change in endpoint_changes:
            field = change.field_path or "-"
            if change.replacement:
                field = f"{field} [cyan]->[/cyan] {escape(change.replacement)}"
            else:
                field = escape(field)
            table.add_row(_SEVERITY_MARKUP[change.severity], escape(change.type.value), field)
        console.print(table)

    summary = report.summary
    console.print(
        f"\n[bold]{summary.total}[/bold] change(s) across "
        f"[bold]{summary.endpoints_affected}[/bold] endpoint(s): "
        f"[red]{summary.breaking} breaking[/red], "
        f"[yellow]{summary.potentially_breaking} potentially breaking[/yellow], "
        f"[green]{summary.non_breaking} non-breaking[/green]"
    )


@app.command(name="api-diff")
def api_diff(
    old_spec: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="Previous OpenAPI spec."),
    ],
    new_spec: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="New OpenAPI spec."),
    ],
    as_json: Annotated[bool, typer.Option("--json", help="Emit the full report as JSON.")] = False,
    min_severity: Annotated[
        Severity,
        typer.Option("--min-severity", help="Hide changes less severe than this."),
    ] = Severity.NON_BREAKING,
    fail_on: Annotated[
        FailOn,
        typer.Option("--fail-on", help="Exit non-zero when changes at this level are found."),
    ] = FailOn.NEVER,
) -> None:
    """Compare two OpenAPI specifications and report the changes between them.

    Fully deterministic: no LLM is involved, so the same pair of specifications
    always produces the same report. Use ``--fail-on breaking`` to turn this into
    a CI gate.
    """
    report = diff_specs(load_spec(old_spec), load_spec(new_spec))
    selected = report.filter(min_severity)

    if as_json:
        payload = report.model_dump(mode="json", exclude_none=True)
        payload["changes"] = [
            change.model_dump(mode="json", exclude_none=True) for change in selected
        ]
        console.print_json(json.dumps(payload))
    else:
        _render_change_report(report, selected)

    if _should_fail(report, fail_on):
        raise typer.Exit(code=EXIT_FAILURE)


@app.command()
def config() -> None:
    """Print the effective configuration, with secrets redacted."""
    settings: Settings = get_settings()
    console.print_json(json.dumps(settings.model_dump(mode="json"), default=str))


@app.command()
def analyze(
    repo: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, help="Repository root."),
    ],
    as_json: Annotated[bool, typer.Option("--json", help="Emit the full index as JSON.")] = False,
    include_tests: Annotated[
        bool, typer.Option("--tests/--no-tests", help="Include test files in the index.")
    ] = True,
) -> None:
    """Index a repository's Python code and report what it contains.

    Deterministic and LLM-free: imports, definitions, call sites, references,
    environment reads, declared dependencies and entry points are all extracted
    by parsing, not by pattern matching.
    """
    index = build_index(repo, limits=DiscoveryLimits(include_tests=include_tests))

    if as_json:
        console.print_json(index.model_dump_json(exclude_none=True))
        return
    _render_index(index)


def _render_index(index: RepositoryIndex) -> None:
    stats = index.stats
    console.print(f"[bold]{index.root}[/bold]")
    console.print(
        f"  {stats.files_indexed} file(s) indexed, {stats.test_files} test, "
        f"{stats.total_lines} lines in {stats.duration_seconds:.2f}s"
    )
    console.print(
        f"  {stats.symbols} symbols, {stats.imports} imports, "
        f"{stats.calls} calls, {stats.references} references"
    )
    if stats.files_failed or stats.files_skipped:
        console.print(
            f"  [yellow]{stats.files_failed} unparseable, {stats.files_skipped} skipped[/yellow]"
        )

    modules = index.imported_modules()
    if modules:
        declared = index.declared_dependency_names()
        table = Table(title="Imported modules", title_justify="left", title_style="bold cyan")
        table.add_column("Module", no_wrap=True)
        table.add_column("Imports", justify="right")
        table.add_column("Declared", no_wrap=True)
        for module, count in list(modules.items())[:15]:
            is_declared = module.lower().replace("_", "-") in declared
            table.add_row(escape(module), str(count), "[green]yes[/green]" if is_declared else "-")
        console.print(table)

    if index.entry_points:
        table = Table(title="Entry points", title_justify="left", title_style="bold cyan")
        table.add_column("Kind", no_wrap=True)
        table.add_column("Location", overflow="fold")
        table.add_column("Detail", overflow="fold")
        for entry in index.entry_points:
            location = f"{entry.file}:{entry.line}" if entry.line else entry.file
            table.add_row(escape(entry.kind.value), escape(location), escape(entry.detail))
        console.print(table)

    for failed in index.failed_files:
        reason = escape(failed.parse_error or "")
        console.print(f"[yellow]unparseable[/yellow] {escape(failed.path)}: {reason}")


def _render_references(references: list[Reference], root: Path) -> None:
    table = Table(title="AST references", title_justify="left", title_style="bold cyan")
    table.add_column("Location", no_wrap=True)
    table.add_column("Kind", no_wrap=True)
    table.add_column("Evidence", justify="right")
    table.add_column("Context", overflow="fold")
    for reference in references:
        table.add_row(
            escape(f"{reference.file}:{reference.line}"),
            escape(reference.kind.value),
            f"{reference.evidence:.1f}",
            escape(reference.context or reference.enclosing_symbol or "-"),
        )
    console.print(table)


def _render_text_matches(matches: list[TextMatch], backend_name: str) -> None:
    table = Table(
        title=f"Text matches ({backend_name})", title_justify="left", title_style="bold cyan"
    )
    table.add_column("Location", no_wrap=True)
    table.add_column("Line", overflow="fold")
    for match in matches:
        table.add_row(escape(f"{match.file}:{match.line}"), escape(match.text.strip()))
    console.print(table)


@app.command()
def search(
    repo: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, help="Repository root."),
    ],
    pattern: Annotated[str, typer.Argument(help="Name or pattern to look for.")],
    mode: Annotated[
        SearchMode, typer.Option("--mode", help="Search strategy to use.")
    ] = SearchMode.BOTH,
    kind: Annotated[
        list[ReferenceKind] | None,
        typer.Option("--kind", help="Restrict AST results to these reference kinds."),
    ] = None,
    regex: Annotated[
        bool, typer.Option("--regex", help="Treat the pattern as a regular expression.")
    ] = False,
    backend: Annotated[
        str, typer.Option("--backend", help="Text backend: auto, ripgrep or python.")
    ] = "auto",
    as_json: Annotated[bool, typer.Option("--json", help="Emit results as JSON.")] = False,
) -> None:
    """Find a name in a repository, by parsing and by text search.

    The two strategies answer different questions. AST search knows that
    ``max_tokens=`` is a keyword argument on a specific call and grades it
    accordingly; text search finds every occurrence including comments,
    templates and files Rewire cannot parse. Running both shows the gap between
    them, which is exactly what Phase 10's ablation measures.
    """
    root = resolve_repository_root(repo)
    references: list[Reference] = []
    matches: list[TextMatch] = []
    backend_name = ""

    if mode in (SearchMode.AST, SearchMode.BOTH):
        index = build_index(root)
        references = index.find_references(pattern, kinds=frozenset(kind) if kind else None)

    if mode in (SearchMode.TEXT, SearchMode.BOTH):
        searcher = get_backend(backend)
        backend_name = searcher.name
        matches = searcher.search(root, pattern, regex=regex)

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "pattern": pattern,
                    "root": str(root),
                    "text_backend": backend_name or None,
                    "references": [r.model_dump(mode="json") for r in references],
                    "text_matches": [m.model_dump(mode="json") for m in matches],
                }
            )
        )
        return

    if references:
        _render_references(references, root)
    if matches:
        _render_text_matches(matches, backend_name)
    if not references and not matches:
        console.print(f"[yellow]No occurrences of {escape(pattern)!r} found.[/yellow]")
        return

    if mode is SearchMode.BOTH:
        console.print(
            f"\n[bold]{len(references)}[/bold] parsed reference(s), "
            f"[bold]{len(matches)}[/bold] text match(es)"
        )


def _confidence_markup(confidence: float) -> str:
    if confidence >= 0.9:
        return f"[red]{confidence:.2f}[/red]"
    if confidence >= 0.6:
        return f"[yellow]{confidence:.2f}[/yellow]"
    return f"[green]{confidence:.2f}[/green]"


def _render_impact(report: ImpactReport, *, explain: bool) -> None:
    if not report.impacts or report.summary.locations == 0:
        console.print("[green]No affected code found for the detected changes.[/green]")
        return

    for impact in report.impacts:
        if not impact.locations:
            continue
        change = impact.change
        subject = escape(change.field_path or change.endpoint or "")
        heading = f"[bold]{escape(change.type.value)} — {subject}[/bold]"
        if change.replacement:
            heading += f" [cyan]->[/cyan] [bold]{escape(change.replacement)}[/bold]"
        console.print(
            f"\n{heading} {_SEVERITY_MARKUP[change.severity]} {escape(change.endpoint or '')}"
        )

        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("Conf", no_wrap=True)
        table.add_column("Location", no_wrap=True)
        table.add_column("Symbol", overflow="fold")
        table.add_column("Code", overflow="fold")
        for location in impact.locations:
            table.add_row(
                _confidence_markup(location.confidence),
                escape(location.location),
                escape(location.symbol or "-"),
                escape(location.snippet or ""),
            )
        console.print(table)

        if explain:
            for location in impact.locations:
                console.print(f"  [dim]{escape(location.location)}[/dim]")
                for signal in location.signals:
                    colour = "green" if signal.weight > 0 else "red"
                    console.print(
                        f"    [{colour}]{signal.weight:+.1f}[/{colour}]  {escape(signal.detail)}"
                    )

    summary = report.summary
    console.print(
        f"\n[bold]{summary.locations}[/bold] location(s) across "
        f"[bold]{summary.files_affected}[/bold] file(s), from "
        f"{summary.changes_with_impact}/{summary.changes_analysed} change(s); "
        f"{summary.test_locations} in tests"
    )


@app.command()
def impact(
    repo: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, help="Repository root."),
    ],
    old_spec: Annotated[
        Path,
        typer.Option("--old", exists=True, dir_okay=False, help="Previous OpenAPI spec."),
    ],
    new_spec: Annotated[
        Path, typer.Option("--new", exists=True, dir_okay=False, help="New OpenAPI spec.")
    ],
    package: Annotated[
        list[str] | None,
        typer.Option("--package", help="Package the API belongs to; inferred when omitted."),
    ] = None,
    min_confidence: Annotated[
        float,
        typer.Option("--min-confidence", min=0.0, max=1.0, help="Discard weaker locations."),
    ] = DEFAULT_MIN_CONFIDENCE,
    min_severity: Annotated[
        Severity, typer.Option("--min-severity", help="Ignore less severe changes.")
    ] = Severity.NON_BREAKING,
    explain: Annotated[
        bool, typer.Option("--explain", help="Show the evidence behind every score.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the report as JSON.")] = False,
) -> None:
    """Find the code in a repository that an API change affects.

    Joins deterministic change detection to a deterministic repository index and
    scores every candidate against an auditable evidence model. No LLM is
    involved, and nothing is modified: this command only reports.
    """
    changes = diff_specs(load_spec(old_spec), load_spec(new_spec))
    index = build_index(repo)
    report = attach_snippets(
        analyse_impact(
            changes,
            index,
            packages=tuple(package or ()),
            min_confidence=min_confidence,
            min_severity=min_severity,
        ),
        index.root,
    )

    if as_json:
        console.print_json(report.model_dump_json(exclude_none=True))
        return
    _render_impact(report, explain=explain)


eval_app = typer.Typer(help="Run Rewire's benchmarks.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")


@eval_app.command("impact")
def eval_impact(
    dataset: Annotated[
        Path,
        typer.Option("--dataset", exists=True, file_okay=False, help="Labelled cases."),
    ] = Path("evals/datasets/impact"),
    min_confidence: Annotated[
        float, typer.Option("--min-confidence", min=0.0, max=1.0)
    ] = DEFAULT_MIN_CONFIDENCE,
    results: Annotated[
        Path, typer.Option("--results", help="Directory to write latest.json/latest.md.")
    ] = Path("evals/results"),
    write: Annotated[bool, typer.Option("--write/--no-write", help="Write result files.")] = True,
    fail_under: Annotated[
        float,
        typer.Option("--fail-under", min=0.0, max=1.0, help="Exit non-zero below this F1."),
    ] = 0.0,
) -> None:
    """Score impact analysis against labelled ground truth.

    Reports precision, recall and F1 at two granularities. Location metrics are
    the stricter of the two and are always shown, because reporting only the
    file-level number would flatter the result.
    """
    result = run_evaluation(dataset, min_confidence=min_confidence, version=__version__)
    console.print(render_markdown(result))

    if write:
        json_path, markdown_path = write_results(result, results)
        console.print(
            f"\n[dim]wrote {escape(str(json_path))} and {escape(str(markdown_path))}[/dim]"
        )

    if result.location_metrics.f1 < fail_under:
        console.print(
            f"[red]location F1 {result.location_metrics.f1:.3f} "
            f"is below the required {fail_under:.3f}[/red]"
        )
        raise typer.Exit(code=EXIT_FAILURE)


def _render_migration(result: MigrationResult, *, show_diff: bool, verified: bool = False) -> None:
    summary = result.summary
    verdict = "[yellow]CANDIDATE[/yellow]" if summary.produced_patch else "[red]NO PATCH[/red]"
    console.print(f"\n{verdict}  {escape(summary.outcome)}")
    console.print(
        f"  {summary.iterations} iteration(s), {summary.tool_calls} tool call(s)"
        f" ({summary.tool_errors} error(s)), {summary.files_changed} file(s) changed"
    )
    cost = f"${summary.cost_usd:.4f}" if summary.cost_usd is not None else "unknown"
    console.print(
        f"  {summary.usage.total_tokens} tokens"
        f" ({summary.usage.input_tokens} in / {summary.usage.output_tokens} out),"
        f" cost {cost}, {summary.duration_seconds:.1f}s"
    )

    if result.final_message:
        console.print(f"\n[bold]Agent summary[/bold]\n{escape(result.final_message)}")

    if result.patch.files:
        console.print("\n[bold]Files changed[/bold]")
        for change in result.patch.changes:
            if change.changed:
                console.print(
                    f"  {escape(change.file)}"
                    f"  [green]+{change.added_lines}[/green]"
                    f" [red]-{change.removed_lines}[/red]"
                )

    if show_diff and not result.patch.is_empty:
        console.print("\n[bold]Candidate diff[/bold]")
        console.print(Syntax(result.patch.unified_diff(), "diff", theme="ansi_dark"))

    if not verified:
        console.print(
            "\n[dim]This patch is a proposal. Rewire has not executed it: no tests were "
            "run and nothing was written to your repository. Run with --verify to "
            "execute the repository's own checks against it in a sandbox.[/dim]"
        )


#: How each verdict is coloured, and what it means in one word.
VERDICT_STYLE: dict[Verdict, str] = {
    Verdict.VERIFIED: "green",
    Verdict.WEAKENED: "red",
    Verdict.REGRESSED: "red",
    Verdict.INCONCLUSIVE: "yellow",
    Verdict.ERRORED: "red",
}

STATUS_STYLE: dict[SandboxCheckStatus, str] = {
    SandboxCheckStatus.PASSED: "green",
    SandboxCheckStatus.FAILED: "red",
    SandboxCheckStatus.TIMED_OUT: "red",
    SandboxCheckStatus.UNAVAILABLE: "yellow",
    SandboxCheckStatus.SKIPPED: "dim",
}


def _status_cell(result: CheckResult | None) -> str:
    """Render a check status, or a dash where the check never ran."""
    if result is None:
        return "[dim]-[/dim]"
    style = STATUS_STYLE[result.status]
    return f"[{style}]{result.status.value}[/{style}]"


def _verification_request(
    settings: Settings, *, image: str | None, timeout: int | None
) -> VerificationRequest:
    """Build a sandbox policy from settings, with per-invocation overrides."""
    sandbox = settings.sandbox
    return VerificationRequest(
        image=image or sandbox.image,
        check_timeout_seconds=timeout or sandbox.timeout_seconds,
        memory_limit_mb=sandbox.memory_limit_mb,
        cpu_limit=sandbox.cpu_limit,
        pids_limit=sandbox.pids_limit,
        read_only_rootfs=sandbox.read_only_rootfs,
        max_repo_size_mb=sandbox.max_repo_size_mb,
    )


def _render_verification(report: VerificationReport) -> None:
    """Print the evidence behind a verdict, including what was never measured."""
    style = VERDICT_STYLE[report.verdict]
    console.print(f"\n[{style}]{report.verdict.value.upper()}[/{style}]  {escape(report.reason)}")

    table = Table(title="Sandbox checks", title_justify="left")
    table.add_column("Check")
    table.add_column("Tool")
    table.add_column("Before")
    table.add_column("After")
    table.add_column("Detail", overflow="fold")

    before = {result.kind: result for result in report.baseline}
    after = {result.kind: result for result in report.patched}
    for kind in sorted(before | after, key=lambda kind: kind.strength):
        baseline = before.get(kind)
        patched = after.get(kind)
        shown = patched or baseline
        assert shown is not None  # noqa: S101 - keys come from these two mappings
        table.add_row(
            kind.value,
            shown.name,
            _status_cell(baseline),
            _status_cell(patched),
            escape(shown.reason),
        )
    console.print(table)

    if report.weakenings:
        console.print("\n[bold]Checks this patch removed from the repository's tests[/bold]")
        for weakening in report.weakenings:
            marker = "[red]![/red]" if weakening.withholds_verdict else "[yellow]?[/yellow]"
            console.print(f"  {marker} {escape(weakening.describe())}")
        console.print(
            "[dim]A test is evidence. Rewire will not vouch for a patch whose tests were "
            "weakened to make it pass; entries marked ? are reported but not disqualifying."
            "[/dim]"
        )

    if report.install is not None and not report.install.passed and report.install.outcome:
        console.print("\n[bold]Dependency installation failed[/bold]")
        console.print(f"[dim]{escape(report.install.outcome.tail(15))}[/dim]")

    for result in report.patched:
        if result.status in {SandboxCheckStatus.FAILED, SandboxCheckStatus.TIMED_OUT}:
            console.print(f"\n[bold]{escape(result.name)} output[/bold]")
            console.print(f"[dim]{escape(result.outcome.tail(20) if result.outcome else '')}[/dim]")

    console.print(
        f"\n[dim]image {escape(report.image)}, {report.duration_seconds:.1f}s, "
        f"checks ran with no network[/dim]"
    )


def _write_diff(patch: CandidatePatch, target: Path | None) -> None:
    """Write a patch's unified diff to a file, if one was asked for."""
    if target is None or patch.is_empty:
        return
    target.write_text(patch.unified_diff(), encoding="utf-8")
    console.print(f"[dim]wrote diff to {escape(str(target))}[/dim]")


def _render_repair(outcome: RepairOutcome, *, show_diff: bool) -> None:
    """Print the attempt-by-attempt story, then the evidence for the final one.

    The per-attempt table is the point of this phase: it shows whether repair
    changed the answer, which is the only way "the repair loop helps" stops
    being an assertion.
    """
    table = Table(title="Attempts", title_justify="left")
    table.add_column("#")
    table.add_column("Files")
    table.add_column("Verdict")
    table.add_column("Tokens", justify="right")
    table.add_column("Why", overflow="fold")

    for attempt in outcome.attempts:
        verdict = attempt.verdict
        style = VERDICT_STYLE[verdict] if verdict else "red"
        rendered = f"[{style}]{verdict.value}[/{style}]" if verdict else "[red]no patch[/red]"
        reason = attempt.report.reason if attempt.report else attempt.result.summary.outcome
        table.add_row(
            str(attempt.number),
            str(len(attempt.patch.files)),
            rendered,
            str(attempt.result.summary.usage.total_tokens),
            escape(reason),
        )
    console.print()
    console.print(table)

    cost = f"${outcome.total_cost_usd:.4f}" if outcome.total_cost_usd is not None else "unknown"
    console.print(
        f"[dim]{len(outcome.attempts)} attempt(s), {outcome.total_tokens} tokens, "
        f"cost {cost}, {outcome.duration_seconds:.1f}s — "
        f"stopped because {escape(outcome.stopped_because)}[/dim]"
    )
    if outcome.repaired:
        console.print(
            "[green]Repair changed the outcome:[/green] the first attempt failed "
            "verification and a later one passed it."
        )

    best = outcome.best
    if best is not None and best.report is not None:
        _render_verification(best.report)

    if show_diff and not outcome.patch.is_empty:
        heading = "Verified diff" if outcome.verified else "Final candidate diff (unverified)"
        console.print(f"\n[bold]{heading}[/bold]")
        console.print(Syntax(outcome.patch.unified_diff(), "diff", theme="ansi_dark"))


@app.command(name="verify")
def verify_repo(
    repo: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, help="Repository root."),
    ],
    image: Annotated[
        str | None, typer.Option("--image", help="Docker image to run the checks in.")
    ] = None,
    timeout: Annotated[
        int | None, typer.Option("--timeout", min=1, help="Seconds allowed per check.")
    ] = None,
    no_install: Annotated[
        bool,
        typer.Option("--no-install", help="Skip dependency installation and stay fully offline."),
    ] = False,
) -> None:
    """Run a repository's own checks in the sandbox and report what they prove.

    This is the baseline measurement: no patch is applied. It answers whether a
    repository is verifiable at all, which is worth knowing before an agent is
    ever pointed at it — a project whose tests already fail cannot confirm any
    migration.
    """
    settings = get_settings()
    request = _verification_request(settings, image=image, timeout=timeout)
    report = verify(repo, request=request.model_copy(update={"install": not no_install}))
    _render_verification(report)


def _render_benchmark(result: BenchmarkResult) -> None:
    """Print the headline table: what Rewire claimed, and what was true."""
    table = Table(title="Migration benchmark", title_justify="left")
    table.add_column("Arm")
    table.add_column("Correct", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column("Claimed", justify="right")
    table.add_column("Overclaimed", justify="right")
    table.add_column("Repaired", justify="right")
    table.add_column("Cost", justify="right")

    for arm in result.arms:
        cost = f"${arm.total_cost_usd:.2f}" if arm.total_cost_usd is not None else "?"
        over = (
            f"[red]{arm.overclaimed}[/red]"
            if arm.overclaimed
            else f"[green]{arm.overclaimed}[/green]"
        )
        table.add_row(
            arm.arm,
            f"{arm.succeeded}/{arm.total}",
            f"{arm.success_rate:.0%}",
            str(arm.claimed),
            over,
            str(arm.repaired),
            cost,
        )
    console.print()
    console.print(table)
    console.print(
        "[dim]Correct is judged by a contract test injected after the patch and never "
        "visible to the agent. Overclaimed is where Rewire vouched for a patch that "
        "test rejected.[/dim]"
    )
    if result.ungraded_cases:
        console.print(
            f"[yellow]{len(result.ungraded_cases)} case(s) ship no hidden test and are "
            "graded on Rewire's own word.[/yellow]"
        )


@eval_app.command("migrate")
def eval_migrate(
    dataset: Annotated[
        Path,
        typer.Option("--dataset", exists=True, file_okay=False, help="Benchmark dataset root."),
    ] = Path("evals/datasets/migration"),
    case: Annotated[
        list[str] | None, typer.Option("--case", help="Run only these case identifiers.")
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=0, help="Stop after this many cases per arm.")
    ] = 0,
    arm: Annotated[
        list[str] | None, typer.Option("--arm", help="Run only these arms (no-repair, repair).")
    ] = None,
    write: Annotated[
        bool, typer.Option("--write/--no-write", help="Write results under evals/results/.")
    ] = True,
    results_dir: Annotated[
        Path, typer.Option("--results-dir", help="Where to write results.")
    ] = Path("evals/results"),
) -> None:
    """Run the migration benchmark and publish what it measured.

    Every patch is graded twice: once by Rewire's own sandbox, and once by a
    contract test injected after the patch is applied that the agent never saw.
    The second is the number that means something; the gap between them is how
    often Rewire's verification was fooled.

    This costs real model calls and real container time. Use --limit or --case
    to run a slice.
    """
    settings = get_settings()
    provider = build_provider(settings.llm)
    cases = load_migration_cases(dataset)

    arms = tuple(a for a in DEFAULT_ARMS if not arm or a.name in arm)
    if not arms:
        raise ConfigurationError(
            "no matching arm", requested=list(arm or ()), available=[a.name for a in DEFAULT_ARMS]
        )

    settings.ensure_data_dirs()
    result = run_benchmark(
        BenchmarkConfig(
            dataset=dataset,
            arms=arms,
            only=tuple(case or ()),
            limit=limit,
            verification=_verification_request(settings, image=None, timeout=None),
            results_dir=results_dir,
            # --no-write means "leave evals/results alone", which has to include
            # the crash-recovery partial or the flag quietly lies.
            incremental=write,
        ),
        cases,
        provider=provider,
        settings=settings,
    )

    _render_benchmark(result)
    if write:
        json_path, markdown_path = write_benchmark(result, results_dir)
        console.print(f"[dim]wrote {escape(str(json_path))} and {escape(str(markdown_path))}[/dim]")
    else:
        console.print(render_benchmark(result))


def _render_comparison(comparison: ModelComparison) -> None:
    """Print the headline table, and what the numbers do not support."""
    table = Table(title="Model comparison", title_justify="left")
    table.add_column("Model")
    table.add_column("Correct", justify="right")
    table.add_column("95% CI", justify="right")
    table.add_column("Claimed", justify="right")
    table.add_column("Overclaimed", justify="right")
    table.add_column("Cost", justify="right")

    for run in comparison.compared:
        cost = render_money(run.cost_usd)
        over = f"[red]{run.overclaimed}[/red]" if run.overclaimed else "[green]0[/green]"
        table.add_row(
            run.label,
            f"{run.succeeded}/{run.total}",
            run.interval.render(),
            str(run.claimed),
            over,
            cost,
        )
    console.print()
    console.print(table)

    for pair in comparison.pairs():
        style = "green" if pair.is_significant else "yellow"
        console.print(f"[{style}]{escape(pair.verdict())}[/{style}]")

    if unsolved := comparison.unsolved():
        console.print(
            f"[yellow]{len(unsolved)} case(s) no model solved: "
            f"{escape(', '.join(unsolved))}. That is Rewire's ceiling, not the model's."
            "[/yellow]"
        )
    for run in comparison.skipped:
        console.print(f"[dim]skipped {escape(run.label)}: {escape(run.skipped)}[/dim]")


@eval_app.command("models")
def eval_models(
    model: Annotated[
        list[str] | None,
        typer.Option("--model", help="Model to compare, as provider:model. Repeatable."),
    ] = None,
    dataset: Annotated[
        Path,
        typer.Option("--dataset", exists=True, file_okay=False, help="Benchmark dataset root."),
    ] = Path("evals/datasets/migration"),
    case: Annotated[
        list[str] | None, typer.Option("--case", help="Run only these case identifiers.")
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=0, help="Stop after this many cases per model.")
    ] = 0,
    max_attempts: Annotated[
        int, typer.Option("--max-attempts", min=1, max=10, help="Repair budget, same for all.")
    ] = DEFAULT_COMPARISON_ARM.max_attempts,
    write: Annotated[
        bool, typer.Option("--write/--no-write", help="Write results under evals/results/.")
    ] = True,
    results_dir: Annotated[
        Path, typer.Option("--results-dir", help="Where to write results.")
    ] = Path("evals/results"),
) -> None:
    """Run the same benchmark across several models and compare them.

    Every model gets identical cases, prompts, tools and repair budget, and every
    patch is graded by the same hidden contract tests, so the only thing that
    differs between columns is the model.

    Differences are reported with a confidence interval and an exact paired
    significance test. On a ten-case dataset most of them will not be separable
    from chance, and the report says so rather than declaring a winner.

    A model whose provider has no API key configured is listed as skipped with
    that reason, never quietly dropped.

    This costs real model calls and real container time.
    """
    if not model:
        raise ConfigurationError(
            "no models to compare",
            remedy="pass --model provider:model at least once, e.g. --model openai:gpt-4o",
        )

    settings = get_settings()
    specs = tuple(ModelSpec.parse(text) for text in model)
    cases = load_migration_cases(dataset)
    settings.ensure_data_dirs()

    arm = DEFAULT_COMPARISON_ARM.model_copy(update={"max_attempts": max_attempts})
    with console.status("[bold]comparing models") as status:
        comparison = compare_models(
            ComparisonConfig(
                dataset=dataset,
                models=specs,
                arm=arm,
                only=tuple(case or ()),
                limit=limit,
                results_dir=results_dir,
                incremental=write,
                progress=lambda message: status.update(f"[bold]{escape(message)}"),
            ),
            cases,
            settings=settings,
        )

    _render_comparison(comparison)
    if write:
        json_path, markdown_path = write_comparison(comparison, results_dir)
        console.print(f"[dim]wrote {escape(str(json_path))} and {escape(str(markdown_path))}[/dim]")
    else:
        console.print(render_comparison(comparison))


def _render_ablation(result: BenchmarkResult) -> None:
    """Print the arms, what each lost, and whether the gaps mean anything."""
    table = Table(title="Agent ablations", title_justify="left")
    table.add_column("Arm")
    table.add_column("Lost")
    table.add_column("Correct", justify="right")
    table.add_column("95% CI", justify="right")
    table.add_column("Overclaimed", justify="right")

    columns = ablation_contenders(result)
    for column in columns:
        over = f"[red]{column.overclaimed}[/red]" if column.overclaimed else "[green]0[/green]"
        table.add_row(
            column.label,
            escape(column.note),
            f"{column.succeeded}/{column.total}",
            column.interval.render(),
            over,
        )
    console.print()
    console.print(table)

    for pair in comparison_pairs(columns):
        style = "green" if pair.is_significant else "yellow"
        console.print(f"[{style}]{escape(pair.verdict())}[/{style}]")

    if unreached := comparison_unsolved(columns):
        console.print(
            f"[yellow]{len(unreached)} case(s) no arm solved: {escape(', '.join(unreached))}."
            " No configuration of the harness reached them.[/yellow]"
        )


@eval_app.command("ablate")
def eval_ablate(
    dataset: Annotated[
        Path,
        typer.Option("--dataset", exists=True, file_okay=False, help="Benchmark dataset root."),
    ] = Path("evals/datasets/migration"),
    arm: Annotated[list[str] | None, typer.Option("--arm", help="Run only these arms.")] = None,
    case: Annotated[
        list[str] | None, typer.Option("--case", help="Run only these case identifiers.")
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=0, help="Stop after this many cases per arm.")
    ] = 0,
    write: Annotated[
        bool, typer.Option("--write/--no-write", help="Write results under evals/results/.")
    ] = True,
    results_dir: Annotated[
        Path, typer.Option("--results-dir", help="Where to write results.")
    ] = Path("evals/results"),
) -> None:
    """Measure what each part of the harness is worth by taking it away.

    Every arm runs the same cases against the same model with the same repair
    budget; the only thing that differs is what the agent is given. Two arms
    withhold the ranked impact locations, one withholds the tools for searching
    beyond them, and one is the shipped configuration as a control.

    Differences are reported with a confidence interval and an exact paired
    significance test, which on a ten-case dataset will usually decline to
    separate the arms.

    This costs real model calls and real container time.
    """
    settings = get_settings()
    provider = build_provider(settings.llm)
    cases = load_migration_cases(dataset)

    arms = tuple(a for a in DEFAULT_ABLATIONS if not arm or a.name in arm)
    if not arms:
        raise ConfigurationError(
            "no matching arm",
            requested=list(arm or ()),
            available=[a.name for a in DEFAULT_ABLATIONS],
        )

    settings.ensure_data_dirs()
    with console.status("[bold]running ablations") as status:
        result = run_benchmark(
            BenchmarkConfig(
                dataset=dataset,
                arms=arms,
                only=tuple(case or ()),
                limit=limit,
                verification=_verification_request(settings, image=None, timeout=None),
                results_dir=results_dir,
                incremental=write,
                progress=lambda message: status.update(f"[bold]{escape(message)}"),
            ),
            cases,
            provider=provider,
            settings=settings,
        )

    _render_ablation(result)
    if write:
        json_path, markdown_path = write_ablation(result, results_dir)
        console.print(f"[dim]wrote {escape(str(json_path))} and {escape(str(markdown_path))}[/dim]")
    else:
        console.print(render_ablation(result))


@app.command()
def propose(
    repo: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, help="Repository root."),
    ],
    old_spec: Annotated[
        Path, typer.Option("--old", exists=True, dir_okay=False, help="Previous OpenAPI spec.")
    ],
    new_spec: Annotated[
        Path, typer.Option("--new", exists=True, dir_okay=False, help="New OpenAPI spec.")
    ],
    package: Annotated[
        list[str] | None, typer.Option("--package", help="Package the API belongs to.")
    ] = None,
    max_iterations: Annotated[
        int, typer.Option("--max-iterations", min=1, max=50, help="Model turns allowed.")
    ] = 12,
    max_tool_calls: Annotated[
        int, typer.Option("--max-tool-calls", min=1, help="Tool invocations allowed.")
    ] = 40,
    show_diff: Annotated[
        bool, typer.Option("--diff/--no-diff", help="Print the candidate diff.")
    ] = True,
    write_patch_to: Annotated[
        Path | None,
        typer.Option("--write-diff", help="Write the unified diff to a file."),
    ] = None,
    run_verification: Annotated[
        bool,
        typer.Option("--verify/--no-verify", help="Execute the repository's checks in a sandbox."),
    ] = False,
    image: Annotated[
        str | None, typer.Option("--image", help="Docker image used for verification.")
    ] = None,
    repair: Annotated[
        bool,
        typer.Option(
            "--repair/--no-repair",
            help="Feed sandbox failures back to the agent and retry. Implies --verify.",
        ),
    ] = False,
    max_attempts: Annotated[
        int, typer.Option("--max-attempts", min=1, max=10, help="Attempts allowed with --repair.")
    ] = 3,
) -> None:
    """Ask the agent to propose a migration patch. Nothing is modified.

    Runs the deterministic pipeline first — spec diff, repository index, impact
    analysis — and gives the agent only that plus a restricted set of read-only
    tools. The result is a candidate patch: Rewire has not executed it, and the
    agent is not permitted to claim it works.
    """
    settings = get_settings()
    provider = build_provider(settings.llm)

    changes = diff_specs(load_spec(old_spec), load_spec(new_spec))
    index = build_index(repo)
    impact = analyse_impact(changes, index, packages=tuple(package or ()))

    if impact.summary.locations == 0:
        console.print("[green]No affected code found; nothing to migrate.[/green]")
        return

    settings.ensure_data_dirs()
    agent = MigrationAgent(
        provider,
        budget=AgentBudget(
            max_iterations=max_iterations,
            max_tool_calls=max_tool_calls,
            max_tokens=settings.agent.max_tokens_per_task,
            max_files=settings.agent.max_files_per_patch,
            max_output_tokens=settings.llm.max_output_tokens,
        ),
        runs_dir=settings.runs_dir,
    )
    workspace = Workspace.open(repo)
    request = _verification_request(settings, image=image, timeout=None)

    if repair:
        outcome = migrate_with_repair(
            agent=agent,
            repository=repo,
            workspace=workspace,
            index=index,
            changes=changes,
            impact=impact,
            request=request,
            policy=RepairPolicy(
                max_attempts=max_attempts, max_total_tokens=settings.agent.max_tokens_per_task * 3
            ),
        )
        _write_diff(outcome.patch, write_patch_to)
        _render_repair(outcome, show_diff=show_diff)
        if outcome.verified is None:
            raise typer.Exit(code=EXIT_FAILURE)
        return

    result = agent.run(workspace=workspace, index=index, changes=changes, impact=impact)
    _write_diff(result.patch, write_patch_to)
    _render_migration(result, show_diff=show_diff, verified=run_verification)
    console.print(f"[dim]trace: {escape(str(settings.runs_dir / result.summary.run_id))}[/dim]")

    if not result.summary.produced_patch:
        raise typer.Exit(code=EXIT_FAILURE)

    if not run_verification:
        return

    report = verify(repo, result.patch, request=request)
    _render_verification(report)
    if not report.verified:
        raise typer.Exit(code=EXIT_FAILURE)


#: How each terminal status is coloured. Four of the seven are successes.
MIGRATION_STATUS_STYLE: dict[MigrationStatus, str] = {
    MigrationStatus.NO_BREAKING_CHANGES: "green",
    MigrationStatus.NO_AFFECTED_CODE: "green",
    MigrationStatus.APPLIED: "green",
    MigrationStatus.VERIFIED: "green",
    MigrationStatus.REFUSED: "yellow",
    MigrationStatus.UNVERIFIED: "yellow",
    MigrationStatus.NO_PATCH: "red",
}


def _render_outcome(outcome: MigrationOutcome, *, show_diff: bool) -> None:
    """Print what the run concluded, and the evidence behind it."""
    style = MIGRATION_STATUS_STYLE[outcome.status]
    console.print(
        f"\n[{style}]{outcome.status.value.upper().replace('_', ' ')}[/{style}]"
        f"  {escape(outcome.summary_line())}"
    )

    if outcome.changes is not None:
        summary = outcome.changes.summary
        locations = outcome.impact.summary.locations if outcome.impact else 0
        console.print(
            f"[dim]{summary.total} change(s), {summary.breaking} breaking; "
            f"{locations} affected location(s)[/dim]"
        )

    if outcome.repair is not None and outcome.repair.attempts:
        _render_repair(outcome.repair, show_diff=show_diff and not outcome.written)

    if outcome.written:
        console.print("\n[bold]Files written[/bold]")
        for path in outcome.written:
            console.print(f"  {escape(path)}")
        console.print(
            "\n[dim]Review with `git diff`; undo with "
            f"`git checkout -- {escape(' '.join(outcome.written))}`.[/dim]"
        )

    console.print(f"[dim]run {escape(outcome.run_id)}, {outcome.duration_seconds:.1f}s[/dim]")


@app.command()
def migrate(
    repo: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, readable=True, help="Repository root."),
    ],
    old_spec: Annotated[
        Path, typer.Option("--old", exists=True, dir_okay=False, help="Previous OpenAPI spec.")
    ],
    new_spec: Annotated[
        Path, typer.Option("--new", exists=True, dir_okay=False, help="New OpenAPI spec.")
    ],
    package: Annotated[
        list[str] | None, typer.Option("--package", help="Package the API belongs to.")
    ] = None,
    apply_patch: Annotated[
        bool,
        typer.Option("--apply", help="Write the verified patch into the working tree."),
    ] = False,
    allow_dirty: Annotated[
        bool,
        typer.Option("--allow-dirty", help="Permit --apply on a tree with uncommitted changes."),
    ] = False,
    max_attempts: Annotated[
        int, typer.Option("--max-attempts", min=1, max=10, help="Repair attempts allowed.")
    ] = 3,
    show_diff: Annotated[
        bool, typer.Option("--diff/--no-diff", help="Print the resulting diff.")
    ] = True,
    write_patch_to: Annotated[
        Path | None, typer.Option("--write-diff", help="Write the unified diff to a file.")
    ] = None,
    pull_request: Annotated[
        bool,
        typer.Option("--pull-request", help="Open a pull request for the verified patch."),
    ] = False,
    draft: Annotated[
        bool, typer.Option("--draft", help="Open the pull request as a draft.")
    ] = False,
    base: Annotated[
        str, typer.Option("--base", help="Branch to propose into. Defaults to the repo's own.")
    ] = "",
    branch_prefix: Annotated[
        str, typer.Option("--branch-prefix", help="Leading segment of the created branch name.")
    ] = "rewire",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="With --pull-request: branch and commit, but never push."),
    ] = False,
) -> None:
    """Detect, locate, patch, verify, repair — and optionally apply or propose.

    The whole pipeline in one command. Without --apply nothing is written and
    this is a report; with --apply a *verified* patch is written to the working
    tree, and only ever there.

    With --pull-request the verified patch goes onto a new branch and becomes a
    pull request instead. Rewire cannot merge it: there is no merge, approve or
    auto-merge capability anywhere in the tool, so the change waits for a person.

    Rewire will not write or publish a patch the sandbox did not confirm, and
    will not write into a working tree with uncommitted changes — into a clean
    checkout, `git diff` shows exactly what it did and `git checkout` undoes it.
    """
    settings = get_settings()

    # Every reason publishing could fail is knowable in milliseconds. Finding
    # out after an agent run and two container runs would cost real money to
    # learn something that was free.
    if pull_request and (refusal := check_publishable(repo)):
        error_console.print(f"[red]cannot open a pull request[/red]: {escape(refusal)}")
        raise typer.Exit(code=EXIT_FAILURE)

    outcome = run_migration(
        MigrationRequest(
            task=MigrationTask(
                repository=repo,
                old_spec=old_spec,
                new_spec=new_spec,
                packages=tuple(package or ()),
            ),
            policy=MigrationPolicy(
                apply=apply_patch,
                allow_dirty=allow_dirty,
                max_attempts=max_attempts,
            ),
        ),
        provider=build_provider(settings.llm),
        settings=settings,
    )
    _write_diff(outcome.patch, write_patch_to)
    _render_outcome(outcome, show_diff=show_diff)

    if pull_request:
        published = publish(
            outcome,
            PublishRequest(
                repository=repo,
                prefix=branch_prefix,
                draft=draft,
                base=base,
                dry_run=dry_run,
            ),
        )
        _render_publish(published)
        if not published.status.is_success:
            raise typer.Exit(code=EXIT_FAILURE)

    if not outcome.status.is_success:
        raise typer.Exit(code=EXIT_FAILURE)


def _render_publish(outcome: PublishOutcome) -> None:
    """Print what publishing did, or the reason it refused."""
    if outcome.status is PublishStatus.REFUSED:
        console.print(f"\n[red]NOT PUBLISHED[/red]  {escape(outcome.refusal)}")
        return
    if outcome.status is PublishStatus.NOTHING_TO_PUBLISH:
        console.print(f"\n[yellow]NOTHING TO PUBLISH[/yellow]  {escape(outcome.refusal)}")
        return

    if outcome.commit is not None:
        console.print(
            f"\n[green]COMMITTED[/green]  {escape(outcome.commit.sha[:12])} on "
            f"[bold]{escape(outcome.branch)}[/bold]"
            f" ({len(outcome.commit.files)} file(s), and nothing else)"
        )

    if outcome.status is PublishStatus.DRY_RUN:
        console.print(
            "[yellow]DRY RUN[/yellow]  nothing was pushed and no pull request was opened."
        )
        console.print(f"\n[bold]Pull request title[/bold]\n{escape(outcome.title)}")
        console.print(f"\n[bold]Pull request body[/bold]\n{escape(outcome.body)}")
        return

    if outcome.pull_request is not None:
        console.print(f"[green]PULL REQUEST[/green]  {escape(outcome.pull_request.describe())}")
    console.print(
        "[dim]Rewire has no merge, approve or auto-merge capability. This stays open "
        "until a person acts on it.[/dim]"
    )


# ------------------------------------------------------------------- watch ---

watch_app = typer.Typer(
    help="Follow an upstream specification and act when it moves.", no_args_is_help=True
)
app.add_typer(watch_app, name="watch")


class _Migrator:
    """Runs migrations for a watch, building the provider only if one is needed.

    A ``report`` watch never calls a model, and must not require a credential to
    exist before it can tell you your API changed. Constructing the provider
    eagerly would make the cheapest, safest mode the one with the most
    prerequisites.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._provider: LLMProvider | None = None

    def __call__(self, request: MigrationRequest) -> MigrationOutcome:
        """Run one migration, building the provider on first use."""
        if self._provider is None:
            self._provider = build_provider(self._settings.llm)
        return run_migration(request, provider=self._provider, settings=self._settings)


def _store(settings: Settings) -> WatchStore:
    return WatchStore(settings.watch_dir)


def _watch_policy(
    settings: Settings, *, retry: bool, dry_run: bool, allow_http: bool, branch_prefix: str
) -> CheckPolicy:
    return CheckPolicy(
        timeout_seconds=settings.watch.timeout_seconds,
        max_bytes=settings.watch.max_spec_bytes,
        allow_http=allow_http or settings.watch.allow_http,
        retry=retry,
        dry_run=dry_run,
        branch_prefix=branch_prefix,
    )


_WATCH_STATUS_MARKUP: dict[WatchCheckStatus, str] = {
    WatchCheckStatus.ADOPTED: "[cyan]ADOPTED[/cyan]",
    WatchCheckStatus.UNCHANGED: "[dim]UNCHANGED[/dim]",
    WatchCheckStatus.REFORMATTED: "[dim]REFORMATTED[/dim]",
    WatchCheckStatus.NO_BREAKING_CHANGES: "[green]NO BREAKING CHANGES[/green]",
    WatchCheckStatus.CHANGES_FOUND: "[yellow]CHANGES FOUND[/yellow]",
    WatchCheckStatus.ALREADY_ACTED: "[dim]ALREADY ACTED[/dim]",
    WatchCheckStatus.MIGRATED: "[cyan]MIGRATED[/cyan]",
    WatchCheckStatus.SKIPPED: "[dim]SKIPPED[/dim]",
    WatchCheckStatus.FAILED: "[red]FAILED[/red]",
}


def _render_check(outcome: WatchOutcome, *, show_diff: bool) -> None:
    """Print what one check concluded, and the evidence behind it."""
    marker = _WATCH_STATUS_MARKUP[outcome.status]
    version = f" [dim]({escape(outcome.version)})[/dim]" if outcome.version else ""
    console.print(
        f"{marker}  [bold]{escape(outcome.watch.name)}[/bold]{version} "
        f"{escape(outcome.summary_line())}"
    )

    if outcome.changes is not None and outcome.breaking:
        _render_change_report(
            outcome.changes, outcome.changes.filter(Severity.POTENTIALLY_BREAKING)
        )
    if outcome.migration is not None:
        _render_outcome(outcome.migration, show_diff=show_diff)
    if outcome.publication is not None:
        _render_publish(outcome.publication)
    if outcome.baseline_advanced:
        console.print("[dim]The baseline advanced: this repository now targets that version.[/dim]")


@watch_app.command("add")
def watch_add(
    name: Annotated[str, typer.Argument(help="Name for the watch. Becomes a directory name.")],
    source: Annotated[
        str, typer.Option("--source", help="URL or path of the specification to follow.")
    ],
    repo: Annotated[
        Path,
        typer.Option("--repo", exists=True, file_okay=False, help="Repository the API is used by."),
    ],
    action: Annotated[
        WatchAction,
        typer.Option("--action", help="How far this watch may go on a breaking change."),
    ] = WatchAction.REPORT,
    package: Annotated[
        list[str] | None, typer.Option("--package", help="Package the API belongs to.")
    ] = None,
    base: Annotated[
        str, typer.Option("--base", help="Branch to propose into. Defaults to the repo's own.")
    ] = "",
    draft: Annotated[
        bool, typer.Option("--draft", help="Open any pull request as a draft.")
    ] = False,
    max_attempts: Annotated[
        int, typer.Option("--max-attempts", min=1, max=10, help="Repair attempts allowed.")
    ] = 3,
) -> None:
    """Declare a specification to follow, and what to do when it changes.

    Adding a watch does nothing on its own. The first `rewire watch check`
    adopts whatever is upstream as the baseline — the version this repository is
    taken to target — and reports only what happens after that.

    The default action is `report`: no model is called and nothing is written.
    `migrate` and `pull_request` spend money, and are opted into here rather than
    at check time so that an unattended schedule cannot start doing it by accident.
    """
    settings = get_settings()
    store = _store(settings)
    watch = Watch(
        name=validate_name(name),
        source=source,
        repository=repo,
        packages=tuple(package or ()),
        action=action,
        base=base,
        draft=draft,
        max_attempts=max_attempts,
    )
    store.save(watch)
    console.print(
        f"[green]watching[/green] [bold]{escape(watch.name)}[/bold] "
        f"{escape(watch.source)} [dim]->[/dim] {escape(str(watch.repository))} "
        f"[dim](action: {watch.action.value})[/dim]"
    )
    console.print(
        "[dim]Nothing has been fetched yet. Run 'rewire watch check' to adopt a baseline.[/dim]"
    )
    if watch.action.calls_a_model:
        console.print(
            "[yellow]This watch spends money.[/yellow] Every check that finds a breaking "
            "change will call a model. Each version is acted on once, so repeating the "
            "schedule does not repeat the cost."
        )


@watch_app.command("list")
def watch_list() -> None:
    """List every declared watch and what the last check of it concluded."""
    store = _store(get_settings())
    watches = store.load_all()
    if not watches:
        console.print("[dim]No watches are declared. Add one with 'rewire watch add'.[/dim]")
        return

    table = Table(title="Watches", title_justify="left", title_style="bold cyan")
    table.add_column("Name", style="bold", no_wrap=True)
    table.add_column("Action", no_wrap=True)
    table.add_column("Source", overflow="fold")
    table.add_column("Baseline", no_wrap=True)
    table.add_column("Last check", no_wrap=True)

    for watch in watches:
        state = store.read_state(watch.name)
        name = watch.name if watch.enabled else f"{watch.name} (disabled)"
        table.add_row(
            escape(name),
            watch.action.value,
            escape(watch.source),
            escape(state.version or ("-" if not state.has_baseline else "(unversioned)")),
            escape(f"{state.last_status or '-'} {state.last_checked}".strip()),
        )
    console.print(table)


@watch_app.command("show")
def watch_show(
    name: Annotated[str, typer.Argument(help="Watch to describe.")],
) -> None:
    """Print one watch's declaration and everything remembered about it."""
    store = _store(get_settings())
    watch = store.get(name)
    state = store.read_state(name)
    console.print_json(json.dumps({"watch": watch.to_dict(), "state": state.to_dict()}, indent=2))


@watch_app.command("remove")
def watch_remove(
    name: Annotated[str, typer.Argument(help="Watch to remove.")],
    forget: Annotated[
        bool,
        typer.Option("--forget", help="Also delete its baseline and history."),
    ] = False,
) -> None:
    """Stop following a specification.

    The baseline is kept unless --forget is given. Discarding it means the next
    check of a re-added watch adopts whatever is upstream *then* as the truth,
    silently swallowing any change that happened in between.
    """
    store = _store(get_settings())
    store.remove(name, forget_state=forget)
    console.print(f"[green]removed[/green] {escape(name)}")


@watch_app.command("accept")
def watch_accept(
    name: Annotated[str, typer.Argument(help="Watch whose baseline should advance.")],
) -> None:
    """Record that this repository now targets the version last reported.

    The manual half of the baseline rule. A check will not advance the baseline
    over a breaking change on its own, not even one it migrated and opened a
    pull request for, because an unmerged pull request is a proposal rather than
    a fact about the code. This is how a person says the code has caught up.
    """
    store = _store(get_settings())
    watch = store.get(name)
    version = accept_watch(watch, store=store)
    console.print(
        f"[green]accepted[/green] [bold]{escape(name)}[/bold] now targets "
        f"{escape(version or 'the specification last seen')}"
    )


@watch_app.command("check")
def watch_check(
    name: Annotated[str, typer.Argument(help="Watch to check. All of them when omitted.")] = "",
    retry: Annotated[
        bool,
        typer.Option("--retry", help="Act again on a version that was already acted on."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="For pull_request watches: branch and commit, never push."),
    ] = False,
    allow_http: Annotated[
        bool, typer.Option("--allow-http", help="Permit plain HTTP sources and redirects.")
    ] = False,
    branch_prefix: Annotated[
        str, typer.Option("--branch-prefix", help="Leading segment of any branch created.")
    ] = "rewire",
    show_diff: Annotated[
        bool, typer.Option("--diff/--no-diff", help="Print the diff of any patch produced.")
    ] = False,
    interval: Annotated[
        int,
        typer.Option("--interval", min=0, help="Repeat every N seconds instead of exiting."),
    ] = 0,
    passes: Annotated[
        int,
        typer.Option("--passes", min=0, help="With --interval: stop after N passes. 0 is forever."),
    ] = 0,
) -> None:
    """Check the watches once, and act on anything they are configured to act on.

    One pass, then exit, with the answer in the exit code: 0 nothing needs
    anyone, 1 a check could not complete, 2 something is waiting for a person.
    That is the shape cron, systemd and CI already know how to schedule and
    alert on, which is why Rewire has no daemon.

    --interval turns it into a foreground polling loop for a demonstration or a
    container. It is a convenience, not a supervisor: it will not survive a
    reboot, and it does not know what it missed while it was not running.
    """
    settings = get_settings()
    settings.ensure_data_dirs()
    store = _store(settings)
    watches = (store.get(name),) if name else store.load_all()
    if not watches:
        console.print("[dim]No watches are declared. Add one with 'rewire watch add'.[/dim]")
        return

    policy = _watch_policy(
        settings,
        retry=retry,
        dry_run=dry_run,
        allow_http=allow_http,
        branch_prefix=branch_prefix,
    )
    migrator = _Migrator(settings)

    code = 0
    completed = 0
    while True:
        outcomes = check_all(watches, store=store, policy=policy, migrate=migrator, publish=publish)
        for outcome in outcomes:
            _render_check(outcome, show_diff=show_diff)
        code = exit_code_for(outcomes)
        completed += 1

        if interval <= 0 or (passes and completed >= passes):
            break
        try:
            time.sleep(interval)
        except KeyboardInterrupt:  # pragma: no cover - requires a signal
            break

    if code:
        raise typer.Exit(code=code)


def main() -> None:
    """Console-script entry point with domain-error handling."""
    try:
        app()
    except RewireError as exc:
        # Escape the rendered error: repository paths and API field names can
        # contain square brackets, which Rich would otherwise eat as markup.
        error_console.print(f"[red]error[/red] {escape(exc.code)}: {escape(str(exc))}")
        raise SystemExit(EXIT_FAILURE) from exc


if __name__ == "__main__":  # pragma: no cover - module executed as a script
    main()
