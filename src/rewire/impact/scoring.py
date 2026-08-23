"""The confidence model for impact analysis.

Deciding whether a line is affected is not a yes/no question a parser can
answer. ``max_tokens`` appearing in a file might be the SDK field being
migrated, an unrelated parameter on the repository's own function, or a word in
a log message. What separates them is *accumulated evidence*, and that is what
this module models.

Signals are combined in **log-odds** rather than by multiplying or averaging
probabilities. Three properties make that the right shape:

* Weights add, so a location's score is a sum and each signal's contribution is
  readable straight off the report.
* Evidence against a location is expressible as a negative weight, rather than
  needing a separate veto mechanism.
* The sigmoid keeps the result in ``[0, 1]`` without clamping, so strong
  evidence saturates smoothly instead of hitting a ceiling and losing ordering
  information among the top candidates.

The weights below are **hand-assigned priors, not fitted parameters**. They were
chosen so that the ordering they produce matches the ordering a reviewer would
give, and they are asserted against ground-truth fixtures rather than trusted.
Phase 8 replaces them with values fitted to labelled impact data; until then
they are honestly a prior, and this module is written so that swapping them is a
one-line change.
"""

from __future__ import annotations

import math
from typing import Final

from rewire.analyzers.models import ReferenceKind
from rewire.changes.models import ChangeLocation
from rewire.impact.models import Signal, SignalKind

#: Starting log-odds before any evidence. Zero means "even chance": a bare name
#: match is treated as equally likely to be relevant or not until something says
#: otherwise.
PRIOR_LOGIT: Final[float] = 0.0

#: Evidence contributed by *how* the name appears at the location. A keyword
#: argument is the strongest syntactic evidence available short of resolving the
#: callee; a bare string literal argues mildly against, because prose and log
#: messages contain field names far more often than they constitute usage.
REFERENCE_KIND_WEIGHTS: Final[dict[ReferenceKind, float]] = {
    ReferenceKind.KEYWORD_ARGUMENT: 1.6,
    ReferenceKind.DICT_KEY: 1.2,
    ReferenceKind.SUBSCRIPT_KEY: 0.9,
    ReferenceKind.PARAMETER: 0.5,
    ReferenceKind.ATTRIBUTE: 0.1,
    ReferenceKind.NAME: -0.4,
    ReferenceKind.STRING_LITERAL: -0.8,
}

#: The reference is an argument to a call whose target resolves into the changed
#: API's package. This is the single strongest signal available, because it is
#: the only one that connects the *name* to the *library* rather than inferring
#: a connection from proximity.
SDK_CALL_TARGET_WEIGHT: Final[float] = 2.0

#: The file imports the package. Weaker than a resolved call target, but it is
#: what distinguishes a client module from an unrelated one that happens to use
#: the same word.
PACKAGE_IMPORTED_WEIGHT: Final[float] = 1.0

#: The file names the field but does not import the package at all. Strong
#: evidence against: this is the shape of a false positive. Weighted heavily
#: enough to sink a decoy that has nothing else going for it -- a local helper
#: whose parameter merely shares the name -- which measurement showed a milder
#: weight could not do.
PACKAGE_NOT_IMPORTED_WEIGHT: Final[float] = -2.0

#: The package is declared in the repository's metadata. Deliberately weak: it
#: says the SDK is used *somewhere in the repository*, not that it is used in
#: this file, and it applies equally to every file. At a higher weight it was
#: floating decoys above the reporting threshold on no evidence of their own.
PACKAGE_DECLARED_WEIGHT: Final[float] = 0.3

#: The changed endpoint's path appears literally in the file, e.g. a raw HTTP
#: client calling "/v1/chat/completions" without the SDK. Often the only handle
#: available when a repository talks to the API without a client library.
ENDPOINT_MENTIONED_WEIGHT: Final[float] = 0.8

#: The repository shows no sign of the package anywhere -- it is neither
#: declared as a dependency nor imported by any file. The symmetric counterpart
#: of PACKAGE_DECLARED, and the difference between "this repository uses the SDK
#: but not in this file" and "this repository does not use the SDK at all".
PACKAGE_ABSENT_WEIGHT: Final[float] = -0.8

#: The file imports a module of this repository that itself calls the SDK. This
#: is the call-graph-proximity signal: a wrapper or a test one hop from the API
#: never imports the package, so without it every such site looks like a decoy.
IMPORTS_SDK_MODULE_WEIGHT: Final[float] = 1.2

#: The enclosing function also calls the SDK, so the name occurs in a context
#: that demonstrably talks to the API even if this line does not.
ENCLOSING_SDK_USAGE_WEIGHT: Final[float] = 0.7

