"""Tests for deterministic rename detection."""

from __future__ import annotations

import pytest

from rewire.changes.renames import (
    RENAME_SCORE_THRESHOLD,
    detect_renames,
    name_similarity,
    schema_compatibility,
    score_pair,
    tokenize,
)

INT = {"type": "integer"}
STR = {"type": "string"}


def test_tokenize_handles_snake_and_camel_case() -> None:
    assert tokenize("max_output_tokens") == {"max", "output", "tokens"}
    assert tokenize("maxOutputTokens") == {"max", "output", "tokens"}
    assert tokenize("max-output.tokens") == {"max", "output", "tokens"}
    assert tokenize("") == frozenset()


def test_convention_change_alone_scores_perfectly() -> None:
    assert name_similarity("maxTokens", "max_tokens") == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("max_tokens", "max_output_tokens"),
        ("max_tokens", "max_completion_tokens"),
        ("max_tokens", "max_tokens_to_sample"),
        ("created", "created_at"),
        ("item", "items"),
    ],
)
def test_real_renames_are_detected(old: str, new: str) -> None:
    assert score_pair(old, INT, new, INT) >= RENAME_SCORE_THRESHOLD


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # Both integers, but nothing in the names connects them. An earlier
        # scorer paired these purely on shared type.
        ("max_tokens", "temperature"),
        # 'user' is a subsequence of 'customer'; character similarity alone
        # would call this a rename.
        ("user", "customer"),
        ("functions", "tools"),
        ("charge", "payment_intent"),
    ],
)
def test_unrelated_names_are_not_paired(old: str, new: str) -> None:
    assert score_pair(old, INT, new, INT) < RENAME_SCORE_THRESHOLD


def test_incompatible_schemas_veto_the_pair() -> None:
    """Names alone must never carry a pair whose types disagree."""
    assert name_similarity("max_tokens", "max_output_tokens") > RENAME_SCORE_THRESHOLD
    assert score_pair("max_tokens", INT, "max_output_tokens", STR) == 0.0


def test_schema_compatibility_scale() -> None:
    assert schema_compatibility(INT, INT) == pytest.approx(1.0)
    assert schema_compatibility(INT, STR) == 0.0
    assert schema_compatibility({}, {}) == pytest.approx(0.9)
    assert schema_compatibility(INT, {}) == pytest.approx(0.85)
    assert schema_compatibility(
        {"type": "string", "format": "date"}, {"type": "string", "format": "date-time"}
    ) == pytest.approx(0.9)


def test_detect_renames_pairs_the_obvious_case() -> None:
    matched = detect_renames({"max_tokens": INT}, {"max_output_tokens": INT})
    assert [(c.old_name, c.new_name) for c in matched] == [("max_tokens", "max_output_tokens")]


def test_each_name_is_used_at_most_once() -> None:
    matched = detect_renames(
        {"max_tokens": INT, "min_tokens": INT},
        {"max_output_tokens": INT, "min_output_tokens": INT},
    )
    assert {c.old_name for c in matched} == {"max_tokens", "min_tokens"}
    assert {c.new_name for c in matched} == {"max_output_tokens", "min_output_tokens"}


def test_best_scoring_pair_wins_when_candidates_compete() -> None:
    matched = detect_renames(
        {"max_tokens": INT}, {"max_tokens_v2": INT, "max_completion_tokens": INT}
    )
    assert len(matched) == 1
    assert matched[0].new_name == "max_tokens_v2"


def test_matching_is_independent_of_input_order() -> None:
    forward = detect_renames(
        {"a_tokens": INT, "b_tokens": INT}, {"a_tokens_new": INT, "b_tokens_new": INT}
    )
    reverse = detect_renames(
        {"b_tokens": INT, "a_tokens": INT}, {"b_tokens_new": INT, "a_tokens_new": INT}
    )
    assert sorted(forward) == sorted(reverse)


def test_results_are_ordered_by_descending_score() -> None:
    matched = detect_renames(
        {"maxTokens": INT, "max_tokens": INT},
        {"max_tokens_alt": INT, "max_tokens_to_sample_alt": INT},
    )
    scores = [candidate.score for candidate in matched]
    assert scores == sorted(scores, reverse=True)


def test_no_candidates_yields_no_pairs() -> None:
    assert detect_renames({}, {"a": INT}) == []
    assert detect_renames({"a": INT}, {}) == []


def test_threshold_is_configurable() -> None:
    assert detect_renames({"user": STR}, {"customer": STR}) == []
    assert len(detect_renames({"user": STR}, {"customer": STR}, threshold=0.0)) == 1


def test_impossible_threshold_disables_detection() -> None:
    assert detect_renames({"max_tokens": INT}, {"max_output_tokens": INT}, threshold=1.1) == []


def test_differing_enums_discount_but_do_not_veto() -> None:
    assert schema_compatibility(
        {"type": "string", "enum": ["a"]}, {"type": "string", "enum": ["a", "b"]}
    ) == pytest.approx(0.9)
