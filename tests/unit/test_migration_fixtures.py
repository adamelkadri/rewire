"""End-to-end assertions against fixtures modelling real API migrations.

These are the closest thing Phase 1 has to ground truth: each pair encodes a
migration that actually happened, and the assertions state what a correct
detector must say about it. They are what stops a refactor of the severity
model from silently changing what Rewire reports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.changes import ApiChange, ChangeReport, ChangeType, Severity, diff_specs, load_spec


def report_for(specs: Path, old: str, new: str) -> ChangeReport:
    return diff_specs(load_spec(specs / old), load_spec(specs / new))


def find(report: ChangeReport, change_type: ChangeType, field_path: str) -> ApiChange:
    matches = [
        change
        for change in report.changes
        if change.type is change_type and change.field_path == field_path
    ]
    assert len(matches) == 1, f"expected exactly one {change_type} at {field_path}: {matches}"
    return matches[0]


# ------------------------------------------------------------------ OpenAI ---


@pytest.fixture
def openai(specs: Path) -> ChangeReport:
    return report_for(specs, "openai/chat_old.yaml", "openai/chat_new.yaml")


def test_openai_detects_the_max_tokens_rename(openai: ChangeReport) -> None:
    removed = find(openai, ChangeType.REQUEST_FIELD_REMOVED, "max_tokens")
    assert removed.severity is Severity.BREAKING
    assert removed.replacement == "max_completion_tokens"
    assert removed.endpoint == "POST /v1/chat/completions"


def test_openai_does_not_invent_a_functions_to_tools_rename(openai: ChangeReport) -> None:
    """The names share no token; guessing would send the agent to the wrong symbol."""
    assert find(openai, ChangeType.REQUEST_FIELD_REMOVED, "functions").replacement is None
    assert find(openai, ChangeType.REQUEST_FIELD_ADDED, "tools").severity is Severity.NON_BREAKING


def test_openai_flags_completion_tokens_becoming_optional(openai: ChangeReport) -> None:
    """A response field that may now be absent breaks every unguarded read of it."""
    change = find(openai, ChangeType.RESPONSE_FIELD_BECAME_OPTIONAL, "usage.completion_tokens")
    assert change.severity is Severity.BREAKING


def test_openai_grades_enum_widening_by_direction(openai: ChangeReport) -> None:
    request_enum = find(openai, ChangeType.REQUEST_SCHEMA_CHANGED, "messages[].role")
    response_enum = find(openai, ChangeType.RESPONSE_SCHEMA_CHANGED, "choices[].finish_reason")
    # Sending a value the server already accepted still works...
    assert request_enum.severity is Severity.NON_BREAKING
    # ...but receiving a value the client has never seen may not.
    assert response_enum.severity is Severity.POTENTIALLY_BREAKING


def test_openai_detects_the_removed_completions_endpoint(openai: ChangeReport) -> None:
    removed = [c for c in openai.changes if c.type is ChangeType.ENDPOINT_REMOVED]
    assert [c.endpoint for c in removed] == ["POST /v1/completions"]
    assert removed[0].severity is Severity.BREAKING


def test_openai_new_response_field_is_non_breaking(openai: ChangeReport) -> None:
    assert (
        find(openai, ChangeType.RESPONSE_FIELD_ADDED, "system_fingerprint").severity
        is Severity.NON_BREAKING
    )


def test_openai_summary(openai: ChangeReport) -> None:
    assert openai.summary.breaking == 5
    assert openai.has_breaking_changes


# --------------------------------------------------------------- Anthropic ---


@pytest.fixture
def anthropic(specs: Path) -> ChangeReport:
    return report_for(specs, "anthropic/messages_old.json", "anthropic/messages_new.json")


def test_anthropic_json_specs_are_supported(anthropic: ChangeReport) -> None:
    assert anthropic.old_spec.openapi_version == "3.1.0"
    assert anthropic.summary.total > 0


def test_anthropic_detects_max_tokens_to_sample_rename(anthropic: ChangeReport) -> None:
    removed = find(anthropic, ChangeType.REQUEST_FIELD_REMOVED, "max_tokens_to_sample")
    assert removed.replacement == "max_tokens"
    assert removed.severity is Severity.BREAKING


def test_anthropic_detects_operation_deprecation(anthropic: ChangeReport) -> None:
    deprecated = [c for c in anthropic.changes if c.type is ChangeType.OPERATION_DEPRECATED]
    assert [c.endpoint for c in deprecated] == ["POST /v1/complete"]


def test_anthropic_new_optional_header_is_non_breaking(anthropic: ChangeReport) -> None:
    added = find(anthropic, ChangeType.PARAMETER_ADDED, "anthropic-beta")
    assert added.severity is Severity.NON_BREAKING
    assert added.location is not None and added.location.value == "header"


def test_anthropic_response_enum_widening_is_flagged(anthropic: ChangeReport) -> None:
    change = find(anthropic, ChangeType.RESPONSE_SCHEMA_CHANGED, "stop_reason")
    assert change.severity is Severity.POTENTIALLY_BREAKING
    assert change.new_value == ["tool_use"]


# ------------------------------------------------------------------ Stripe ---


@pytest.fixture
def stripe(specs: Path) -> ChangeReport:
    return report_for(specs, "stripe/charges_old.yaml", "stripe/charges_new.yaml")


def test_stripe_detects_endpoint_replacement(stripe: ChangeReport) -> None:
    removed = {c.endpoint for c in stripe.changes if c.type is ChangeType.ENDPOINT_REMOVED}
    added = {c.endpoint for c in stripe.changes if c.type is ChangeType.ENDPOINT_ADDED}
    assert removed == {"POST /v1/charges/{charge}/refund"}
    assert added == {"POST /v1/payment_intents", "POST /v1/refunds"}


def test_stripe_detects_removed_post_on_a_surviving_path(stripe: ChangeReport) -> None:
    """/v1/charges still exists for GET, so this is an operation change."""
    removed = [c for c in stripe.changes if c.type is ChangeType.OPERATION_REMOVED]
    assert [c.endpoint for c in removed] == ["POST /v1/charges"]
    assert removed[0].severity is Severity.BREAKING


def test_stripe_detects_deprecations(stripe: ChangeReport) -> None:
    assert any(c.type is ChangeType.OPERATION_DEPRECATED for c in stripe.changes)
    assert find(stripe, ChangeType.PARAMETER_DEPRECATED, "customer").severity is (
        Severity.POTENTIALLY_BREAKING
    )


def test_stripe_detects_tightened_limit(stripe: ChangeReport) -> None:
    change = find(stripe, ChangeType.PARAMETER_SCHEMA_CHANGED, "limit")
    assert change.old_value == {"maximum": 100}
    assert change.new_value == {"maximum": 50}
    assert change.severity is Severity.POTENTIALLY_BREAKING


# ------------------------------------------------------------------ GitHub ---


@pytest.fixture
def github(specs: Path) -> ChangeReport:
    return report_for(specs, "github/issues_old.yaml", "github/issues_new.yaml")


def test_github_detects_newly_required_parameter(github: ChangeReport) -> None:
    change = find(github, ChangeType.REQUIRED_PARAMETER_ADDED, "since")
    assert change.severity is Severity.BREAKING


def test_github_detects_narrowed_request_enum(github: ChangeReport) -> None:
    """Dropping 'all' rejects a request that previously succeeded."""
    change = find(github, ChangeType.PARAMETER_SCHEMA_CHANGED, "state")
    assert change.severity is Severity.BREAKING
    assert change.old_value == ["all"]


def test_github_detects_assignee_becoming_optional(github: ChangeReport) -> None:
    change = find(github, ChangeType.RESPONSE_FIELD_BECAME_OPTIONAL, "[].assignee")
    assert change.severity is Severity.BREAKING
    assert change.field == "assignee"


def test_github_nullable_fields_that_did_not_change_are_silent(github: ChangeReport) -> None:
    assert not [c for c in github.changes if c.field_path == "[].body"]


# ------------------------------------------------------------------ general ---


ALL_PAIRS = [
    ("openai/chat_old.yaml", "openai/chat_new.yaml"),
    ("anthropic/messages_old.json", "anthropic/messages_new.json"),
    ("stripe/charges_old.yaml", "stripe/charges_new.yaml"),
    ("github/issues_old.yaml", "github/issues_new.yaml"),
]


@pytest.mark.parametrize(("old", "new"), ALL_PAIRS)
def test_diffing_a_spec_against_itself_finds_nothing(specs: Path, old: str, new: str) -> None:
    """The strongest available check for false positives."""
    for name in (old, new):
        spec = load_spec(specs / name)
        assert diff_specs(spec, spec).changes == []


@pytest.mark.parametrize(("old", "new"), ALL_PAIRS)
def test_every_change_locates_itself(specs: Path, old: str, new: str) -> None:
    """Phase 3 can only search for a change that says where it applies."""
    for change in report_for(specs, old, new).changes:
        assert change.endpoint
        assert change.path and change.method
        assert change.detail
        if change.field_path:
            assert change.field


@pytest.mark.parametrize(("old", "new"), ALL_PAIRS)
def test_reports_round_trip_through_json(specs: Path, old: str, new: str) -> None:
    report = report_for(specs, old, new)
    assert ChangeReport.model_validate_json(report.model_dump_json()) == report