#: The call forwards ``**kwargs``, so the arguments it really passes are not
#: visible. Mildly positive: the site is more likely to need review, not less,
#: but static analysis cannot confirm the field is involved.
OPAQUE_ARGUMENTS_WEIGHT: Final[float] = 0.3

#: Test code. Not down-weighted: a migration that leaves the tests calling the
#: old API leaves the build broken, so tests are genuinely affected. The signal
#: is recorded at zero weight so the report can distinguish them without the
#: ranking pretending they matter less.
TEST_CODE_WEIGHT: Final[float] = 0.0


#: Ways a name is written that mean the code is *producing* a value.
_PRODUCING_KINDS: Final[frozenset[ReferenceKind]] = frozenset(
    {ReferenceKind.KEYWORD_ARGUMENT, ReferenceKind.DICT_KEY, ReferenceKind.PARAMETER}
)

#: Ways that mean the code is *consuming* one.
_CONSUMING_KINDS: Final[frozenset[ReferenceKind]] = frozenset(
    {ReferenceKind.ATTRIBUTE, ReferenceKind.SUBSCRIPT_KEY}
)

#: Parts of an operation the client writes.
_REQUEST_LOCATIONS: Final[frozenset[ChangeLocation]] = frozenset(
    {
        ChangeLocation.QUERY,
        ChangeLocation.HEADER,
        ChangeLocation.PATH,
        ChangeLocation.COOKIE,
        ChangeLocation.REQUEST_BODY,
    }
)

#: Agreement between how the field travels and how the code uses the name.
DIRECTION_CONSISTENT_WEIGHT: Final[float] = 0.5

#: Disagreement. Weighted heavily because it is not weak evidence but a
#: contradiction: a field that only ever arrives in a response cannot be the
#: one being set by ``{"role": "user"}``. Without this, every generic response
#: field name matched the request payloads that happened to share it.
DIRECTION_INCONSISTENT_WEIGHT: Final[float] = -2.0


def sigmoid(logit: float) -> float:
    """Map summed log-odds to a probability in ``[0, 1]``.

    Guarded against overflow so that a very large accumulation of evidence
    saturates rather than raising.
    """
    if logit >= 0:
        return 1.0 / (1.0 + math.exp(-min(logit, 700.0)))
    exponential = math.exp(max(logit, -700.0))
    return exponential / (1.0 + exponential)


def confidence_from(signals: list[Signal]) -> float:
    """Return the confidence implied by a set of signals."""
    return sigmoid(PRIOR_LOGIT + sum(signal.weight for signal in signals))


def order_signals(signals: list[Signal]) -> tuple[Signal, ...]:
    """Order signals by the size of their contribution, largest first.

    Reports are read top-down, and the first line of the explanation should be
    the reason that dominated the score.
    """
    return tuple(sorted(signals, key=lambda signal: (-abs(signal.weight), signal.kind.value)))


def reference_kind_signal(kind: ReferenceKind) -> Signal:
    """Signal for how a name appears at a location."""
    return Signal(
        kind=SignalKind.REFERENCE_KIND,
        weight=REFERENCE_KIND_WEIGHTS[kind],
        detail=f"occurs as a {kind.value.replace('_', ' ')}",
    )


def sdk_call_target_signal(resolved_callee: str) -> Signal:
    """Signal that the reference sits on a call resolving into the SDK."""
    return Signal(
        kind=SignalKind.SDK_CALL_TARGET,
        weight=SDK_CALL_TARGET_WEIGHT,
        detail=f"argument to {resolved_callee}",
    )


def package_import_signal(package: str, *, imported: bool) -> Signal:
    """Signal for whether the file imports the changed API's package."""
    if imported:
        return Signal(
            kind=SignalKind.PACKAGE_IMPORTED,
            weight=PACKAGE_IMPORTED_WEIGHT,
            detail=f"file imports {package}",
        )
    return Signal(
        kind=SignalKind.PACKAGE_NOT_IMPORTED,
        weight=PACKAGE_NOT_IMPORTED_WEIGHT,
        detail=f"file does not import {package}",
    )


def package_declared_signal(package: str) -> Signal:
    """Signal that the package is a declared dependency."""
    return Signal(
        kind=SignalKind.PACKAGE_DECLARED,
        weight=PACKAGE_DECLARED_WEIGHT,
        detail=f"{package} is a declared dependency",
    )


