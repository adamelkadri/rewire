"""Tests for extracting structured facts from Python source."""

from __future__ import annotations

import pytest

from rewire.analyzers.models import ReferenceKind, SymbolKind
from rewire.analyzers.python_ast import MAX_STRING_REFERENCE_LENGTH, analyse_source


def analyse(source: str, *, path: str = "app/mod.py", module: str = "app.mod"):
    return analyse_source(source.encode("utf-8"), path=path, module=module)


def calls(source: str) -> dict[str, str | None]:
    """Map each written callee to what it resolved to."""
    return {call.callee: call.resolved_callee for call in analyse(source).calls}


# ------------------------------------------------------------------ imports ---


def test_plain_import() -> None:
    record = analyse("import openai").imports[0]
    assert (record.module, record.name, record.local_name) == ("openai", None, "openai")
    assert record.qualified_name == "openai"
    assert not record.is_relative


def test_dotted_import_binds_the_root_name() -> None:
    record = analyse("import openai.types").imports[0]
    assert record.local_name == "openai"


def test_aliased_import() -> None:
    record = analyse("import openai as oai").imports[0]
    assert record.local_name == "oai"
    assert record.module == "openai"


def test_from_import() -> None:
    record = analyse("from openai import OpenAI").imports[0]
    assert record.qualified_name == "openai.OpenAI"
    assert record.local_name == "OpenAI"


def test_aliased_from_import() -> None:
    record = analyse("from openai import OpenAI as Client").imports[0]
    assert record.local_name == "Client"
    assert record.qualified_name == "openai.OpenAI"


def test_multiple_names_produce_one_record_each() -> None:
    records = analyse("from openai import OpenAI, AsyncOpenAI").imports
    assert [record.name for record in records] == ["OpenAI", "AsyncOpenAI"]


def test_relative_import_is_recorded_but_not_resolved() -> None:
    record = analyse("from . import sibling").imports[0]
    assert record.is_relative
    assert record.level == 1
    # Resolving it would require loading the package; a wrong guess is worse.
    assert analyse("from . import sibling\nsibling.run()").calls[0].resolved_callee is None


def test_star_import_is_recorded_and_binds_nothing() -> None:
    info = analyse("from openai import *\nOpenAI()")
    assert info.imports[0].name == "*"
    assert info.calls[0].resolved_callee is None


def test_is_from_matches_submodules() -> None:
    record = analyse("from openai.types import Model").imports[0]
    assert record.is_from("openai")
    assert record.is_from("openai.types")
    assert not record.is_from("openai_other")


# -------------------------------------------------------------------- calls ---


def test_call_resolves_through_an_alias() -> None:
    assert calls("import openai as oai\noai.chat.create()") == {
        "oai.chat.create": "openai.chat.create"
    }


def test_call_resolves_through_a_constructed_client() -> None:
    source = "from openai import OpenAI\nclient = OpenAI()\nclient.chat.completions.create()"
    assert (
        calls(source)["client.chat.completions.create"] == "openai.OpenAI.chat.completions.create"
    )


def test_call_resolves_through_a_chain_of_aliases() -> None:
    source = "from openai import OpenAI as Client\na = Client()\nb = a\nc = b\nc.chat.create()"
    assert calls(source)["c.chat.create"] == "openai.OpenAI.chat.create"


def test_call_resolves_through_an_instance_attribute() -> None:
    """The client is assigned in __init__ and used in a method defined later."""
    source = (
        "from openai import OpenAI\n"
        "class W:\n"
        "    def __init__(self):\n"
        "        self._c = OpenAI()\n"
        "    def go(self):\n"
        "        return self._c.chat.completions.create()\n"
    )
    assert (
        calls(source)["self._c.chat.completions.create"] == "openai.OpenAI.chat.completions.create"
    )


def test_unresolvable_call_keeps_its_literal_callee() -> None:
    """Giving up costs recall, never correctness."""
    call = analyse("helper.run()").calls[0]
    assert call.callee == "helper.run"
    assert call.resolved_callee is None


def test_keyword_arguments_are_captured() -> None:
    call = analyse("f(a, b, x=1, y=2)").calls[0]
    assert call.keywords == ("x", "y")
    assert call.positional_count == 2
    assert not call.has_star_args


def test_star_args_are_flagged() -> None:
    assert analyse("f(**payload)").calls[0].has_star_args
    assert analyse("f(*args)").calls[0].has_star_args


def test_calls_on_non_name_roots_are_skipped() -> None:
    """`get_client().create()` has no traceable root, so it is not a call record."""
    assert [call.callee for call in analyse("get_client().create()").calls] == ["get_client"]


