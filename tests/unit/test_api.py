"""Tests for the HTTP surface.

Two of these matter more than the rest: that the application refuses to exist
without a token and an allowlist, and that no request body can talk the server
into touching a path outside one. Everything else is plumbing.

No worker runs here. The API's contract is that it queues and reports, so what
is asserted is that a submission produces a queued job and returns — not that a
migration happened.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from rewire.api import create_app, resolve_within
from rewire.core.config import ApiSettings, Settings
from rewire.core.errors import ConfigurationError
from rewire.jobs import JobState, JobStore
from rewire.services.migrate import MigrationOutcome, MigrationStatus
from rewire.services.record import write_record

TOKEN = "test-token"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An allowed root with a repository and two specs inside it."""
    root = tmp_path / "allowed"
    (root / "repo").mkdir(parents=True)
    (root / "old.yaml").write_text("openapi: 3.0.3\n", encoding="utf-8")
    (root / "new.yaml").write_text("openapi: 3.0.3\n", encoding="utf-8")
    return root


@pytest.fixture
def settings(tmp_path: Path, workspace: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / ".rewire",
        api=ApiSettings(token=TOKEN, allowed_roots=(workspace,)),  # type: ignore[arg-type]
    )


@pytest.fixture
def queue(settings: Settings) -> JobStore:
    settings.ensure_data_dirs()
    return JobStore(settings.jobs_path)


@pytest.fixture
def client(settings: Settings, queue: JobStore) -> Iterator[TestClient]:
    with TestClient(create_app(settings, store=queue)) as test_client:
        test_client.headers["authorization"] = f"Bearer {TOKEN}"
        yield test_client


def body(workspace: Path, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "repository": str(workspace / "repo"),
        "old_spec": str(workspace / "old.yaml"),
        "new_spec": str(workspace / "new.yaml"),
    }
    return {**payload, **overrides}


# ------------------------------------------------------------ fails closed ---


def test_the_api_refuses_to_exist_without_a_token(tmp_path: Path, workspace: Path) -> None:
    """An API that starts without credentials is one somebody forgot to configure."""
    settings = Settings(data_dir=tmp_path / ".rewire", api=ApiSettings(allowed_roots=(workspace,)))
    with pytest.raises(ConfigurationError, match="without a token"):
        create_app(settings)


def test_the_api_refuses_to_exist_without_an_allowlist(tmp_path: Path) -> None:
    """Otherwise a request body chooses which directory runs in a container."""
    settings = Settings(data_dir=tmp_path / ".rewire", api=ApiSettings(token=TOKEN))  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="repository roots"):
        create_app(settings)


# -------------------------------------------------------------------- auth ---


