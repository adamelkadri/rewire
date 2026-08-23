"""Extracting structured facts from a Python source file.

One walk over the AST produces imports, definitions, call sites, name references
and environment-variable reads. Nothing here is heuristic text matching: a
``max_tokens=`` keyword argument is recognised because it *is* a keyword argument
in the parse tree, which is why the results can be trusted enough to drive an
automated edit.

Instance attributes are collected in a pre-pass before the main walk, because a
client assigned in ``__init__`` is nearly always used by a method defined later
in the file, and a single forward walk would miss it.
"""

from __future__ import annotations

import ast
from typing import Final

from rewire.analyzers.bindings import SELF, BindingTable, attribute_chain
from rewire.analyzers.models import (
    EnvVarUsage,
    FileInfo,
    FunctionCall,
    Import,
    Reference,
    ReferenceKind,
    Symbol,
    SymbolKind,
)

#: Call targets that read an environment variable, mapped to the argument index
#: holding the variable's name.
ENV_ACCESSORS: Final[dict[str, int]] = {
    "os.getenv": 0,
    "os.environ.get": 0,
    "os.environ.setdefault": 0,
    "environ.get": 0,
    "getenv": 0,
}

#: Subscript targets that read an environment variable.
ENV_SUBSCRIPTS: Final[frozenset[str]] = frozenset({"os.environ", "environ"})

#: Longest string constant recorded as a reference. Anything longer is prose,
#: SQL or embedded data rather than an API field name.
MAX_STRING_REFERENCE_LENGTH: Final[int] = 128


def _function_kind(node: ast.AST, *, in_class: bool) -> SymbolKind:
    is_async = isinstance(node, ast.AsyncFunctionDef)
    if in_class:
        return SymbolKind.ASYNC_METHOD if is_async else SymbolKind.METHOD
    return SymbolKind.ASYNC_FUNCTION if is_async else SymbolKind.FUNCTION


def _decorator_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> tuple[str, ...]:
    names: list[str] = []
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        chain = attribute_chain(target)
        if chain is not None:
            names.append(".".join(chain))
    return tuple(names)


