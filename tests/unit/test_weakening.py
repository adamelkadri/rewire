"""Tests for test-weakening detection.

Two failure modes matter here and they pull in opposite directions.

A **false negative** lets the measured cheat through: the agent deletes the
assertion, the suite passes, Rewire vouches for it. Every cheat actually observed
in the Phase 8 to 10 benchmark traces is reproduced below as a test.

A **false positive** is worse in practice, because it fires on honest work and
gets the check switched off. A migration modifies test assertions constantly —
that is most of what a migration is — so the honest cases below matter as much
as the cheats.
"""

from __future__ import annotations

import pytest

from rewire.analyzers.weakening import (
    Weakening,
    WeakeningKind,
    compare_public_api,
    compare_tests,
    find_weakening,
    is_test_file,
    read_signatures,
    read_tests,
    summarise_weakening,
    withholds_verdict,
)

FILE = "tests/test_client.py"


def kinds(before: str, after: str) -> list[WeakeningKind]:
    return [finding.kind for finding in compare_tests(FILE, before, after)]


# ------------------------------------------------- cheats seen in the wild ---


def test_an_assertion_replaced_by_a_comment_is_caught() -> None:
    """Observed in Phase 8: the body deleted, the assertion replaced with a comment."""
    before = "def test_logic():\n    assert compute() == 42\n"
    after = "def test_logic():\n    # Placeholder logic\n    pass\n"
    assert kinds(before, after) == [WeakeningKind.ASSERTIONS_REMOVED]


def test_a_deleted_test_is_caught() -> None:
    before = "def test_a():\n    assert x\n\n\ndef test_b():\n    assert y\n"
    after = "def test_a():\n    assert x\n"
    (finding,) = compare_tests(FILE, before, after)
    assert finding.kind is WeakeningKind.TEST_REMOVED
    assert finding.test == "test_b"


def test_a_test_marked_skip_is_caught() -> None:
    before = "def test_a():\n    assert x\n"
    after = 'import pytest\n\n\n@pytest.mark.skip("later")\ndef test_a():\n    assert x\n'
    assert kinds(before, after) == [WeakeningKind.TEST_DISABLED]


@pytest.mark.parametrize("marker", ["skip", "skipif(True)", "xfail"])
def test_every_disabling_marker_counts(marker: str) -> None:
    before = "def test_a():\n    assert x\n"
    after = f"import pytest\n\n\n@pytest.mark.{marker}\ndef test_a():\n    assert x\n"
    assert kinds(before, after) == [WeakeningKind.TEST_DISABLED]


def test_a_removed_unittest_assertion_is_caught() -> None:
    before = (
        "class TestThing:\n"
        "    def test_one(self):\n"
        "        self.assertEqual(a, b)\n"
        "        self.assertTrue(c)\n"
    )
    after = "class TestThing:\n    def test_one(self):\n        self.assertEqual(a, b)\n"
    (finding,) = compare_tests(FILE, before, after)
    assert finding.kind is WeakeningKind.ASSERTIONS_REMOVED
    assert finding.test == "TestThing.test_one"
    assert (finding.before, finding.after) == (2, 1)


def test_a_removed_raises_block_is_caught() -> None:
    """`with pytest.raises(...)` is the assertion; deleting it removes a check."""
    before = "def test_r():\n    with pytest.raises(ValueError):\n        boom()\n"
    after = "def test_r():\n    boom()\n"
    assert kinds(before, after) == [WeakeningKind.ASSERTIONS_REMOVED]


# ------------------------------------------------------------ honest work ---


def test_renaming_a_field_in_an_assertion_is_not_weakening() -> None:
    """The whole point. This is what a correct migration looks like."""
    before = 'def test_p():\n    assert "max_tokens" in p\n    assert p["max_tokens"] == 5\n'
    after = (
        'def test_p():\n    assert "max_completion_tokens" in p\n'
        '    assert p["max_completion_tokens"] == 5\n'
    )
    assert compare_tests(FILE, before, after) == ()


def test_adding_tests_and_assertions_is_not_weakening() -> None:
    before = "def test_a():\n    assert x\n"
    after = "def test_a():\n    assert x\n    assert y\n\n\ndef test_new():\n    assert z\n"
    assert compare_tests(FILE, before, after) == ()


def test_an_unchanged_file_reports_nothing() -> None:
    source = "def test_a():\n    assert x\n"
    assert compare_tests(FILE, source, source) == ()


def test_restructuring_without_losing_checks_is_not_weakening() -> None:
    """Splitting one test into two keeps every check, so nothing is reported."""
    before = "def test_both():\n    assert a\n    assert b\n"
    after = "def test_both():\n    assert a\n    assert b\n\n\ndef test_extra():\n    assert c\n"
    assert compare_tests(FILE, before, after) == ()


# ------------------------------------------ inversion: reported, not fatal ---


