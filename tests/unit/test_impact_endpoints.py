"""Tests for endpoint-level impact and the report's query surface."""

from __future__ import annotations

from pathlib import Path

from rewire.analyzers import build_index
from rewire.changes import Severity, diff_specs, parse_spec_text
from rewire.impact import analyse_impact
from rewire.impact.models import ImpactReport, MatchStrategy, SignalKind
from rewire.impact.scoring import in_test_code_signal

HEAD = 'openapi: "3.0.3"\ninfo: {title: OpenAI API, version: "1"}\n'

WITH_ENDPOINT = (
    HEAD
    + """paths:
  /v1/completions:
    post:
      responses: {'200': {description: OK}}
  /v1/chat/completions:
    post:
      responses: {'200': {description: OK}}
"""
)

WITHOUT_ENDPOINT = (
    HEAD
    + """paths:
  /v1/chat/completions:
    post:
      responses: {'200': {description: OK}}
"""
)

CALLER = """
import httpx

URL = "https://api.openai.com/v1/completions"


def call():
    return httpx.post(URL)
"""

PROJECT = '[project]\nname = "a"\nversion = "1"\ndependencies = ["httpx"]\n'


def report_for(tmp_path: Path, files: dict[str, str]) -> ImpactReport:
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    changes = diff_specs(parse_spec_text(WITH_ENDPOINT), parse_spec_text(WITHOUT_ENDPOINT))
    return analyse_impact(changes, build_index(tmp_path))


def test_removed_endpoint_is_found_by_its_path(tmp_path: Path) -> None:
    """No field name exists to match, so the URL literal is the only handle."""
    report = report_for(tmp_path, {"pyproject.toml": PROJECT, "app.py": CALLER})
    locations = [
        location
        for impact in report.impacts
        if impact.change.severity is Severity.BREAKING
        for location in impact.locations
    ]
    assert [(location.file, location.line) for location in locations] == [("app.py", 4)]
    assert locations[0].strategy is MatchStrategy.ENDPOINT_PATH
    assert SignalKind.ENDPOINT_MENTIONED in {s.kind for s in locations[0].signals}


def test_a_similar_but_different_path_is_not_matched(tmp_path: Path) -> None:
    other = 'URL = "https://api.openai.com/v2/embeddings"\n'
    report = report_for(tmp_path, {"pyproject.toml": PROJECT, "app.py": other})
    assert report.summary.locations == 0


def test_endpoint_locations_in_tests_are_marked(tmp_path: Path) -> None:
    test_file = 'URL = "https://api.openai.com/v1/completions"\n'
    report = report_for(tmp_path, {"pyproject.toml": PROJECT, "tests/test_api.py": test_file})
    assert report.summary.test_locations == 1


# ------------------------------------------------------------ opaque calls ---


def test_star_kwargs_are_flagged_on_the_call_site(tmp_path: Path) -> None:
    """Forwarding **kwargs hides the arguments, so the site needs review."""
    old = (
        HEAD
        + """paths:
  /v1/chat/completions:
    post:
      requestBody:
        content:
          application/json:
            schema: {type: object, properties: {max_tokens: {type: integer}}}
      responses: {'200': {description: OK}}
"""
    )
    new = old.replace("max_tokens", "max_completion_tokens")
    source = """
from openai import OpenAI

client = OpenAI()


def go(payload):
    return client.chat.completions.create(max_tokens=1, **payload)
"""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=["openai"]\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    report = analyse_impact(
        diff_specs(parse_spec_text(old), parse_spec_text(new)), build_index(tmp_path)
    )
    kinds = {
        signal.kind
        for impact in report.impacts
        for location in impact.locations
        for signal in location.signals
    }
    assert SignalKind.OPAQUE_ARGUMENTS in kinds


# ----------------------------------------------------------- report surface --


def test_logit_sums_the_signal_weights(tmp_path: Path) -> None:
    report = report_for(tmp_path, {"pyproject.toml": PROJECT, "app.py": CALLER})
    location = next(loc for impact in report.impacts for loc in impact.locations)
    assert location.logit == sum(signal.weight for signal in location.signals)


def test_change_impact_lists_files_in_confidence_order(tmp_path: Path) -> None:
    report = report_for(tmp_path, {"pyproject.toml": PROJECT, "app.py": CALLER})
    impact = next(impact for impact in report.impacts if impact.locations)
    assert impact.files == ["app.py"]
    assert impact.best_confidence > 0


def test_change_impact_with_no_locations_has_zero_confidence(tmp_path: Path) -> None:
    report = report_for(tmp_path, {"pyproject.toml": PROJECT})
    assert all(impact.best_confidence == 0.0 for impact in report.impacts)
    assert all(impact.files == [] for impact in report.impacts)


def test_breaking_impacts_filters_by_severity(tmp_path: Path) -> None:
    report = report_for(tmp_path, {"pyproject.toml": PROJECT, "app.py": CALLER})
    breaking = report.breaking_impacts()
    assert breaking
    assert all(impact.change.severity is Severity.BREAKING for impact in breaking)
    assert all(impact.locations for impact in breaking)


def test_locations_in_selects_one_file(tmp_path: Path) -> None:
    report = report_for(tmp_path, {"pyproject.toml": PROJECT, "app.py": CALLER})
    assert [loc.file for loc in report.locations_in("app.py")] == ["app.py"]
    assert report.locations_in("absent.py") == []


def test_in_test_code_signal_carries_no_weight() -> None:
    """Tests are affected too; marking them must not down-rank them."""
    assert in_test_code_signal("tests/test_a.py").weight == 0.0


def test_low_confidence_locations_render_in_green() -> None:
    """The three confidence bands must all be reachable."""
    from rewire.cli import _confidence_markup

    assert "green" in _confidence_markup(0.4)
    assert "yellow" in _confidence_markup(0.7)
    assert "red" in _confidence_markup(0.95)


def test_endpoint_location_inside_an_sdk_using_symbol(tmp_path: Path) -> None:
    """The URL literal sits in a function that also calls the SDK."""
    caller = """
from openai import OpenAI

client = OpenAI()


def call():
    url = "https://api.openai.com/v1/completions"
    return client.chat.completions.create(model=url)
"""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=["openai"]\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(caller, encoding="utf-8")
    changes = diff_specs(parse_spec_text(WITH_ENDPOINT), parse_spec_text(WITHOUT_ENDPOINT))
    report = analyse_impact(changes, build_index(tmp_path))
    kinds = {
        signal.kind
        for impact in report.impacts
        for location in impact.locations
        for signal in location.signals
    }
    assert SignalKind.ENCLOSING_SDK_USAGE in kinds


def test_an_undeclared_but_imported_package_is_not_argued_against(tmp_path: Path) -> None:
    """Vendored or implicitly available packages must not be treated as absent."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=[]\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        'import openai\n\nURL = "https://api.openai.com/v1/completions"\n', encoding="utf-8"
    )
    changes = diff_specs(parse_spec_text(WITH_ENDPOINT), parse_spec_text(WITHOUT_ENDPOINT))
    report = analyse_impact(changes, build_index(tmp_path), packages=("openai",))
    kinds = {
        signal.kind
        for impact in report.impacts
        for location in impact.locations
        for signal in location.signals
    }
    assert SignalKind.PACKAGE_ABSENT not in kinds
