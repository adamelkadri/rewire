"""Resolving local names back to the library they came from.

Finding calls to an SDK is not a text-matching problem. The same call appears as
``client.chat.completions.create``, ``oai.chat.completions.create`` or
``self._client.chat.completions.create`` depending on what the author named
things, and a grep for any one spelling misses the others.

This module tracks what each local name is bound to, so a chain can be rewritten
into its library-qualified form::

    from openai import OpenAI as Client   # Client -> openai.OpenAI
    client = Client()                     # client -> openai.OpenAI
    c = client                            # c      -> openai.OpenAI
    c.chat.completions.create(...)        # resolves to
                                          # openai.OpenAI.chat.completions.create

Resolution is intentionally shallow. It follows imports, direct assignments,
aliasing and instance attributes assigned on ``self`` — the patterns that
actually appear in client code — and gives up rather than guessing on anything
requiring real type inference. An unresolved call is still recorded with its
literal callee, so giving up costs recall, not correctness.
"""

from __future__ import annotations

import ast
from typing import Final

#: Name that binds a method's instance attributes, by convention.
SELF: Final[str] = "self"


def attribute_chain(node: ast.expr) -> list[str] | None:
    """Flatten an attribute expression into its dotted parts.

    ``a.b.c`` yields ``["a", "b", "c"]``. Returns ``None`` for anything whose
    root is not a plain name — ``f().b`` or ``xs[0].b`` cannot be traced to a
    binding, and inventing a name for them would produce false matches.
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    parts.reverse()
    return parts


class BindingTable:
    """A lexical scope stack mapping local names to qualified origins.

    Bindings are last-write-wins within a scope. A name reassigned to something
    else part-way through a function will resolve to whichever assignment the
    walk saw most recently, which is right for the overwhelmingly common case of
    a client constructed once and used throughout.
    """

    def __init__(self) -> None:
        self._scopes: list[dict[str, str]] = [{}]
        #: Instance attributes assigned via ``self.x = ...``, keyed by the
        #: qualified name of the owning class.
        self._class_attributes: dict[str, dict[str, str]] = {}
        self._class_stack: list[str] = []

    # ------------------------------------------------------------- scopes ---

    def push_scope(self) -> None:
        """Enter a nested lexical scope."""
        self._scopes.append({})

    def pop_scope(self) -> None:
        """Leave the innermost scope, discarding its bindings."""
        if len(self._scopes) > 1:
            self._scopes.pop()

    def push_class(self, qualified_name: str) -> None:
        """Enter a class body, so ``self`` resolves against its attributes."""
        self._class_stack.append(qualified_name)
        self._class_attributes.setdefault(qualified_name, {})

    def pop_class(self) -> None:
        """Leave the innermost class body."""
        if self._class_stack:
            self._class_stack.pop()

    @property
    def current_class(self) -> str | None:
        """Qualified name of the class currently being walked, if any."""
        return self._class_stack[-1] if self._class_stack else None

    # ----------------------------------------------------------- bindings ---

    def bind(self, name: str, qualified: str) -> None:
        """Bind ``name`` to ``qualified`` in the innermost scope."""
        self._scopes[-1][name] = qualified

    def bind_class_attribute(self, class_name: str, attribute: str, qualified: str) -> None:
        """Record ``self.attribute = ...`` for ``class_name``.

        Collected in a pre-pass, because a client assigned in ``__init__`` is
        almost always *used* in a method defined further down the file.
        """
        self._class_attributes.setdefault(class_name, {})[attribute] = qualified

    def lookup(self, name: str) -> str | None:
        """Resolve ``name`` against the scope stack, innermost first."""
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return None

    def lookup_self_attribute(self, attribute: str) -> str | None:
        """Resolve ``self.attribute`` against the enclosing classes."""
        for class_name in reversed(self._class_stack):
            found = self._class_attributes.get(class_name, {}).get(attribute)
            if found is not None:
                return found
        return None

    # --------------------------------------------------------- resolution ---

    def resolve_chain(self, parts: list[str]) -> str | None:
        """Rewrite a dotted chain into its library-qualified form.

        Returns ``None`` when the root is not traceable to an import, which is
        the honest answer for a locally defined helper or an untracked builtin.
        """
        if not parts:
            return None

        if parts[0] == SELF and len(parts) > 1:
            base = self.lookup_self_attribute(parts[1])
            remainder = parts[2:]
        else:
            base = self.lookup(parts[0])
            remainder = parts[1:]

        if base is None:
            return None
        return ".".join([base, *remainder])

    def resolve_expression(self, node: ast.expr) -> str | None:
        """Resolve the qualified origin of a value expression.

        Handles the two forms that matter for tracking a client object: a direct
        reference (``c = client``) and a constructor call
        (``client = OpenAI()``). A constructor resolves to the class itself, so
        the instance and the class share a qualified name; distinguishing them
        would need type inference and buys nothing for call-site matching.
        """
        if isinstance(node, ast.Call):
            return self.resolve_expression(node.func)
        chain = attribute_chain(node)
        if chain is None:
            return None
        return self.resolve_chain(chain)


__all__ = ["SELF", "BindingTable", "attribute_chain"]