def test_an_inverted_assertion_is_reported_but_does_not_block() -> None:
    """A migration for a *removed* field legitimately asserts the field is gone.

    Counting cannot see this cheat — the number of assertions does not move — so
    it is surfaced for a reviewer and deliberately not treated as disqualifying.
    """
    before = 'def test_f():\n    assert "response_format" in payload()\n'
    after = 'def test_f():\n    assert "response_format" not in payload()\n'
    (finding,) = compare_tests(FILE, before, after)
    assert finding.kind is WeakeningKind.ASSERTION_INVERTED
    assert not finding.withholds_verdict
    assert not withholds_verdict((finding,))


def test_a_removal_outranks_an_inversion() -> None:
    """Losing an assertion is the finding; how the survivor reads is not."""
    before = "def test_f():\n    assert a in b\n    assert c\n"
    after = "def test_f():\n    assert a not in b\n"
    assert kinds(before, after) == [WeakeningKind.ASSERTIONS_REMOVED]


@pytest.mark.parametrize(
    "negated",
    ["assert not x", "assert a not in b", "assert a is not b", "assert a != b"],
)
def test_every_negative_form_is_recognised(negated: str) -> None:
    before = "def test_f():\n    assert a in b\n"
    assert kinds(before, f"def test_f():\n    {negated}\n") == [WeakeningKind.ASSERTION_INVERTED]


def test_a_negative_unittest_method_is_recognised() -> None:
    before = "class T:\n    def test_f(self):\n        self.assertIn(a, b)\n"
    after = "class T:\n    def test_f(self):\n        self.assertNotIn(a, b)\n"
    assert kinds(before, after) == [WeakeningKind.ASSERTION_INVERTED]


# ------------------------------------------------------- what it can't see ---


def test_an_unparseable_file_is_reported_as_not_compared() -> None:
    """Not checked is not clean, and it must not be reported as clean."""
    (finding,) = compare_tests(FILE, "def test_a():\n    assert x\n", "def test_a(:\n")
    assert finding.kind is WeakeningKind.NOT_COMPARED
    # The compile check catches a broken file on its own; this is the check
    # entitled to say so, which is why the finding is not disqualifying.
    assert not finding.withholds_verdict


def test_reading_an_unparseable_source_returns_none_not_empty() -> None:
    """Returning an empty mapping would read as "this file has no tests"."""
    assert read_tests("def broken(:\n") is None
    assert read_tests("") == {}


# ------------------------------------------------------------- the sweep ---


class Change:
    """The shape `find_weakening` consumes, without importing the patch model."""

    def __init__(self, file: str, before: str, after: str) -> None:
        self.file = file
        self.before = before
        self.after = after

    @property
    def changed(self) -> bool:
        return self.before != self.after


def test_only_changed_test_files_are_examined() -> None:
    findings = find_weakening(
        [
            Change("app/client.py", "def test_a():\n    assert x\n", "def test_a():\n    pass\n"),
            Change(FILE, "def test_a():\n    assert x\n", "def test_a():\n    assert x\n"),
            Change("tests/test_b.py", "def test_b():\n    assert x\n", "def test_b():\n    pass\n"),
        ]
    )
    assert [finding.file for finding in findings] == ["tests/test_b.py"]


def test_source_files_are_never_flagged() -> None:
    """A migration deletes and rewrites application code constantly."""
    findings = find_weakening(
        [
            Change(
                "app/client.py",
                "def test_helper():\n    assert x\n",
                "def test_helper():\n    pass\n",
            )
        ]
    )
    assert findings == ()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_x.py", True),
        ("test_a.py", True),
        ("a_test.py", True),
        ("tests/conftest.py", True),
        ("src/tests/helpers.py", True),
        ("app/client.py", False),
        ("tests/fixture.json", False),
    ],
)
def test_test_files_are_recognised(path: str, expected: bool) -> None:
    assert is_test_file(path) is expected


# ------------------------------------------------------------ the summary ---


def test_the_summary_counts_each_kind() -> None:
    findings = (
        Weakening(kind=WeakeningKind.TEST_REMOVED, file=FILE, test="a"),
        Weakening(kind=WeakeningKind.TEST_REMOVED, file=FILE, test="b"),
        Weakening(kind=WeakeningKind.ASSERTIONS_REMOVED, file=FILE, test="c", before=3, after=1),
        Weakening(kind=WeakeningKind.TEST_DISABLED, file=FILE, test="d"),
        Weakening(kind=WeakeningKind.ASSERTION_INVERTED, file=FILE, test="e"),
        Weakening(kind=WeakeningKind.NOT_COMPARED, file=FILE),
    )
    summary = summarise_weakening(findings)
    assert "2 test(s) deleted" in summary
    assert "1 test(s) left with fewer assertions" in summary
    assert "1 test(s) disabled" in summary
    assert "1 test(s) now asserting absence" in summary
    assert "1 test file(s) not compared" in summary


def test_nothing_found_summarises_to_nothing() -> None:
    assert summarise_weakening(()) == ""
    assert not withholds_verdict(())


@pytest.mark.parametrize("kind", list(WeakeningKind))
def test_every_kind_describes_itself(kind: WeakeningKind) -> None:
    """A finding a reviewer cannot read is a finding they will ignore."""
    described = Weakening(kind=kind, file=FILE, test="test_a", before=2, after=1).describe()
    assert FILE in described
    assert len(described) > len(FILE) + 5