def _parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    args = node.args
    names = [arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return tuple(names)


def _bind_import(bindings: BindingTable, node: ast.Import) -> None:
    """Bind the names introduced by a plain ``import`` statement."""
    for alias in node.names:
        local = alias.asname or alias.name.split(".", maxsplit=1)[0]
        bindings.bind(local, alias.name if alias.asname else local)


def _bind_import_from(bindings: BindingTable, node: ast.ImportFrom) -> None:
    """Bind the names introduced by a ``from x import y`` statement.

    Relative imports and star imports bind nothing: resolving either needs the
    target module, which the analyser does not load.
    """
    if node.level:
        return
    module = node.module or ""
    for alias in node.names:
        if alias.name == "*":
            continue
        bindings.bind(alias.asname or alias.name, f"{module}.{alias.name}")


class _InstanceAttributeCollector(ast.NodeVisitor):
    """Pre-pass gathering ``self.x = ...`` assignments for each class.

    Imports are bound here too. Without them ``self._client = OpenAI()`` cannot
    be resolved, because the pre-pass runs before the main walk has seen the
    import that gives ``OpenAI`` its meaning.
    """

    def __init__(self, bindings: BindingTable, module: str) -> None:
        self._bindings = bindings
        self._module = module
        self._class_stack: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        _bind_import(self._bindings, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        _bind_import_from(self._bindings, node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = ".".join([self._module, *self._class_stack, node.name])
        self._class_stack.append(node.name)
        self._bindings.push_class(qualified)
        self.generic_visit(node)
        self._bindings.pop_class()
        self._class_stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        current = self._bindings.current_class
        if current is not None:
            resolved = self._bindings.resolve_expression(node.value)
            if resolved is not None:
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == SELF
                    ):
                        self._bindings.bind_class_attribute(current, target.attr, resolved)
        self.generic_visit(node)


class _FileAnalyser(ast.NodeVisitor):
    """Single-pass collector for one module's facts."""

    def __init__(self, path: str, module: str, bindings: BindingTable) -> None:
        self._path = path
        self._module = module
        self._bindings = bindings
        self._symbol_stack: list[str] = []

        self.imports: list[Import] = []
        self.symbols: list[Symbol] = []
        self.calls: list[FunctionCall] = []
        self.references: list[Reference] = []
        self.env_vars: list[EnvVarUsage] = []
        #: Constants already consumed as an environment-variable name. Recording
        #: them again as bare string literals would break the invariant that one
        #: source position yields one reference, and would give Phase 3 two
        #: pieces of evidence for a single occurrence.
        self._consumed_constants: set[int] = set()

    # ------------------------------------------------------------ helpers ---

    @property
    def _enclosing(self) -> str | None:
        return self._symbol_stack[-1] if self._symbol_stack else None

    def _qualify(self, name: str) -> str:
        parent = self._enclosing or self._module
        return f"{parent}.{name}"

    def _add_reference(
        self, name: str, kind: ReferenceKind, node: ast.AST, *, context: str | None = None
    ) -> None:
        self.references.append(
            Reference(
                file=self._path,
                line=getattr(node, "lineno", 0),
                column=getattr(node, "col_offset", 0),
                enclosing_symbol=self._enclosing,
                name=name,
                kind=kind,
                context=context,
            )
        )

    # ------------------------------------------------------------ imports ---

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            record = Import(
                file=self._path,
                line=node.lineno,
                column=node.col_offset,
                module=alias.name,
                alias=alias.asname,
            )
            self.imports.append(record)
            # `import a.b.c` binds `a`; `import a.b.c as x` binds `x` to a.b.c.
            self._bindings.bind(
                record.local_name, alias.name if alias.asname else record.local_name
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                # A star import binds names that cannot be enumerated without
                # resolving the target module. Record it, bind nothing.
                self.imports.append(
                    Import(
                        file=self._path,
                        line=node.lineno,
                        column=node.col_offset,
                        module=module,
                        name="*",
                        level=node.level or 0,
                    )
                )
                continue
            record = Import(
                file=self._path,
                line=node.lineno,
                column=node.col_offset,
                module=module,
                name=alias.name,
                alias=alias.asname,
                level=node.level or 0,
            )
            self.imports.append(record)
        _bind_import_from(self._bindings, node)
        self.generic_visit(node)

    # -------------------------------------------------------- definitions ---

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = self._qualify(node.name)
        self.symbols.append(
            Symbol(
                file=self._path,
                line=node.lineno,
                column=node.col_offset,
                enclosing_symbol=self._enclosing,
                name=node.name,
                qualified_name=qualified,
                kind=SymbolKind.CLASS,
                end_line=node.end_lineno or node.lineno,
                decorators=_decorator_names(node),
            )
        )
        self._symbol_stack.append(qualified)
        self._bindings.push_class(qualified)
        self._bindings.push_scope()
        self.generic_visit(node)
        self._bindings.pop_scope()
        self._bindings.pop_class()
        self._symbol_stack.pop()

    def _record_parameters(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Record each declared parameter as a reference to its own name.

        A wrapper that accepts ``max_tokens`` and forwards it is part of the
        surface a migration has to change, and its signature is the only place
        the name appears without being a keyword argument at a call site.
        """
        args = node.args
        for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            self._add_reference(argument.arg, ReferenceKind.PARAMETER, argument, context=node.name)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified = self._qualify(node.name)
        self.symbols.append(
            Symbol(
                file=self._path,
                line=node.lineno,
                column=node.col_offset,
                enclosing_symbol=self._enclosing,
                name=node.name,
                qualified_name=qualified,
                kind=_function_kind(node, in_class=self._bindings.current_class is not None),
                end_line=node.end_lineno or node.lineno,
                decorators=_decorator_names(node),
                parameters=_parameter_names(node),
            )
        )
        self._symbol_stack.append(qualified)
        self._bindings.push_scope()
        self._record_parameters(node)
        self.generic_visit(node)
        self._bindings.pop_scope()
        self._symbol_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    # -------------------------------------------------------- assignments ---

    def visit_Assign(self, node: ast.Assign) -> None:
        resolved = self._bindings.resolve_expression(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if resolved is not None:
                    self._bindings.bind(target.id, resolved)
                if not self._symbol_stack:
                    self.symbols.append(
                        Symbol(
                            file=self._path,
                            line=target.lineno,
                            column=target.col_offset,
                            name=target.id,
                            qualified_name=self._qualify(target.id),
                            kind=SymbolKind.VARIABLE,
                            end_line=node.end_lineno or node.lineno,
                        )
                    )
        self.generic_visit(node)

    # --------------------------------------------------------------- calls ---

    def visit_Call(self, node: ast.Call) -> None:
        chain = attribute_chain(node.func)
        if chain is not None:
            callee = ".".join(chain)
            resolved = self._bindings.resolve_chain(chain)
            keywords = tuple(keyword.arg for keyword in node.keywords if keyword.arg is not None)
            call = FunctionCall(
                file=self._path,
                line=node.lineno,
                column=node.col_offset,
                enclosing_symbol=self._enclosing,
                callee=callee,
                end_line=node.end_lineno or node.lineno,
                resolved_callee=resolved,
                keywords=keywords,
                positional_count=len(node.args),
                has_star_args=any(isinstance(arg, ast.Starred) for arg in node.args)
                or any(keyword.arg is None for keyword in node.keywords),
            )
            self.calls.append(call)

            # Anchor each keyword to its own position, not the call's. Real
            # SDK calls span several lines, so using the call node reported
            # every argument at the line the callee was written on.
            for keyword in node.keywords:
                if keyword.arg is not None:
                    self._add_reference(
                        keyword.arg, ReferenceKind.KEYWORD_ARGUMENT, keyword, context=callee
                    )
            self._record_env_call(node, callee, resolved)

        self.generic_visit(node)

    def _record_env_call(self, node: ast.Call, callee: str, resolved: str | None) -> None:
        index = ENV_ACCESSORS.get(resolved or callee, ENV_ACCESSORS.get(callee))
        if index is None or len(node.args) <= index:
            return
        argument = node.args[index]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            self._consumed_constants.add(id(argument))
            self.env_vars.append(
                EnvVarUsage(
                    file=self._path,
                    line=node.lineno,
                    column=node.col_offset,
                    enclosing_symbol=self._enclosing,
                    name=argument.value,
                    accessor=callee,
                )
            )

    # ---------------------------------------------------------- references ---

    def visit_Dict(self, node: ast.Dict) -> None:
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                self._add_reference(key.value, ReferenceKind.DICT_KEY, key)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        index = node.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, str):
            chain = attribute_chain(node.value)
            container = ".".join(chain) if chain else None
            if container in ENV_SUBSCRIPTS or (
                container and self._bindings.resolve_chain(chain or []) in ENV_SUBSCRIPTS
            ):
                self._consumed_constants.add(id(index))
                self.env_vars.append(
                    EnvVarUsage(
                        file=self._path,
                        line=node.lineno,
                        column=node.col_offset,
                        enclosing_symbol=self._enclosing,
                        name=index.value,
                        accessor=container or "",
                    )
                )
            else:
                self._add_reference(
                    index.value, ReferenceKind.SUBSCRIPT_KEY, index, context=container
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._add_reference(node.attr, ReferenceKind.ATTRIBUTE, node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._add_reference(node.id, ReferenceKind.NAME, node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if id(node) in self._consumed_constants:
            return
        if isinstance(node.value, str) and 0 < len(node.value) <= MAX_STRING_REFERENCE_LENGTH:
            self._add_reference(node.value, ReferenceKind.STRING_LITERAL, node)
        self.generic_visit(node)


def _deduplicate_references(references: list[Reference]) -> tuple[Reference, ...]:
    """Keep the strongest reference kind per (name, line, column).

    A dict key is visited both as a dict key and as a string constant; a keyword
    argument is not, but an attribute inside a call chain is visited once per
    nesting level. Recording every visit would inflate Phase 3's evidence counts
    for what is really a single occurrence in the source.
    """
    strongest: dict[tuple[str, int, int], Reference] = {}
    for reference in references:
        key = (reference.name, reference.line, reference.column)
        current = strongest.get(key)
        if current is None or reference.evidence > current.evidence:
            strongest[key] = reference
    return tuple(sorted(strongest.values(), key=lambda item: (item.line, item.column, item.name)))


def analyse_source(
    source: bytes, *, path: str, module: str, is_test: bool = False, size_bytes: int | None = None
) -> FileInfo:
    """Analyse one Python source file.

    Args:
        source: Raw file bytes. Passed to :func:`ast.parse` undecoded so that a
            PEP 263 encoding declaration is honoured.
        path: Repository-relative POSIX path, recorded on every extracted record.
        module: Dotted module path used to qualify symbol names.
        is_test: Whether the file is test code.
        size_bytes: Size on disk; defaults to the length of ``source``.

    Returns:
        A populated :class:`FileInfo`, or one carrying ``parse_error`` if the
        file could not be parsed. Unparseable files are never dropped: a silent
        omission would let Rewire report "no usages found" for code it never read.
    """
    text = source.decode("utf-8", errors="replace")
    line_count = text.count("\n") + 1 if text else 0
    resolved_size = size_bytes if size_bytes is not None else len(source)

    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as exc:
        return FileInfo(
            path=path,
            module=module,
            is_test=is_test,
            line_count=line_count,
            size_bytes=resolved_size,
            parse_error=f"{type(exc).__name__}: {exc}",
        )

    bindings = BindingTable()
    _InstanceAttributeCollector(bindings, module).visit(tree)

    analyser = _FileAnalyser(path, module, bindings)
    analyser.visit(tree)

    return FileInfo(
        path=path,
        module=module,
        is_test=is_test,
        line_count=line_count,
        size_bytes=resolved_size,
        imports=tuple(analyser.imports),
        symbols=tuple(analyser.symbols),
        calls=tuple(analyser.calls),
        references=_deduplicate_references(analyser.references),
        env_vars=tuple(analyser.env_vars),
    )


__all__ = [
    "ENV_ACCESSORS",
    "ENV_SUBSCRIPTS",
    "MAX_STRING_REFERENCE_LENGTH",
    "analyse_source",
]
