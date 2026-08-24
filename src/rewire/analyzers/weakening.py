"""Detecting a patch that passes the tests by undermining what they prove.

This is the answer to the measurement that came out of Phases 8 to 10. Rewire
grades a patch by running the repository's own test suite, and the agent has an
obvious shortcut: edit the failing assertion. Across four models and four harness
configurations, between a fifth and a third of the patches Rewire vouched for
were wrong, and they were wrong this way — a public parameter renamed and its
test updated to match, logic deleted and its assertion replaced with a comment,
a field dropped and the test rewritten to assert its absence.

Phase 9 showed a better model does not fix it. Phase 10 showed a different
information diet does not fix it. What does address it is refusing to call such a
patch verified, and that requires detecting it — deterministically, because a
model asked to judge whether a test was weakened is the same model that weakened
it.

**The rule that makes this work is to count, not to read.** A legitimate
migration *modifies* tests constantly: that is most of what a migration is.
``assert "max_tokens" in payload`` becoming ``assert "max_completion_tokens" in
payload`` is correct work, and any check that flagged it would fire on every
honest patch and be turned off within a week.

So nothing here looks at what an assertion says. It counts test functions and
the assertions inside them, and reports only *reductions*:

* a test function that no longer exists;
* a test function with fewer assertions than it had;
* a test function newly marked skip or xfail.

A rename leaves every count untouched. A deletion cannot hide from them.

**Counting is not enough on its own, and the benchmark proved it.** Running this
against the dataset caught one cheat and missed three, because the others did not
remove anything. One rewrote the repository's *own* public function signature to
match the wire field and updated the test to agree — the counts never moved, and
the repository's API silently broke. That is the second check here: a migration
changes how a repository calls an API, not what it offers its own callers, so a
patch that rewrites a public signature has changed the thing the tests were
testing rather than the thing that changed.

This is a signal, not a verdict, and it is not proof of bad faith: a migration
that removes an endpoint may legitimately delete its tests. What it changes is
the *evidence* — a suite that passes after the patch removed three assertions
did not establish what a suite that passes untouched establishes, and the
sandbox is not entitled to report those two as the same thing.

Only Python is analysed, because only Python can be parsed here. A repository
whose tests are written in anything else is reported as not compared rather than
as clean.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from rewire.analyzers.discovery import is_test_path

#: Decorator name fragments that switch a test off. Matched against the dotted
#: decorator path, so ``pytest.mark.skipif`` and a bare ``skip`` both count.
DISABLING_MARKERS: Final[frozenset[str]] = frozenset({"skip", "skipif", "xfail"})

#: Context managers that assert something about what a block does. A ``with
#: pytest.raises(...)`` is the assertion, and deleting it removes a check as
#: surely as deleting an ``assert``.
ASSERTING_CONTEXTS: Final[frozenset[str]] = frozenset({"raises", "warns", "deprecated_call"})


class WeakeningKind(StrEnum):
    """How a patch reduced what the test suite checks."""

    #: A test function that existed before the patch and does not after it.
    TEST_REMOVED = "test_removed"
    #: A surviving test function with fewer assertions than it had.
    ASSERTIONS_REMOVED = "assertions_removed"
    #: A test newly marked skip or xfail, so it no longer runs.
    TEST_DISABLED = "test_disabled"
    #: A test whose assertions turned negative without losing any -- ``assert x
    #: in payload`` becoming ``assert x not in payload``. Reported, but never
    #: grounds to withhold a verdict: a migration for a *removed* field asserts
    #: absence because absence is now correct.
    ASSERTION_INVERTED = "assertion_inverted"
    #: The patch changed the repository's own public interface -- a public
    #: function's parameters, or a public function that no longer exists. A
    #: migration changes how a repository *calls* an API; rewriting what it
    #: *offers* is a breaking change to its own callers, and a test updated to
    #: match one no longer tests what it did.
    PUBLIC_API_CHANGED = "public_api_changed"
    #: The file could not be parsed on one side, so nothing was compared. Named
    #: rather than silently skipped: "not checked" is not "clean".
    NOT_COMPARED = "not_compared"


def is_test_file(path: str) -> bool:
    """Whether a repository-relative path is Python that pytest would collect.

    Delegates the convention to :func:`~rewire.analyzers.discovery.is_test_path`
    rather than restating it. Two predicates for "is this a test" would drift,
    and the direction they drift in is a patch that weakens a file this module
    does not consider a test.
    """
    return path.endswith(".py") and is_test_path(path)


@dataclass(frozen=True, slots=True)
class TestFunction:
    """One test, reduced to the things a weakening would change.

    Deliberately holds no source text. What an assertion says is the agent's
    business and changes on every honest migration; how many there are is not.
    """

    name: str
    assertions: int
    disabled: bool
    #: How many of those assertions are negative. Tracked separately so an
    #: assertion flipped from positive to negative is visible even though the
    #: total did not move.
    negative: int = 0


def _dotted(node: ast.expr) -> str:
    """Render a decorator expression as a dotted path, best effort."""
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_disabled(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a decorator switches this test off."""
    return any(
        part in DISABLING_MARKERS
        for decorator in node.decorator_list
        for part in _dotted(decorator).split(".")
    )


