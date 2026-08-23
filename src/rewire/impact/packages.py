"""Working out which Python package an API specification corresponds to.

An OpenAPI document describes an HTTP surface; it says nothing about the SDK a
repository uses to reach it. But "does this file import the package?" is one of
the strongest impact signals available, so the link has to be established
somehow.

The inference is deliberately conservative: title tokens are matched against
names the repository *actually* uses -- its declared dependencies and its
imported modules -- rather than against a guess. An unmatched title yields no
candidates, and the package signals are then simply absent. That matters:
absence of evidence must not become evidence against, or every repository using
a client library Rewire failed to name would score its real call sites as
unaffected.

Callers who know the answer should pass it explicitly; the CLI exposes
``--package`` for exactly that.
"""

from __future__ import annotations

import re
from typing import Final

from rewire.analyzers.models import RepositoryIndex

#: Words that appear in almost every API title and identify nothing.
TITLE_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "api",
        "apis",
        "beta",
        "cloud",
        "http",
        "internal",
        "openapi",
        "platform",
        "public",
        "rest",
        "sdk",
        "server",
        "service",
        "services",
        "spec",
        "specification",
        "the",
        "v1",
        "v2",
        "v3",
        "web",
    }
)

#: Shortest token considered; one- and two-letter fragments match everything.
MIN_TOKEN_LENGTH: Final[int] = 3

_TOKEN = re.compile(r"[^a-z0-9]+")


def title_tokens(title: str | None) -> list[str]:
    """Split a specification title into candidate package-name tokens."""
    if not title:
        return []
    tokens = [token for token in _TOKEN.split(title.lower()) if token]
    return [
        token for token in tokens if len(token) >= MIN_TOKEN_LENGTH and token not in TITLE_STOPWORDS
    ]


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def infer_packages(title: str | None, index: RepositoryIndex) -> list[str]:
    """Infer which packages in ``index`` an API titled ``title`` belongs to.

    A token matches a name when the two are equal after normalisation, or when
    one contains the other as a whole segment -- enough to link ``openai`` to
    ``openai`` and to ``openai-python``, without linking ``github`` to
    ``pygithub``, which is a real miss rather than a guess.

    Returns:
        Matching top-level module names, sorted. Empty when nothing matched, in
        which case package-based signals should be omitted entirely.
    """
    tokens = set(title_tokens(title))
    if not tokens:
        return []

    known = {module: _normalise(module) for module in index.imported_modules()} | {
        dependency.name: dependency.normalised_name for dependency in index.dependencies
    }

    matched = {
        name
        for name, normalised in known.items()
        if any(_matches(token, normalised) for token in tokens)
    }
    # Prefer names that are actually imported: a declared-but-unused dependency
    # cannot be the source of a call site.
    imported = set(index.imported_modules())
    return sorted(matched & imported) or sorted(matched)


def _matches(token: str, normalised_name: str) -> bool:
    if token == normalised_name:
        return True
    segments = normalised_name.split("-")
    return token in segments


def resolve_packages(
    title: str | None, index: RepositoryIndex, *, explicit: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Return the packages to attribute a change to.

    An explicit choice is always honoured as given, even when the repository
    does not appear to import it -- the caller may know something the index
    cannot see, and silently overriding them would be worse than a wrong answer
    they can observe.
    """
    if explicit:
        return tuple(dict.fromkeys(explicit))
    return tuple(infer_packages(title, index))


__all__ = [
    "MIN_TOKEN_LENGTH",
    "TITLE_STOPWORDS",
    "infer_packages",
    "resolve_packages",
    "title_tokens",
]