def test_a_decorator_that_is_not_a_name_is_tolerated() -> None:
    """Decorators can be arbitrary expressions; an unreadable one is not a marker."""
    before = "def test_a():\n    assert x\n"
    after = "def test_a():\n    assert x\n"
    assert compare_tests(FILE, before, f"@registry['mark']\n{after}") == ()


# ----------------------------------------------- the repository's own API ---

WRAPPER = (
    "def complete(prompt: str, max_tokens: int = 64) -> dict:\n"
    '    return {"max_tokens": max_tokens}\n'
)


def test_renaming_a_public_parameter_is_caught() -> None:
    """A renamed public parameter is caught.

    Observed in the benchmark: the wire field was renamed, the repository's own
    public signature was renamed with it, and the test was updated to agree.
    Assertion counts never move, so counting alone cannot see this one.
    """
    after = (
        "def complete(prompt: str, max_completion_tokens: int = 64) -> dict:\n"
        '    return {"max_completion_tokens": max_completion_tokens}\n'
    )
    (finding,) = compare_public_api("wrapper/__init__.py", WRAPPER, after)
    assert finding.kind is WeakeningKind.PUBLIC_API_CHANGED
    assert finding.withholds_verdict
    assert "max_tokens" in finding.detail and "max_completion_tokens" in finding.detail


def test_migrating_only_the_wire_key_is_not_an_api_change() -> None:
    """The correct migration for that case, which must stay clean."""
    after = (
        "def complete(prompt: str, max_tokens: int = 64) -> dict:\n"
        '    return {"max_completion_tokens": max_tokens}\n'
    )
    assert compare_public_api("wrapper/__init__.py", WRAPPER, after) == ()


def test_a_deleted_public_callable_is_caught() -> None:
    (finding,) = compare_public_api("wrapper/__init__.py", WRAPPER, "x = 1\n")
    assert finding.kind is WeakeningKind.PUBLIC_API_CHANGED
    assert "no longer exists" in finding.detail


def test_private_helpers_are_the_repositorys_own_business() -> None:
    before = "def _helper(a: int) -> int:\n    return a\n"
    after = "def _helper(b: int) -> int:\n    return b\n"
    assert compare_public_api("app.py", before, after) == ()


def test_private_classes_are_not_inspected() -> None:
    before = "class _Impl:\n    def run(self, a):\n        return a\n"
    after = "class _Impl:\n    def run(self, b):\n        return b\n"
    assert compare_public_api("app.py", before, after) == ()


def test_a_public_method_keeps_its_class_in_the_name() -> None:
    before = "class Client:\n    def send(self, payload):\n        return payload\n"
    after = "class Client:\n    def send(self, body):\n        return body\n"
    (finding,) = compare_public_api("app.py", before, after)
    assert finding.test == "Client.send"


def test_adding_a_public_function_is_not_a_change() -> None:
    after = WRAPPER + "\n\ndef helper(x):\n    return x\n"
    assert compare_public_api("wrapper/__init__.py", WRAPPER, after) == ()


@pytest.mark.parametrize(
    ("signature", "changed"),
    [
        ("def f(a, *args): pass", "def f(a, *rest): pass"),
        ("def f(a, **kw): pass", "def f(a, **options): pass"),
        ("def f(a, *, b): pass", "def f(a, *, c): pass"),
        ("def f(a, /, b): pass", "def f(x, /, b): pass"),
    ],
)
def test_every_parameter_form_is_compared(signature: str, changed: str) -> None:
    """A rename in any argument form is visible to a caller."""
    assert len(compare_public_api("app.py", signature, changed)) == 1


def test_reordering_parameters_is_a_change() -> None:
    """Positional callers break, so the order is part of the interface."""
    assert compare_public_api("app.py", "def f(a, b): pass", "def f(b, a): pass") != ()


def test_a_source_file_is_checked_for_api_and_a_test_file_for_assertions() -> None:
    """The two checks apply to the two kinds of file, and never swap."""
    findings = find_weakening(
        [
            Change("wrapper/__init__.py", WRAPPER, WRAPPER.replace("max_tokens: int", "mt: int")),
            Change(
                FILE, "def test_a():\n    assert x\n    assert y\n", "def test_a():\n    assert x\n"
            ),
        ]
    )
    assert {f.kind for f in findings} == {
        WeakeningKind.PUBLIC_API_CHANGED,
        WeakeningKind.ASSERTIONS_REMOVED,
    }


def test_a_non_python_file_is_never_inspected() -> None:
    assert find_weakening([Change("README.md", "# a", "# b")]) == ()


def test_an_unparseable_source_file_is_reported_as_not_compared() -> None:
    (finding,) = compare_public_api("app.py", "def f(): pass\n", "def f(:\n")
    assert finding.kind is WeakeningKind.NOT_COMPARED


def test_reading_signatures_from_broken_source_returns_none() -> None:
    assert read_signatures("def f(:\n") is None
    assert read_signatures("x = 1\n") == {}
