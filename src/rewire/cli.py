"""Command-line entry point for Rewire.

Only commands that are genuinely implemented are exposed. Commands for later
phases are deliberately absent rather than stubbed, so that ``rewire --help``
never advertises behaviour that does not exist.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, assert_never

import typer
from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table

from rewire.__version__ import __version__
from rewire.agents import AgentBudget, MigrationAgent, MigrationResult, Workspace
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
from rewire.core.errors import RewireError
from rewire.core.logging import configure_from_settings
from rewire.evals import render_markdown, run_evaluation, write_results
from rewire.impact import (
    DEFAULT_MIN_CONFIDENCE,
    ImpactReport,
    analyse_impact,
    attach_snippets,
)
from rewire.llm import build_provider
from rewire.sandbox import (
    CheckResult,
    Verdict,
    VerificationReport,
    VerificationRequest,
    verify,
)
from rewire.sandbox import CheckStatus as SandboxCheckStatus

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
    result = agent.run(workspace=Workspace.open(repo), index=index, changes=changes, impact=impact)

    if write_patch_to is not None and not result.patch.is_empty:
        write_patch_to.write_text(result.patch.unified_diff(), encoding="utf-8")
        console.print(f"[dim]wrote diff to {escape(str(write_patch_to))}[/dim]")

    _render_migration(result, show_diff=show_diff, verified=run_verification)
    console.print(f"[dim]trace: {escape(str(settings.runs_dir / result.summary.run_id))}[/dim]")

    if not result.summary.produced_patch:
        raise typer.Exit(code=EXIT_FAILURE)

    if not run_verification:
        return

    report = verify(
        repo, result.patch, request=_verification_request(settings, image=image, timeout=None)
    )
    _render_verification(report)
    if not report.verified:
        raise typer.Exit(code=EXIT_FAILURE)


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
