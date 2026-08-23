"""Tests for joining API changes to the code they affect."""

from __future__ import annotations

from pathlib import Path

from rewire.analyzers import build_index
from rewire.changes import diff_specs, parse_spec_text
from rewire.impact import analyse_impact, attach_snippets
from rewire.impact.models import ImpactReport, MatchStrategy, SignalKind

SPEC_HEAD = 'openapi: "3.0.3"\ninfo: {{title: {title}, version: "{version}"}}\n'


def spec(
    *,
    title: str = "OpenAI API",
    version: str = "1",
    body: str = "",
    usage_required: str = "[completion_tokens]",
) -> str:
    return (
        SPEC_HEAD.format(title=title, version=version)
        + f"""paths:
  /v1/chat/completions:
    post:
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
{body}
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: object
                properties:
                  usage:
                    type: object
                    required: {usage_required}
                    properties:
                      completion_tokens: {{type: integer}}
"""
    )


OLD_SPEC = spec(body="                max_tokens: {type: integer}\n")
NEW_SPEC = spec(
    version="2",
    body="                max_completion_tokens: {type: integer}\n",
    # completion_tokens leaves `required`, so the client may now read a field
    # the server no longer promises to send.
    usage_required="[]",
)


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def run(tmp_path: Path, files: dict[str, str], **kwargs: object) -> ImpactReport:
    root = make_repo(tmp_path, files)
    changes = diff_specs(parse_spec_text(OLD_SPEC), parse_spec_text(NEW_SPEC))
    return analyse_impact(changes, build_index(root), **kwargs)  # type: ignore[arg-type]


CLIENT = """
from openai import OpenAI

client = OpenAI()


def ask(prompt):
    return client.chat.completions.create(
        model="m",
        max_tokens=100,
    )
"""

PYPROJECT = '[project]\nname = "app"\nversion = "0.1"\ndependencies = ["openai"]\n'
HTTPX_PROJECT = '[project]\nname = "x"\nversion = "1"\ndependencies = ["httpx"]\n'


def locations_for(report: ImpactReport, field: str) -> list[tuple[str, int]]:
    return [
        (location.file, location.line)
        for impact in report.impacts
        if impact.change.field == field
        for location in impact.locations
    ]


# ------------------------------------------------------------------- basics ---


