"""Tests for `rewire watch`, the commands that make Rewire notice on its own.

The monitor's decisions are tested in ``test_watch_monitor``. What is covered
here is the layer a person and a cron job actually touch: the exit codes, what
reaches the terminal, and the two things that must be true of an unattended
schedule — that a reporting watch needs no credential, and that ``--interval``
stops when it was told to.

Every source is a local file. A watch over a URL would be a test of the network.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rewire import cli
from rewire.cli import app
from rewire.core.config import get_settings
from rewire.core.errors import ConfigurationError
from rewire.services.migrate import MigrationOutcome, MigrationStatus
from rewire.services.publish import PublishOutcome, PublishStatus
from rewire.watch import monitor
from rewire.watch.store import WatchStore

runner = CliRunner()


def spec(version: str = "1.0.0", *, field: str = "customer_name") -> str:
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Orders API", "version": version},
        "paths": {
            "/orders": {
                "get": {
                    "operationId": "listOrders",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {field: {"type": "string"}},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    return yaml.safe_dump(document, sort_keys=True)


def flat(text: str) -> str:
    """Collapse Rich's line wrapping, so a prose assertion is not a layout assertion."""
    return " ".join(text.split())


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("REWIRE_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("REWIRE_DATA_DIR", str(tmp_path / ".rewire"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A specification file and a repository directory to watch it for."""
    (tmp_path / "repo").mkdir()
    (tmp_path / "openapi.yaml").write_text(spec(), encoding="utf-8")
    return tmp_path


def add(project: Path, *extra: str) -> None:
    result = runner.invoke(
        app,
        [
            "watch",
            "add",
            "orders",
            "--source",
            str(project / "openapi.yaml"),
            "--repo",
            str(project / "repo"),
            *extra,
        ],
    )
    assert result.exit_code == 0, result.output


def store(project: Path) -> WatchStore:
    return WatchStore(project / ".rewire" / "watch")


# ------------------------------------------------------------------ declare ---


def test_adding_a_watch_says_it_has_not_fetched_anything_yet(project: Path) -> None:
    """A watch that appeared to be armed the moment it was created would mislead."""
    result = runner.invoke(
        app,
        [
            "watch",
            "add",
            "orders",
            "--source",
            str(project / "openapi.yaml"),
            "--repo",
            str(project / "repo"),
        ],
    )
    assert result.exit_code == 0
    assert "Nothing has been fetched yet" in flat(result.output)
    assert store(project).get("orders").action.value == "report"


def test_a_name_that_is_not_a_safe_directory_is_refused(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "watch",
            "add",
            "../escape",
            "--source",
            str(project / "openapi.yaml"),
            "--repo",
            str(project / "repo"),
        ],
    )
    assert result.exit_code != 0


def test_listing_says_so_when_there_is_nothing_to_list(project: Path) -> None:
    result = runner.invoke(app, ["watch", "list"])
    assert result.exit_code == 0
    assert "No watches are declared" in flat(result.output)


def test_listing_shows_the_baseline_and_the_last_check(project: Path) -> None:
    add(project)
    runner.invoke(app, ["watch", "check"])
    output = flat(runner.invoke(app, ["watch", "list"]).output)
    assert "orders" in output
    assert "1.0.0" in output
    assert "adopted" in output


def test_show_prints_the_declaration_and_the_state(project: Path) -> None:
    add(project, "--package", "acme")
    runner.invoke(app, ["watch", "check"])
    result = runner.invoke(app, ["watch", "show", "orders"])
    assert result.exit_code == 0
    assert "acme" in result.output
    assert "semantic_digest" in result.output


def test_removing_keeps_the_baseline_unless_asked(project: Path) -> None:
    add(project)
    runner.invoke(app, ["watch", "check"])
    assert runner.invoke(app, ["watch", "remove", "orders"]).exit_code == 0
    assert store(project).baseline_path("orders").exists()

    add(project)
    assert runner.invoke(app, ["watch", "remove", "orders", "--forget"]).exit_code == 0
    assert not store(project).directory("orders").exists()


# -------------------------------------------------------------------- check ---


def test_checking_with_no_watches_is_not_a_failure(project: Path) -> None:
    result = runner.invoke(app, ["watch", "check"])
    assert result.exit_code == 0
    assert "No watches are declared" in flat(result.output)


def test_the_first_check_adopts_and_exits_zero(project: Path) -> None:
    add(project)
    result = runner.invoke(app, ["watch", "check"])
    assert result.exit_code == 0
    assert "ADOPTED" in flat(result.output)


def test_a_breaking_change_exits_two_and_prints_the_changes(project: Path) -> None:
    """Two rather than one, so cron can tell "waiting for you" from "broken"."""
    add(project)
    runner.invoke(app, ["watch", "check"])
    (project / "openapi.yaml").write_text(spec("2.0.0", field="customer"), encoding="utf-8")

    result = runner.invoke(app, ["watch", "check"])
    assert result.exit_code == 2
    output = flat(result.output)
    assert "CHANGES FOUND" in output
    assert "customer_name" in output


