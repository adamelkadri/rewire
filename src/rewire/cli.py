"""Command-line entry point for Rewire.

Phase 0 exposes only the commands that are genuinely implemented: ``version``
and ``doctor``. Commands for later phases are deliberately absent rather than
stubbed, so that ``rewire --help`` never advertises behaviour that does not
exist.
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
from rewire.changes import ApiChange, ChangeReport, Severity, diff_specs, load_spec
from rewire.core.config import LogLevel, Settings, get_settings
from rewire.core.doctor import CheckStatus, DoctorReport, run_checks
from rewire.core.errors import RewireError
from rewire.core.logging import configure_from_settings

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