def package_absent_signal(package: str) -> Signal:
    """Signal that the repository shows no sign of the package at all."""
    return Signal(
        kind=SignalKind.PACKAGE_ABSENT,
        weight=PACKAGE_ABSENT_WEIGHT,
        detail=f"{package} is neither declared nor imported anywhere",
    )


def imports_sdk_module_signal(module: str) -> Signal:
    """Signal that the file imports a local module which itself uses the SDK."""
    return Signal(
        kind=SignalKind.IMPORTS_SDK_MODULE,
        weight=IMPORTS_SDK_MODULE_WEIGHT,
        detail=f"imports {module}, which calls the SDK",
    )


def endpoint_mentioned_signal(path: str) -> Signal:
    """Signal that the endpoint's path appears literally in the file."""
    return Signal(
        kind=SignalKind.ENDPOINT_MENTIONED,
        weight=ENDPOINT_MENTIONED_WEIGHT,
        detail=f"file mentions {path}",
    )


def enclosing_sdk_usage_signal(symbol: str) -> Signal:
    """Signal that the enclosing symbol also calls the SDK."""
    return Signal(
        kind=SignalKind.ENCLOSING_SDK_USAGE,
        weight=ENCLOSING_SDK_USAGE_WEIGHT,
        detail=f"{symbol} also calls the SDK",
    )


def opaque_arguments_signal(callee: str) -> Signal:
    """Signal that a call forwards arguments statically invisible to analysis."""
    return Signal(
        kind=SignalKind.OPAQUE_ARGUMENTS,
        weight=OPAQUE_ARGUMENTS_WEIGHT,
        detail=f"{callee} forwards **kwargs",
    )


def direction_signal(location: ChangeLocation | None, kind: ReferenceKind) -> Signal | None:
    """Compare the direction a field travels with how the code uses its name.

    A request field is *written* by the client, so it appears as a keyword
    argument, a dict key or a forwarded parameter. A response field is *read*,
    so it appears as an attribute or a subscript. When the two disagree, the
    name almost certainly refers to something else that merely shares it.

    Returns ``None`` when the comparison carries no information -- an
    operation-level change, or a bare name that says nothing about direction.
    """
    if location is None or location is ChangeLocation.OPERATION:
        return None

    writes = location in _REQUEST_LOCATIONS
    if kind in _PRODUCING_KINDS:
        consistent = writes
    elif kind in _CONSUMING_KINDS:
        consistent = not writes
    else:
        return None

    direction = "request" if writes else "response"
    usage = "written" if kind in _PRODUCING_KINDS else "read"
    if consistent:
        return Signal(
            kind=SignalKind.DIRECTION_CONSISTENT,
            weight=DIRECTION_CONSISTENT_WEIGHT,
            detail=f"{direction} field is {usage} here",
        )
    return Signal(
        kind=SignalKind.DIRECTION_INCONSISTENT,
        weight=DIRECTION_INCONSISTENT_WEIGHT,
        detail=f"{direction} field would not be {usage} here",
    )


def in_test_code_signal(path: str) -> Signal:
    """Marker that a location is test code; carries no weight by design.

    Named to avoid the ``test_`` prefix: pytest collects any such name it can
    import as a test case, and this is a factory function, not a test.
    """
    return Signal(
        kind=SignalKind.TEST_CODE,
        weight=TEST_CODE_WEIGHT,
        detail=f"{path} is test code",
    )


__all__ = [
    "DIRECTION_CONSISTENT_WEIGHT",
    "DIRECTION_INCONSISTENT_WEIGHT",
    "ENCLOSING_SDK_USAGE_WEIGHT",
    "ENDPOINT_MENTIONED_WEIGHT",
    "IMPORTS_SDK_MODULE_WEIGHT",
    "OPAQUE_ARGUMENTS_WEIGHT",
    "PACKAGE_ABSENT_WEIGHT",
    "PACKAGE_DECLARED_WEIGHT",
    "PACKAGE_IMPORTED_WEIGHT",
    "PACKAGE_NOT_IMPORTED_WEIGHT",
    "PRIOR_LOGIT",
    "REFERENCE_KIND_WEIGHTS",
    "SDK_CALL_TARGET_WEIGHT",
    "TEST_CODE_WEIGHT",
    "confidence_from",
    "direction_signal",
    "enclosing_sdk_usage_signal",
    "endpoint_mentioned_signal",
    "imports_sdk_module_signal",
    "in_test_code_signal",
    "opaque_arguments_signal",
    "order_signals",
    "package_absent_signal",
    "package_declared_signal",
    "package_import_signal",
    "reference_kind_signal",
    "sdk_call_target_signal",
    "sigmoid",
]
