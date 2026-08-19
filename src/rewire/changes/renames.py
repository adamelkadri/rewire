"""Deterministic detection of renamed parameters and fields.

A rename appears in a raw diff as two unrelated events: something removed,
something added. Reporting it that way is technically correct and practically
useless — the migration Rewire needs to generate is "replace X with Y", and
Phase 3 needs the replacement name to rank affected call sites.

Pairing is done with two deterministic signals and no model call:

1. **Name similarity**, driven by token overlap on ``snake_case``/``camelCase``
   boundaries. This is the shape most real renames take: ``max_tokens`` →
   ``max_completion_tokens`` shares two of three tokens.
2. **Schema compatibility**, applied as a *multiplier* rather than a bonus. A
   type mismatch vetoes the pair outright. It cannot promote one, because most
   API fields are just ``string`` or ``integer`` and agreeing on that is no
   evidence at all — an early version of this scorer paired ``max_tokens`` with
   ``temperature`` purely because both were integers.

Candidates are matched greedily by descending score, so the assignment is stable
and independent of dictionary ordering. Anything below
:data:`RENAME_SCORE_THRESHOLD` stays reported as a separate removal and addition.

The scorer is deliberately biased towards missing renames rather than inventing
them: a missed rename degrades to two honest changes, whereas a wrong one sends
the agent to edit the wrong symbol. Renames that share no tokens are therefore
not detected at all -- Stripe's ``charge`` to ``payment_intent`` is invisible to
any name-based heuristic, and guessing at it would be worse than reporting the
removal and the addition separately.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Final, NamedTuple

from rewire.changes.schema_diff import type_set
from rewire.changes.spec import JsonSchema

#: Minimum score for a removal/addition pair to be called a rename. At 0.5 a
#: pair must share at least half its tokens (``max_tokens`` →
#: ``max_tokens_to_sample``) or be a near-spelling variant.
RENAME_SCORE_THRESHOLD: Final[float] = 0.5

#: Character-level similarity is only trusted above this value, where it means
#: "almost the same string" (``item`` → ``items``). At moderate values it fires
#: on unrelated names that merely share letters: ``user`` and ``customer`` score
#: 0.67 on characters alone despite sharing no token.
HIGH_CHARACTER_SIMILARITY: Final[float] = 0.8

_TOKEN_BOUNDARY = re.compile(r"[_\-. ]+|(?<=[a-z0-9])(?=[A-Z])")


class RenameCandidate(NamedTuple):
    """A removal paired with an addition, with the score that paired them."""

    old_name: str
    new_name: str
    score: float


def tokenize(name: str) -> frozenset[str]:
    """Split an identifier into lowercase tokens on case and separator boundaries.

    ``maxOutputTokens`` and ``max_output_tokens`` both yield
    ``{"max", "output", "tokens"}``, so a rename that also changes convention is
    still recognised.
    """
    return frozenset(part.lower() for part in _TOKEN_BOUNDARY.split(name) if part)


def name_similarity(old: str, new: str) -> float:
    """Score how likely two identifiers are to name the same concept, in ``[0, 1]``.

    Primarily token-set Jaccard similarity, which handles added, dropped and
    reordered words. Character similarity is consulted only when it is high
    enough to mean "almost the same string", which catches morphological
    variants such as ``item`` → ``items`` that share no whole token.
    """
    old_tokens, new_tokens = tokenize(old), tokenize(new)
    jaccard = (
        len(old_tokens & new_tokens) / len(old_tokens | new_tokens)
        if old_tokens or new_tokens
        else 0.0
    )
    character = SequenceMatcher(None, old.lower(), new.lower()).ratio()
    if character < HIGH_CHARACTER_SIMILARITY:
        return jaccard
    return max(jaccard, character)


def schema_compatibility(old: JsonSchema, new: JsonSchema) -> float:
    """Score how compatible two schemas are, as a multiplier in ``[0, 1]``.

    A type mismatch returns ``0`` and vetoes the pair. Matching types return
    ``1`` but confer no advantage over an unknown schema beyond a small discount,
    because agreeing on ``string`` is not evidence of anything. Untyped schemas
    are treated as absence of evidence, not as disagreement.
    """
    old_types, new_types = type_set(old), type_set(new)
    if not old_types or not new_types:
        return 0.9 if not old_types and not new_types else 0.85
    if old_types != new_types:
        return 0.0

    score = 1.0
    if old.get("format") != new.get("format"):
        score -= 0.1
    if old.get("enum") != new.get("enum"):
        score -= 0.1
    return score


def score_pair(
    old_name: str, old_schema: JsonSchema, new_name: str, new_schema: JsonSchema
) -> float:
    """Combined likelihood that ``new_name`` is a rename of ``old_name``.

    Name similarity scaled by schema compatibility, so an incompatible schema
    reduces any pair to zero regardless of how alike the names are.
    """
    return name_similarity(old_name, new_name) * schema_compatibility(old_schema, new_schema)


def detect_renames(
    removed: dict[str, JsonSchema],
    added: dict[str, JsonSchema],
    *,
    threshold: float = RENAME_SCORE_THRESHOLD,
) -> list[RenameCandidate]:
    """Pair removed names with added names that most likely replace them.

    Args:
        removed: Names that disappeared, mapped to their schemas.
        added: Names that appeared, mapped to their schemas.
        threshold: Minimum score to accept a pair.

    Returns:
        Accepted pairs, highest score first. Each name appears at most once, so
        the result is a partial one-to-one matching.
    """
    scored = sorted(
        (
            RenameCandidate(
                old_name, new_name, score_pair(old_name, old_schema, new_name, new_schema)
            )
            for old_name, old_schema in removed.items()
            for new_name, new_schema in added.items()
        ),
        # Names break score ties so the matching never depends on input ordering.
        key=lambda candidate: (-candidate.score, candidate.old_name, candidate.new_name),
    )

    matched: list[RenameCandidate] = []
    used_old: set[str] = set()
    used_new: set[str] = set()
    for candidate in scored:
        if candidate.score < threshold:
            break
        if candidate.old_name in used_old or candidate.new_name in used_new:
            continue
        matched.append(candidate)
        used_old.add(candidate.old_name)
        used_new.add(candidate.new_name)
    return matched


__all__ = [
    "HIGH_CHARACTER_SIMILARITY",
    "RENAME_SCORE_THRESHOLD",
    "RenameCandidate",
    "detect_renames",
    "name_similarity",
    "schema_compatibility",
    "score_pair",
    "tokenize",
]
