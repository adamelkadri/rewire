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
from rich.table import Table

from rewire.__version__ import __version__
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