def test_matches_supports_suffix_patterns() -> None:
    source = "from openai import OpenAI\nc = OpenAI()\nc.chat.completions.create()"
    call = next(
        call for call in analyse(source).calls if call.callee.endswith("completions.create")
    )
    assert call.matches("c.chat.completions.create")
    assert call.matches("chat.completions.create")
    assert call.matches("openai.OpenAI.chat.completions.create")
    assert not call.matches("chat.completions.delete")
    assert not call.matches("completions")


# ------------------------------------------------------------------ symbols ---


def test_symbol_kinds() -> None:
    source = (
        "CONST = 1\n"
        "def fn(): ...\n"
        "async def afn(): ...\n"
        "class C:\n"
        "    def m(self): ...\n"
        "    async def am(self): ...\n"
    )
    kinds = {symbol.name: symbol.kind for symbol in analyse(source).symbols}
    assert kinds == {
        "CONST": SymbolKind.VARIABLE,
        "fn": SymbolKind.FUNCTION,
        "afn": SymbolKind.ASYNC_FUNCTION,
        "C": SymbolKind.CLASS,
        "m": SymbolKind.METHOD,
        "am": SymbolKind.ASYNC_METHOD,
    }


def test_qualified_names_nest() -> None:
    source = "class Outer:\n    class Inner:\n        def method(self): ...\n"
    names = {symbol.qualified_name for symbol in analyse(source).symbols}
    assert names == {
        "app.mod.Outer",
        "app.mod.Outer.Inner",
        "app.mod.Outer.Inner.method",
    }


def test_decorators_and_parameters_are_recorded() -> None:
    source = "import functools\n@functools.cache\ndef fn(a, b=1, *rest, c, **kw): ...\n"
    symbol = next(s for s in analyse(source).symbols if s.name == "fn")
    assert symbol.decorators == ("functools.cache",)
    assert symbol.parameters == ("a", "b", "c", "*rest", "**kw")


def test_symbol_line_range_and_containment() -> None:
    symbol = next(s for s in analyse("def fn():\n    x = 1\n    return x\n").symbols)
    assert symbol.line == 1
    assert symbol.end_line == 3
    assert symbol.contains_line(2)
    assert not symbol.contains_line(4)


def test_only_module_level_assignments_become_symbols() -> None:
    source = "TOP = 1\ndef fn():\n    inner = 2\n"
    names = {symbol.name for symbol in analyse(source).symbols}
    assert "TOP" in names
    assert "inner" not in names


def test_is_callable() -> None:
    symbols = {s.name: s for s in analyse("X = 1\ndef fn(): ...\nclass C: ...\n").symbols}
    assert symbols["fn"].is_callable
    assert symbols["C"].is_callable
    assert not symbols["X"].is_callable


def test_calls_record_their_enclosing_symbol() -> None:
    source = "class C:\n    def m(self):\n        helper()\n"
    call = analyse(source).calls[0]
    assert call.enclosing_symbol == "app.mod.C.m"


# --------------------------------------------------------------- references ---


def references(source: str, name: str) -> list[tuple[int, ReferenceKind]]:
    return [
        (reference.line, reference.kind)
        for reference in analyse(source).references
        if reference.name == name
    ]


def test_keyword_argument_reference() -> None:
    assert references("f(max_tokens=1)", "max_tokens") == [(1, ReferenceKind.KEYWORD_ARGUMENT)]


def test_keyword_reference_records_its_call_target() -> None:
    reference = next(
        r for r in analyse("client.create(max_tokens=1)").references if r.name == "max_tokens"
    )
    assert reference.context == "client.create"


def test_dict_key_reference() -> None:
    assert references('{"max_tokens": 1}', "max_tokens") == [(1, ReferenceKind.DICT_KEY)]


def test_subscript_key_reference() -> None:
    reference = next(
        r for r in analyse('payload["max_tokens"]').references if r.name == "max_tokens"
    )
    assert reference.kind is ReferenceKind.SUBSCRIPT_KEY
    assert reference.context == "payload"


def test_parameter_reference() -> None:
    """A wrapper forwarding the field under its own name is part of the surface."""
    reference = next(
        r for r in analyse("def go(max_tokens=1): ...").references if r.name == "max_tokens"
    )
    assert reference.kind is ReferenceKind.PARAMETER
    assert reference.context == "go"


def test_attribute_reference() -> None:
    assert references("response.max_tokens", "max_tokens") == [(1, ReferenceKind.ATTRIBUTE)]


def test_string_literal_reference() -> None:
    assert references('x = "max_tokens"', "max_tokens") == [(1, ReferenceKind.STRING_LITERAL)]


def test_evidence_is_ordered_by_kind() -> None:
    keyword = analyse("f(max_tokens=1)").references[0]
    literal = next(r for r in analyse('"max_tokens"').references if r.name == "max_tokens")
    assert keyword.evidence > literal.evidence