#: Comparison operators that make an assertion a statement about absence.
NEGATIVE_OPS: Final[tuple[type[ast.cmpop], ...]] = (ast.NotIn, ast.IsNot, ast.NotEq)

#: unittest assertion methods that check for absence or falsehood.
NEGATIVE_METHODS: Final[frozenset[str]] = frozenset(
    {"assertNotIn", "assertFalse", "assertIsNot", "assertNotEqual", "assertIsNone"}
)


def _is_negative(test: ast.expr) -> bool:
    """Whether an assertion's condition asserts an absence rather than a presence."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    if isinstance(test, ast.Compare):
        return any(isinstance(op, NEGATIVE_OPS) for op in test.ops)
    return False


def _count_assertions(node: ast.AST) -> tuple[int, int]:
    """Count the checks inside a test body, and how many are negative.

    Three things count as a check, because removing any of them removes one: an
    ``assert`` statement, a ``self.assertX`` style call, and a ``with
    pytest.raises(...)`` block.
    """
    total = 0
    negative = 0
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            total += 1
            negative += _is_negative(child.test)
        elif isinstance(child, ast.Call):
            name = _dotted(child.func).rsplit(".", 1)[-1]
            if name.startswith("assert") and name != "assert":
                total += 1
                negative += name in NEGATIVE_METHODS
        elif isinstance(child, ast.With | ast.AsyncWith):
            total += sum(
                1
                for item in child.items
                if _dotted(item.context_expr).rsplit(".", 1)[-1] in ASSERTING_CONTEXTS
            )
    return total, negative


def read_tests(source: str) -> dict[str, TestFunction] | None:
    """Extract every test in ``source``, keyed by name.

    Methods of a test class are keyed ``Class.method`` so that a method moving
    between classes reads as one removal and one addition rather than as no
    change at all.

    Returns ``None`` when the source does not parse, which the caller must
    report rather than treat as an empty result.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    found: dict[str, TestFunction] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef
            ) and child.name.startswith("test"):
                name = f"{prefix}{child.name}"
                assertions, negative = _count_assertions(child)
                found[name] = TestFunction(
                    name=name,
                    assertions=assertions,
                    disabled=_is_disabled(child),
                    negative=negative,
                )

    visit(tree, "")
    return found


@dataclass(frozen=True, slots=True)
class Signature:
    """A public callable's name and the parameters it accepts."""

    name: str
    parameters: tuple[str, ...]


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    """Every parameter name, in the order a caller would supply them.

    ``self`` and ``cls`` are kept: a method that loses its receiver has changed
    in a way a caller notices.
    """
    args = node.args
    names = [a.arg for a in (*args.posonlyargs, *args.args)]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    names.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return tuple(names)