def test_liveness_needs_no_credential(client: TestClient) -> None:
    del client.headers["authorization"]
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic abc", ""])
def test_everything_else_needs_the_token(client: TestClient, header: str | None) -> None:
    del client.headers["authorization"]
    headers = {"authorization": header} if header is not None else {}
    response = client.get("/v1/migrations", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_the_right_token_is_accepted(client: TestClient) -> None:
    assert client.get("/v1/migrations").status_code == 200


# --------------------------------------------------------------- allowlist ---


def test_a_path_outside_every_root_is_refused(client: TestClient, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    response = client.post("/v1/migrations", json=body(outside))
    assert response.status_code == 403
    assert "allowed repository root" in response.json()["detail"]


def test_a_traversing_path_is_judged_by_where_it_leads(client: TestClient, workspace: Path) -> None:
    """`..` is resolved before the comparison, not matched as text."""
    escape = workspace / "repo" / ".." / ".." / "elsewhere"
    response = client.post("/v1/migrations", json=body(workspace, repository=str(escape)))
    assert response.status_code == 403


def test_a_symlink_out_of_a_root_is_refused(
    client: TestClient, workspace: Path, tmp_path: Path
) -> None:
    """The check follows the link, because the container would too."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = workspace / "sneaky"
    link.symlink_to(target)
    response = client.post("/v1/migrations", json=body(workspace, repository=str(link)))
    assert response.status_code == 403


def test_the_root_itself_is_inside_itself(workspace: Path) -> None:
    assert resolve_within(workspace, (workspace,)) == workspace.resolve()


def test_resolve_within_refuses_when_there_are_no_roots(workspace: Path) -> None:
    with pytest.raises(HTTPException) as caught:
        resolve_within(workspace, ())
    assert caught.value.status_code == 403


def test_a_path_that_does_not_exist_is_a_bad_request(client: TestClient, workspace: Path) -> None:
    response = client.post(
        "/v1/migrations", json=body(workspace, old_spec=str(workspace / "absent.yaml"))
    )
    assert response.status_code == 400
    assert "no such path" in response.json()["detail"]


# ------------------------------------------------------------------ submit ---


def test_a_submission_is_accepted_and_queued_not_run(
    client: TestClient, workspace: Path, queue: JobStore
) -> None:
    """202 rather than 200: the work has not happened yet."""
    response = client.post("/v1/migrations", json=body(workspace))
    assert response.status_code == 202

    payload = response.json()
    assert payload["state"] == JobState.QUEUED.value
    assert payload["record"] is None
    assert queue.get(payload["id"]).state is JobState.QUEUED


def test_a_body_cannot_grant_itself_permission_to_write(
    client: TestClient, workspace: Path
) -> None:
    """The submission model has no such field, and forbids unknown ones."""
    response = client.post("/v1/migrations", json=body(workspace, apply=True))
    assert response.status_code == 422


def test_packages_reach_the_job(client: TestClient, workspace: Path, queue: JobStore) -> None:
    response = client.post("/v1/migrations", json=body(workspace, packages=["acme"]))
    job = queue.get(response.json()["id"])
    assert job.payload["packages"] == ["acme"]


def test_a_body_missing_a_field_is_rejected_before_anything_happens(
    client: TestClient, queue: JobStore
) -> None:
    assert client.post("/v1/migrations", json={"repository": "/x"}).status_code == 422
    assert queue.list_jobs() == ()


# ------------------------------------------------------------------ report ---


def test_a_job_can_be_read_back(client: TestClient, workspace: Path) -> None:
    job_id = client.post("/v1/migrations", json=body(workspace)).json()["id"]
    response = client.get(f"/v1/migrations/{job_id}")
    assert response.status_code == 200
    assert response.json()["id"] == job_id


def test_a_finished_job_carries_its_run_record(
    client: TestClient, workspace: Path, queue: JobStore, settings: Settings
) -> None:
    """The question an HTTP caller actually has, answered from the record."""
    job_id = client.post("/v1/migrations", json=body(workspace)).json()["id"]
    assert queue.claim("w") is not None
    write_record(
        settings.runs_dir,
        MigrationOutcome(run_id="run-xyz", status=MigrationStatus.VERIFIED),
    )
    queue.succeed(job_id, run_id="run-xyz", worker="w")

    record = client.get(f"/v1/migrations/{job_id}").json()["record"]
    assert record["run_id"] == "run-xyz"
    assert record["status"] == "verified"
    assert record["version"] == 1


def test_a_job_whose_record_is_missing_is_still_reported(
    client: TestClient, workspace: Path, queue: JobStore
) -> None:
    """Failing the request over a missing artefact would lose the job too."""
    job_id = client.post("/v1/migrations", json=body(workspace)).json()["id"]
    assert queue.claim("w") is not None
    queue.succeed(job_id, run_id="never-written", worker="w")

    payload = client.get(f"/v1/migrations/{job_id}").json()
    assert payload["state"] == JobState.SUCCEEDED.value
    assert payload["record"] is None


def test_an_unknown_job_is_a_404(client: TestClient) -> None:
    assert client.get("/v1/migrations/absent").status_code == 404


def test_jobs_are_listed_with_the_queue_totals(client: TestClient, workspace: Path) -> None:
    for _ in range(3):
        client.post("/v1/migrations", json=body(workspace))
    payload = client.get("/v1/migrations").json()
    assert len(payload["jobs"]) == 3
    assert payload["counts"]["queued"] == 3
    assert payload["counts"]["failed"] == 0


def test_listing_can_be_filtered_and_is_bounded(client: TestClient, workspace: Path) -> None:
    client.post("/v1/migrations", json=body(workspace))
    assert client.get("/v1/migrations", params={"state": "running"}).json()["jobs"] == []
    assert client.get("/v1/migrations", params={"limit": 100000}).status_code == 200


# ------------------------------------------------------------------ cancel ---


def test_a_queued_job_can_be_withdrawn(client: TestClient, workspace: Path) -> None:
    job_id = client.post("/v1/migrations", json=body(workspace)).json()["id"]
    response = client.delete(f"/v1/migrations/{job_id}")
    assert response.status_code == 200
    assert response.json()["state"] == JobState.CANCELLED.value


def test_cancelling_a_finished_job_is_a_conflict(
    client: TestClient, workspace: Path, queue: JobStore
) -> None:
    job_id = client.post("/v1/migrations", json=body(workspace)).json()["id"]
    assert queue.claim("w") is not None
    queue.succeed(job_id, run_id="r", worker="w")
    assert client.delete(f"/v1/migrations/{job_id}").status_code == 409


def test_cancelling_an_unknown_job_is_a_404(client: TestClient) -> None:
    assert client.delete("/v1/migrations/absent").status_code == 404


# ----------------------------------------------------------------- roots ---


def test_allowed_roots_can_be_given_as_a_string() -> None:
    """Environment variables are strings; a list has to survive the trip."""
    parsed = ApiSettings(allowed_roots="/tmp/a,/tmp/b")  # type: ignore[arg-type]  # noqa: S108
    assert len(parsed.allowed_roots) == 2
    assert all(root.is_absolute() for root in parsed.allowed_roots)


def test_allowed_roots_survive_the_trip_through_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructing the model directly does not exercise the env source.

    The first version of this setting parsed fine in a test and failed the moment
    the command ran, because pydantic-settings decodes a complex field from the
    environment as JSON before any validator sees it. Reading it the way the
    process actually does is the only way that shows.
    """
    from rewire.core.config import get_settings

    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("REWIRE_API__TOKEN", TOKEN)
    monkeypatch.setenv("REWIRE_API__ALLOWED_ROOTS", f"{first}:{second}")
    get_settings.cache_clear()
    try:
        roots = get_settings().api.allowed_roots
        assert roots == (first.resolve(), second.resolve())
    finally:
        get_settings.cache_clear()