def test_one_reference_per_source_position() -> None:
    """A dict key is also a string constant; counting both would inflate evidence."""
    found = [r for r in analyse('{"max_tokens": 1}').references if r.name == "max_tokens"]
    assert len(found) == 1
    assert found[0].kind is ReferenceKind.DICT_KEY


def test_long_strings_are_not_recorded_as_references() -> None:
    prose = "x" * (MAX_STRING_REFERENCE_LENGTH + 1)
    assert not [r for r in analyse(f'x = "{prose}"').references if r.name == prose]


def test_empty_strings_are_not_recorded() -> None:
    assert not [r for r in analyse('x = ""').references if r.name == ""]


def test_references_are_sorted_by_position() -> None:
    info = analyse("a = 1\nb = 2\nc = 3\n")
    positions = [(r.line, r.column) for r in info.references]
    assert positions == sorted(positions)


# ------------------------------------------------------- environment access ---


def test_env_var_via_subscript() -> None:
    info = analyse('import os\nKEY = os.environ["API_KEY"]')
    assert [(e.name, e.accessor) for e in info.env_vars] == [("API_KEY", "os.environ")]


def test_env_var_via_getenv() -> None:
    info = analyse('import os\nKEY = os.getenv("API_KEY")')
    assert [(e.name, e.accessor) for e in info.env_vars] == [("API_KEY", "os.getenv")]


def test_env_var_via_environ_get() -> None:
    info = analyse('import os\nKEY = os.environ.get("API_KEY", "default")')
    assert [e.name for e in info.env_vars] == ["API_KEY"]


def test_env_var_via_imported_getenv() -> None:
    info = analyse('from os import getenv\nKEY = getenv("API_KEY")')
    assert [e.name for e in info.env_vars] == ["API_KEY"]


def test_env_subscript_is_not_also_a_plain_reference() -> None:
    info = analyse('import os\nos.environ["API_KEY"]')
    assert not [r for r in info.references if r.name == "API_KEY"]


def test_non_literal_env_names_are_skipped() -> None:
    """`os.getenv(name)` names a variable, not a variable name."""
    assert analyse("import os\nos.getenv(name)").env_vars == ()


def test_getenv_without_arguments_is_skipped() -> None:
    assert analyse("import os\nos.getenv()").env_vars == ()


# ---------------------------------------------------------------- resilience ---


def test_syntax_error_is_recorded_not_raised() -> None:
    """A dropped file would let Rewire report 'no usages' for code it never read."""
    info = analyse("def broken(\n")
    assert not info.parsed
    assert "SyntaxError" in (info.parse_error or "")
    assert info.symbols == ()
    assert info.line_count == 2


def test_null_bytes_are_reported_as_a_parse_error() -> None:
    info = analyse_source(b"x = 1\x00\n", path="a.py", module="a")
    assert not info.parsed


def test_encoding_declaration_is_honoured() -> None:
    source = "# -*- coding: latin-1 -*-\nname = 'caf\xe9'\n".encode("latin-1")
    info = analyse_source(source, path="a.py", module="a")
    assert info.parsed


def test_empty_file_parses_to_nothing() -> None:
    info = analyse("")
    assert info.parsed
    assert info.symbols == ()
    assert info.line_count == 0


@pytest.mark.parametrize(
    "source",
    [
        "match x:\n    case 1:\n        pass\n",
        "type Alias = int\n",
        "def f[T](x: T) -> T: ...\n",
        "async def f():\n    async with a as b:\n        pass\n",
        "x = [i async for i in gen()]\n",
    ],
)
def test_modern_syntax_parses(source: str) -> None:
    assert analyse(source).parsed


def test_size_and_line_counts() -> None:
    info = analyse_source(b"a = 1\nb = 2\n", path="a.py", module="a", size_bytes=999)
    assert info.size_bytes == 999
    assert info.line_count == 3


def test_root_name_is_the_first_segment() -> None:
    call = analyse("client.chat.completions.create()").calls[0]
    assert call.root_name == "client"
    assert analyse("run()").calls[0].root_name == "run"


def test_unnameable_decorators_are_skipped() -> None:
    """`@registry["name"]` has no dotted form to record."""
    source = 'registry = {}\n@registry["hook"]\ndef fn(): ...\n'
    symbol = next(s for s in analyse(source).symbols if s.name == "fn")
    assert symbol.decorators == ()


def test_non_string_dict_keys_are_not_references() -> None:
    """A dict may be keyed by ints, or spread with `**other`."""
    info = analyse('{1: "a", **other, "max_tokens": 2}')
    assert [r.name for r in info.references if r.kind is ReferenceKind.DICT_KEY] == ["max_tokens"]