def test_finds_the_call_site(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    assert ("app.py", 10) in locations_for(report, "max_tokens")


def test_confidence_is_high_at_a_resolved_call_site(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    best = max(
        (loc for impact in report.impacts for loc in impact.locations),
        key=lambda location: location.confidence,
    )
    assert best.confidence > 0.95
    assert SignalKind.SDK_CALL_TARGET in {signal.kind for signal in best.signals}


def test_analysis_never_modifies_the_repository(tmp_path: Path) -> None:
    """Phase 3 is analysis only; a write here would be a contract violation."""
    files = {"pyproject.toml": PYPROJECT, "app.py": CLIENT}
    root = make_repo(tmp_path, files)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    changes = diff_specs(parse_spec_text(OLD_SPEC), parse_spec_text(NEW_SPEC))
    analyse_impact(changes, build_index(root))
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_results_are_deterministic(tmp_path: Path) -> None:
    files = {"pyproject.toml": PYPROJECT, "app.py": CLIENT}
    root = make_repo(tmp_path, files)
    changes = diff_specs(parse_spec_text(OLD_SPEC), parse_spec_text(NEW_SPEC))
    index = build_index(root)
    first = analyse_impact(changes, index)
    second = analyse_impact(changes, index)
    assert first.model_dump_json(exclude={"summary"}) == second.model_dump_json(exclude={"summary"})


def test_report_round_trips_through_json(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    assert ImpactReport.model_validate_json(report.model_dump_json()) == report


def test_empty_repository_yields_no_locations(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT})
    assert report.summary.locations == 0
    # Changes are still listed, so "nothing affected" is distinguishable from
    # "never considered".
    assert report.summary.changes_analysed > 0


# -------------------------------------------------------------- precision ----


def test_a_decoy_without_the_import_is_excluded(tmp_path: Path) -> None:
    decoy = "def clamp(requested, max_tokens=100):\n    return min(requested, max_tokens)\n"
    report = run(
        tmp_path,
        {"pyproject.toml": PYPROJECT, "app.py": CLIENT, "budget.py": decoy},
    )
    assert not [file for file, _ in locations_for(report, "max_tokens") if file == "budget.py"]


def test_a_repository_that_never_uses_the_api_reports_nothing(tmp_path: Path) -> None:
    unrelated = '[project]\nname = "x"\nversion = "1"\ndependencies = ["httpx"]\n'
    decoy = "def budget(max_tokens=10):\n    return max_tokens\n"
    report = run(tmp_path, {"pyproject.toml": unrelated, "app.py": decoy}, packages=("openai",))
    assert locations_for(report, "max_tokens") == []


def test_min_confidence_filters_weaker_matches(tmp_path: Path) -> None:
    weak = 'FIELD = "max_tokens"\n'
    files = {"pyproject.toml": PYPROJECT, "app.py": CLIENT, "log.py": weak}
    permissive = run(tmp_path, files, min_confidence=0.0)
    strict = run(tmp_path, files, min_confidence=0.99)
    assert strict.summary.locations < permissive.summary.locations


def test_min_severity_skips_lesser_changes(tmp_path: Path) -> None:
    from rewire.changes import Severity

    files = {"pyproject.toml": PYPROJECT, "app.py": CLIENT}
    everything = run(tmp_path, files)
    breaking = run(tmp_path, files, min_severity=Severity.BREAKING)
    assert breaking.summary.changes_analysed < everything.summary.changes_analysed


# ----------------------------------------------------------------- recall ----


def test_a_test_file_one_hop_from_the_sdk_is_found(tmp_path: Path) -> None:
    """It imports no SDK, so only call-graph proximity can rescue it."""
    test_file = "from app import ask\n\n\ndef test_ask():\n    ask(max_tokens=5)\n"
    report = run(
        tmp_path,
        {"pyproject.toml": PYPROJECT, "app.py": CLIENT, "tests/test_app.py": test_file},
    )
    files = {file for file, _ in locations_for(report, "max_tokens")}
    assert "tests/test_app.py" in files


def test_test_locations_are_marked_but_not_discounted(tmp_path: Path) -> None:
    """A migration that leaves tests on the old API leaves a red build."""
    test_file = "from app import ask\n\n\ndef test_ask():\n    ask(max_tokens=5)\n"
    report = run(
        tmp_path,
        {"pyproject.toml": PYPROJECT, "app.py": CLIENT, "tests/test_app.py": test_file},
    )
    assert report.summary.test_locations >= 1


def test_a_raw_http_caller_is_found_via_the_endpoint_path(tmp_path: Path) -> None:
    raw = """
import httpx


def call(prompt):
    return httpx.post(
        "https://api.openai.com/v1/chat/completions",
        json={"max_tokens": 10},
    )
"""
    report = run(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname="x"\nversion="1"\ndependencies=["httpx"]\n',
            "raw.py": raw,
        },
    )
    assert ("raw.py", 8) in locations_for(report, "max_tokens")


def test_multi_line_calls_report_the_argument_line(tmp_path: Path) -> None:
    """Anchoring to the call would point the agent at the wrong line."""
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    lines = [line for file, line in locations_for(report, "max_tokens") if file == "app.py"]
    assert 10 in lines  # the `max_tokens=100,` line, not line 8 where the call opens


# ---------------------------------------------------------------- direction ---


RESPONSE_READER = """
from openai import OpenAI

client = OpenAI()


def cost(prompt):
    response = client.chat.completions.create(model="m")
    return response.usage.completion_tokens


def fake():
    return {"completion_tokens": 0}
"""


def test_reading_a_response_field_outranks_constructing_it(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": RESPONSE_READER})
    by_line = {
        location.line: location.confidence
        for impact in report.impacts
        if impact.change.field == "completion_tokens"
        for location in impact.locations
    }
    assert by_line.get(9, 0.0) > by_line.get(13, 0.0)


# ------------------------------------------------------------------ report ----


def test_locations_are_ordered_by_confidence(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT}, min_confidence=0.0)
    for impact in report.impacts:
        confidences = [location.confidence for location in impact.locations]
        assert confidences == sorted(confidences, reverse=True)


def test_each_line_is_reported_once(tmp_path: Path) -> None:
    """`max_tokens=max_tokens` yields two references on one line."""
    forwarding = CLIENT.replace("max_tokens=100,", "max_tokens=max_tokens,").replace(
        "def ask(prompt):", "def ask(prompt, max_tokens=1):"
    )
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": forwarding})
    found = locations_for(report, "max_tokens")
    assert len(found) == len(set(found))


def test_signals_explain_every_score(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    for impact in report.impacts:
        for location in impact.locations:
            assert location.signals
            assert all(signal.detail for signal in location.signals)


def test_affected_files_are_deduplicated_and_sorted(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    assert report.affected_files == sorted(set(report.affected_files))


def test_strategy_is_recorded(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    strategies = {location.strategy for impact in report.impacts for location in impact.locations}
    assert MatchStrategy.FIELD_REFERENCE in strategies


def test_snippets_are_attached_separately(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    changes = diff_specs(parse_spec_text(OLD_SPEC), parse_spec_text(NEW_SPEC))
    report = analyse_impact(changes, build_index(root))
    assert all(
        location.snippet is None for impact in report.impacts for location in impact.locations
    )
    with_snippets = attach_snippets(report, root)
    assert any(
        location.snippet for impact in with_snippets.impacts for location in impact.locations
    )


def test_snippets_tolerate_a_vanished_file(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    changes = diff_specs(parse_spec_text(OLD_SPEC), parse_spec_text(NEW_SPEC))
    report = analyse_impact(changes, build_index(root))
    (root / "app.py").unlink()
    assert attach_snippets(report, root) is not None


# ---------------------------------------------------------------- packages ---


def test_packages_are_inferred_from_the_specification_title(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    assert report.impacts[0].packages == ("openai",)


def test_an_explicit_package_overrides_inference(tmp_path: Path) -> None:
    report = run(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT}, packages=("custom",))
    assert report.impacts[0].packages == ("custom",)


def test_no_package_means_no_package_signals(tmp_path: Path) -> None:
    """Absence of evidence must not become evidence against."""
    changes = diff_specs(
        parse_spec_text(spec(title="Some Unknown Service")),
        parse_spec_text(spec(title="Some Unknown Service", version="2", body="")),
    )
    root = make_repo(tmp_path, {"pyproject.toml": PYPROJECT, "app.py": CLIENT})
    report = analyse_impact(changes, build_index(root))
    kinds = {
        signal.kind
        for impact in report.impacts
        for location in impact.locations
        for signal in location.signals
    }
    assert SignalKind.PACKAGE_NOT_IMPORTED not in kinds
    assert SignalKind.PACKAGE_ABSENT not in kinds