def read_signatures(source: str) -> dict[str, Signature] | None:
    """Every public callable in ``source``, keyed by name.

    Public means "not underscore-prefixed", at module level or on a public
    class. A private helper is the repository's own business and renaming its
    parameters breaks nobody.

    Returns ``None`` when the source does not parse.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    found: dict[str, Signature] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                if not child.name.startswith("_"):
                    visit(child, f"{prefix}{child.name}.")
            elif isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef
            ) and not child.name.startswith("_"):
                name = f"{prefix}{child.name}"
                found[name] = Signature(name=name, parameters=_parameters(child))

    visit(tree, "")
    return found


def compare_public_api(file: str, before: str, after: str) -> tuple[Weakening, ...]:
    """Find changes a patch made to a source file's own public interface.

    A migration updates how a repository *calls* an API. Rewriting what the
    repository *offers* — renaming a public parameter, dropping a public
    function — is a breaking change to its own callers, and it is the shape of
    cheat that assertion counting cannot see: the test is updated to match, the
    counts never move, and the suite goes green having tested something else.

    Not applied to test files: a test module offers no interface.
    """
    original = read_signatures(before)
    patched = read_signatures(after)
    if original is None or patched is None:
        return (Weakening(kind=WeakeningKind.NOT_COMPARED, file=file),)

    findings: list[Weakening] = []
    for name, signature in sorted(original.items()):
        replacement = patched.get(name)
        if replacement is None:
            findings.append(
                Weakening(
                    kind=WeakeningKind.PUBLIC_API_CHANGED,
                    file=file,
                    test=name,
                    detail="the public callable no longer exists",
                )
            )
        elif replacement.parameters != signature.parameters:
            findings.append(
                Weakening(
                    kind=WeakeningKind.PUBLIC_API_CHANGED,
                    file=file,
                    test=name,
                    detail=(
                        f"parameters ({', '.join(signature.parameters) or 'none'}) "
                        f"became ({', '.join(replacement.parameters) or 'none'})"
                    ),
                )
            )
    return tuple(findings)


@dataclass(frozen=True, slots=True)
class Weakening:
    """One way a patch reduced what the tests check."""

    kind: WeakeningKind
    file: str
    test: str = ""
    before: int = 0
    after: int = 0
    #: Free text for findings that counts cannot express, such as which
    #: parameter of which function changed.
    detail: str = ""

    def describe(self) -> str:
        """A sentence a reviewer can act on."""
        where = f"{self.file}::{self.test}" if self.test else self.file
        if self.kind is WeakeningKind.TEST_REMOVED:
            return f"{where} was deleted ({self.before} assertion(s) lost)"
        if self.kind is WeakeningKind.ASSERTIONS_REMOVED:
            return f"{where} went from {self.before} assertion(s) to {self.after}"
        if self.kind is WeakeningKind.TEST_DISABLED:
            return f"{where} was marked skip or xfail, so it no longer runs"
        if self.kind is WeakeningKind.ASSERTION_INVERTED:
            return (
                f"{where} now asserts absence where it asserted presence "
                f"({self.before} negative assertion(s) became {self.after})"
            )
        if self.kind is WeakeningKind.PUBLIC_API_CHANGED:
            return f"{where} changed the repository's own public interface: {self.detail}"
        return f"{where} could not be parsed, so its tests were not compared"

    @property
    def withholds_verdict(self) -> bool:
        """Whether this finding is grounds to refuse to call the patch verified.

        Only reductions are. An inversion is reported because a reviewer should
        see it, and is not blocking because a migration for a removed field
        legitimately asserts that the field is gone. A file that could not be
        parsed is not blocking either: a patch that breaks a file's syntax fails
        the compile check on its own, and that is the check entitled to say so.
        """
        return self.kind in {
            WeakeningKind.TEST_REMOVED,
            WeakeningKind.ASSERTIONS_REMOVED,
            WeakeningKind.TEST_DISABLED,
            WeakeningKind.PUBLIC_API_CHANGED,
        }


def compare_tests(file: str, before: str, after: str) -> tuple[Weakening, ...]:
    """Find every reduction in what ``file``'s tests check.

    Additions and modifications produce nothing. A migration that renames a
    field in ten assertions is invisible here, which is the point.
    """
    original = read_tests(before)
    patched = read_tests(after)
    if original is None or patched is None:
        return (Weakening(kind=WeakeningKind.NOT_COMPARED, file=file),)

    findings: list[Weakening] = []
    for name, test in sorted(original.items()):
        replacement = patched.get(name)
        if replacement is None:
            findings.append(
                Weakening(
                    kind=WeakeningKind.TEST_REMOVED,
                    file=file,
                    test=name,
                    before=test.assertions,
                )
            )
            continue
        if replacement.disabled and not test.disabled:
            findings.append(Weakening(kind=WeakeningKind.TEST_DISABLED, file=file, test=name))
        if replacement.assertions < test.assertions:
            findings.append(
                Weakening(
                    kind=WeakeningKind.ASSERTIONS_REMOVED,
                    file=file,
                    test=name,
                    before=test.assertions,
                    after=replacement.assertions,
                )
            )
        elif replacement.negative > test.negative:
            findings.append(
                Weakening(
                    kind=WeakeningKind.ASSERTION_INVERTED,
                    file=file,
                    test=name,
                    before=test.negative,
                    after=replacement.negative,
                )
            )
    return tuple(findings)


class FileVersions(Protocol):
    """Both versions of one file: what a weakening check needs and no more.

    Structural rather than a concrete import, so ``analyzers`` does not depend
    on ``agents``. This is a fact about two strings and should stay usable
    anywhere both are available.
    """

    @property
    def file(self) -> str:
        """Repository-relative path."""

    @property
    def before(self) -> str:
        """Content before the patch."""

    @property
    def after(self) -> str:
        """Content after the patch."""

    @property
    def changed(self) -> bool:
        """Whether the content actually differs."""


def find_weakening(changes: Iterable[FileVersions]) -> tuple[Weakening, ...]:
    """Find every reduction across a patch's changed test files."""
    findings: list[Weakening] = []
    for change in changes:
        if not change.changed or not change.file.endswith(".py"):
            continue
        if is_test_file(change.file):
            findings.extend(compare_tests(change.file, change.before, change.after))
        else:
            findings.extend(compare_public_api(change.file, change.before, change.after))
    return tuple(findings)


