"""Tests for the confidence model."""

from __future__ import annotations

import math

import pytest

from rewire.analyzers.models import ReferenceKind
from rewire.changes.models import ChangeLocation
from rewire.impact.models import Signal, SignalKind
from rewire.impact.scoring import (
    REFERENCE_KIND_WEIGHTS,
    confidence_from,
    direction_signal,
    order_signals,
    package_import_signal,
    reference_kind_signal,
    sdk_call_target_signal,
    sigmoid,
)


def test_sigmoid_is_centred_at_zero() -> None:
    assert sigmoid(0.0) == pytest.approx(0.5)


def test_sigmoid_is_monotonic() -> None:
    values = [sigmoid(x) for x in (-5, -1, 0, 1, 5)]
    assert values == sorted(values)


def test_sigmoid_saturates_without_overflowing() -> None:
    """Strong evidence must saturate rather than raise."""
    assert sigmoid(10_000.0) == pytest.approx(1.0)
    assert sigmoid(-10_000.0) == pytest.approx(0.0)
    assert not math.isnan(sigmoid(-10_000.0))


def test_confidence_of_no_evidence_is_even() -> None:
    assert confidence_from([]) == pytest.approx(0.5)


def test_signals_accumulate() -> None:
    one = [Signal(kind=SignalKind.REFERENCE_KIND, weight=1.0)]
    two = [*one, Signal(kind=SignalKind.PACKAGE_DECLARED, weight=1.0)]
    assert confidence_from(two) > confidence_from(one) > 0.5


def test_negative_signals_argue_against() -> None:
    assert confidence_from([Signal(kind=SignalKind.PACKAGE_ABSENT, weight=-2.0)]) < 0.5


def test_every_reference_kind_has_a_weight() -> None:
    """A missing weight would raise KeyError during analysis, not at import."""
    assert set(REFERENCE_KIND_WEIGHTS) == set(ReferenceKind)


def test_reference_kind_weights_are_ordered_by_strength() -> None:
    assert (
        REFERENCE_KIND_WEIGHTS[ReferenceKind.KEYWORD_ARGUMENT]
        > REFERENCE_KIND_WEIGHTS[ReferenceKind.DICT_KEY]
        > REFERENCE_KIND_WEIGHTS[ReferenceKind.SUBSCRIPT_KEY]
        > REFERENCE_KIND_WEIGHTS[ReferenceKind.ATTRIBUTE]
        > REFERENCE_KIND_WEIGHTS[ReferenceKind.NAME]
        > REFERENCE_KIND_WEIGHTS[ReferenceKind.STRING_LITERAL]
    )


def test_signals_are_ordered_by_contribution() -> None:
    """Reports are read top-down; the dominant reason must come first."""
    ordered = order_signals(
        [
            Signal(kind=SignalKind.PACKAGE_DECLARED, weight=0.3),
            Signal(kind=SignalKind.SDK_CALL_TARGET, weight=2.0),
            Signal(kind=SignalKind.PACKAGE_NOT_IMPORTED, weight=-1.5),
        ]
    )
    assert [signal.weight for signal in ordered] == [2.0, -1.5, 0.3]


def test_a_resolved_sdk_call_dominates() -> None:
    """The only signal linking the name to the library outweighs the rest."""
    with_call = confidence_from(
        [reference_kind_signal(ReferenceKind.KEYWORD_ARGUMENT), sdk_call_target_signal("openai.x")]
    )
    without = confidence_from([reference_kind_signal(ReferenceKind.KEYWORD_ARGUMENT)])
    assert with_call > without


def test_a_missing_import_sinks_a_keyword_match() -> None:
    """The shape of a false positive: right name, wrong library."""
    decoy = confidence_from(
        [
            reference_kind_signal(ReferenceKind.PARAMETER),
            package_import_signal("openai", imported=False),
        ]
    )
    assert decoy < 0.35


# ------------------------------------------------------------ variance ----


@pytest.mark.parametrize(
    ("location", "kind", "expected"),
    [
        # A request field is written by the client.
        (ChangeLocation.REQUEST_BODY, ReferenceKind.KEYWORD_ARGUMENT, True),
        (ChangeLocation.QUERY, ReferenceKind.DICT_KEY, True),
        (ChangeLocation.REQUEST_BODY, ReferenceKind.ATTRIBUTE, False),
        # A response field is read from it.
        (ChangeLocation.RESPONSE, ReferenceKind.ATTRIBUTE, True),
        (ChangeLocation.RESPONSE, ReferenceKind.SUBSCRIPT_KEY, True),
        (ChangeLocation.RESPONSE, ReferenceKind.DICT_KEY, False),
        (ChangeLocation.RESPONSE, ReferenceKind.KEYWORD_ARGUMENT, False),
    ],
)
def test_direction_agreement(location: ChangeLocation, kind: ReferenceKind, expected: bool) -> None:
    signal = direction_signal(location, kind)
    assert signal is not None
    assert (signal.weight > 0) is expected


@pytest.mark.parametrize(
    ("location", "kind"),
    [
        (None, ReferenceKind.DICT_KEY),
        (ChangeLocation.OPERATION, ReferenceKind.DICT_KEY),
        (ChangeLocation.RESPONSE, ReferenceKind.NAME),
        (ChangeLocation.RESPONSE, ReferenceKind.STRING_LITERAL),
    ],
)
def test_direction_is_silent_when_uninformative(
    location: ChangeLocation | None, kind: ReferenceKind
) -> None:
    """A bare name says nothing about which way data flows."""
    assert direction_signal(location, kind) is None


def test_constructing_a_response_field_scores_below_reading_it() -> None:
    """The distinction that separates a real break from a test double."""
    read = confidence_from(
        [
            reference_kind_signal(ReferenceKind.ATTRIBUTE),
            direction_signal(ChangeLocation.RESPONSE, ReferenceKind.ATTRIBUTE)
            or Signal(kind=SignalKind.DIRECTION_CONSISTENT, weight=0.0),
        ]
    )
    written = confidence_from(
        [
            reference_kind_signal(ReferenceKind.DICT_KEY),
            direction_signal(ChangeLocation.RESPONSE, ReferenceKind.DICT_KEY)
            or Signal(kind=SignalKind.DIRECTION_CONSISTENT, weight=0.0),
        ]
    )
    assert read > written