def test_an_unreachable_source_exits_one(project: Path) -> None:
    add(project)
    (project / "openapi.yaml").unlink()
    result = runner.invoke(app, ["watch", "check"])
    assert result.exit_code == 1
    assert "FAILED" in flat(result.output)


def test_a_single_watch_can_be_named(project: Path) -> None:
    add(project)
    (project / "other.yaml").write_text(spec(), encoding="utf-8")
    runner.invoke(
        app,
        [
            "watch",
            "add",
            "other",
            "--source",
            str(project / "other.yaml"),
            "--repo",
            str(project / "repo"),
        ],
    )
    result = runner.invoke(app, ["watch", "check", "orders"])
    assert result.exit_code == 0
    assert "other" not in flat(result.output)
    assert store(project).read_state("other").has_baseline is False


def test_a_reporting_watch_never_needs_a_credential(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheapest, safest mode must not have the most prerequisites."""

    def refuse(_settings: object) -> object:
        raise ConfigurationError("no API key is configured")

    monkeypatch.setattr(cli, "build_provider", refuse)
    add(project)
    runner.invoke(app, ["watch", "check"])
    (project / "openapi.yaml").write_text(spec("2.0.0", field="customer"), encoding="utf-8")

    result = runner.invoke(app, ["watch", "check"])
    assert result.exit_code == 2
    assert "CHANGES FOUND" in flat(result.output)


def test_an_interval_run_stops_after_the_passes_it_was_given(project: Path) -> None:
    """Otherwise there is no way to test the loop, and no way to demonstrate it."""
    add(project)
    result = runner.invoke(app, ["watch", "check", "--interval", "1", "--passes", "2"])
    assert result.exit_code == 0
    assert flat(result.output).count("ADOPTED") + flat(result.output).count("UNCHANGED") == 2


# ------------------------------------------------------------------- accept ---


def test_accepting_advances_the_baseline_and_the_finding_stops(project: Path) -> None:
    add(project)
    runner.invoke(app, ["watch", "check"])
    (project / "openapi.yaml").write_text(spec("2.0.0", field="customer"), encoding="utf-8")
    assert runner.invoke(app, ["watch", "check"]).exit_code == 2

    accepted = runner.invoke(app, ["watch", "accept", "orders"])
    assert accepted.exit_code == 0
    assert "2.0.0" in flat(accepted.output)

    after = runner.invoke(app, ["watch", "check"])
    assert after.exit_code == 0
    assert "UNCHANGED" in flat(after.output)


def test_accepting_with_nothing_newer_seen_is_a_readable_error(project: Path) -> None:
    add(project)
    runner.invoke(app, ["watch", "check"])
    result = runner.invoke(app, ["watch", "accept", "orders"])
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit | Exception)


def test_accepting_an_unknown_watch_is_a_readable_error(project: Path) -> None:
    result = runner.invoke(app, ["watch", "accept", "absent"])
    assert result.exit_code != 0


# ------------------------------------------------------------------- acting ---


def test_adding_an_acting_watch_says_it_will_spend_money(project: Path) -> None:
    """The escalation is opted into here, so the warning belongs here too."""
    result = runner.invoke(
        app,
        [
            "watch",
            "add",
            "orders",
            "--source",
            str(project / "openapi.yaml"),
            "--repo",
            str(project / "repo"),
            "--action",
            "migrate",
        ],
    )
    assert result.exit_code == 0
    assert "This watch spends money" in flat(result.output)


def test_an_acting_watch_runs_the_pipeline_and_prints_both_results(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider is built only now, on the first check that actually acts."""
    built: list[object] = []
    monkeypatch.setattr(cli, "build_provider", lambda settings: built.append(settings) or "model")
    monkeypatch.setattr(monitor, "check_publishable", lambda _root: "")
    monkeypatch.setattr(
        cli,
        "run_migration",
        lambda request, *, provider, settings: MigrationOutcome(
            run_id="run-1", status=MigrationStatus.VERIFIED
        ),
    )
    monkeypatch.setattr(
        cli,
        "publish",
        lambda outcome, request: PublishOutcome(
            status=PublishStatus.DRY_RUN, branch="rewire/x", title="Migrate", body="why"
        ),
    )

    add(project, "--action", "pull_request", "--base", "main")
    runner.invoke(app, ["watch", "check"])
    (project / "openapi.yaml").write_text(spec("2.0.0", field="customer"), encoding="utf-8")

    result = runner.invoke(app, ["watch", "check", "--dry-run"])
    assert result.exit_code == 2
    output = flat(result.output)
    assert "MIGRATED" in output
    assert "DRY RUN" in output
    assert len(built) == 1