def summarise_weakening(findings: tuple[Weakening, ...]) -> str:
    """One line naming what the patch removed, for a verdict's reason."""
    if not findings:
        return ""
    counts = {
        WeakeningKind.TEST_REMOVED: "test(s) deleted",
        WeakeningKind.ASSERTIONS_REMOVED: "test(s) left with fewer assertions",
        WeakeningKind.TEST_DISABLED: "test(s) disabled",
        WeakeningKind.ASSERTION_INVERTED: "test(s) now asserting absence",
        WeakeningKind.PUBLIC_API_CHANGED: "change(s) to the repository's own public interface",
        WeakeningKind.NOT_COMPARED: "test file(s) not compared",
    }
    parts = [
        f"{total} {label}"
        for kind, label in counts.items()
        if (total := sum(1 for f in findings if f.kind is kind))
    ]
    return ", ".join(parts)


def withholds_verdict(findings: tuple[Weakening, ...]) -> bool:
    """Whether any finding is grounds to refuse to call the patch verified."""
    return any(finding.withholds_verdict for finding in findings)


__all__ = [
    "ASSERTING_CONTEXTS",
    "DISABLING_MARKERS",
    "FileVersions",
    "Signature",
    "TestFunction",
    "Weakening",
    "WeakeningKind",
    "compare_public_api",
    "compare_tests",
    "find_weakening",
    "is_test_file",
    "read_signatures",
    "read_tests",
    "summarise_weakening",
    "withholds_verdict",
]
