"""Tests for name binding and attribute-chain resolution."""

from __future__ import annotations

import ast

import pytest

from rewire.analyzers.bindings import BindingTable, attribute_chain


def expression(source: str) -> ast.expr:
    parsed = ast.parse(source, mode="eval")
    return parsed.body


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a", ["a"]),
        ("a.b", ["a", "b"]),
        ("a.b.c.d", ["a", "b", "c", "d"]),
        ("self.client.chat", ["self", "client", "chat"]),
    ],
)
def test_attribute_chain_flattens_dotted_names(source: str, expected: list[str]) -> None:
    assert attribute_chain(expression(source)) == expected


@pytest.mark.parametrize("source", ["f().b", "xs[0].b", "(a + b).c", "'literal'.upper"])
def test_attribute_chain_gives_up_on_non_name_roots(source: str) -> None:
    """Inventing a name for an unnameable root would produce false matches."""
    assert attribute_chain(expression(source)) is None


def test_lookup_finds_innermost_binding() -> None:
    table = BindingTable()
    table.bind("x", "outer.X")
    table.push_scope()
    table.bind("x", "inner.X")
    assert table.lookup("x") == "inner.X"
    table.pop_scope()
    assert table.lookup("x") == "outer.X"


def test_unknown_names_resolve_to_none() -> None:
    assert BindingTable().lookup("nothing") is None


def test_popping_the_root_scope_is_a_no_op() -> None:
    table = BindingTable()
    table.bind("x", "a.X")
    table.pop_scope()
    table.pop_scope()
    assert table.lookup("x") == "a.X"


def test_resolve_chain_rewrites_through_a_binding() -> None:
    table = BindingTable()
    table.bind("client", "openai.OpenAI")
    assert table.resolve_chain(["client", "chat", "create"]) == "openai.OpenAI.chat.create"


def test_resolve_chain_returns_none_for_untracked_roots() -> None:
    assert BindingTable().resolve_chain(["helper", "run"]) is None
    assert BindingTable().resolve_chain([]) is None


def test_resolve_chain_follows_self_attributes() -> None:
    table = BindingTable()
    table.bind_class_attribute("app.Wrapper", "_client", "openai.OpenAI")
    table.push_class("app.Wrapper")
    assert table.resolve_chain(["self", "_client", "create"]) == "openai.OpenAI.create"


def test_self_attributes_are_scoped_to_their_class() -> None:
    table = BindingTable()
    table.bind_class_attribute("app.A", "client", "openai.OpenAI")
    table.push_class("app.B")
    assert table.resolve_chain(["self", "client", "create"]) is None


def test_bare_self_resolves_to_nothing() -> None:
    table = BindingTable()
    table.push_class("app.A")
    assert table.resolve_chain(["self"]) is None


def test_current_class_tracks_the_stack() -> None:
    table = BindingTable()
    assert table.current_class is None
    table.push_class("app.A")
    table.push_class("app.A.B")
    assert table.current_class == "app.A.B"
    table.pop_class()
    assert table.current_class == "app.A"
    table.pop_class()
    table.pop_class()
    assert table.current_class is None


def test_resolve_expression_unwraps_constructor_calls() -> None:
    table = BindingTable()
    table.bind("OpenAI", "openai.OpenAI")
    assert table.resolve_expression(expression("OpenAI()")) == "openai.OpenAI"
    assert table.resolve_expression(expression("OpenAI(api_key='x')")) == "openai.OpenAI"


def test_resolve_expression_follows_plain_aliases() -> None:
    table = BindingTable()
    table.bind("client", "openai.OpenAI")
    assert table.resolve_expression(expression("client")) == "openai.OpenAI"


def test_resolve_expression_gives_up_on_unresolvable_values() -> None:
    assert BindingTable().resolve_expression(expression("[1, 2, 3]")) is None
    assert BindingTable().resolve_expression(expression("f()[0]")) is None
