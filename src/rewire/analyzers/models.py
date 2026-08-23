"""Typed models describing the contents of an analysed repository.

Every record is self-locating: it carries the file and line it came from, so a
query result can be handed straight to Phase 3 or rendered in a report without
needing to be paired back up with its container. That redundancy is deliberate —
it makes the index flat, serialisable and easy to filter.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class SymbolKind(StrEnum):
    """What kind of definition a symbol is."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    METHOD = "method"
    ASYNC_METHOD = "async_method"
    VARIABLE = "variable"


class ReferenceKind(StrEnum):
    """How a name is referred to at a particular location.

    Ordered from most to least specific evidence that the reference really is
    the API field being looked for. A keyword argument named ``max_tokens`` is
    near-certain; the bare string ``"max_tokens"`` in a log message is not.
    """

    KEYWORD_ARGUMENT = "keyword_argument"
    DICT_KEY = "dict_key"
    SUBSCRIPT_KEY = "subscript_key"
    PARAMETER = "parameter"
    ATTRIBUTE = "attribute"
    NAME = "name"
    STRING_LITERAL = "string_literal"


#: Relative strength of each reference kind as evidence of real API usage.
#: Phase 3 turns these into confidence scores; they live here so that the
#: ordering is defined once, alongside the enum it grades.
REFERENCE_EVIDENCE: Final[dict[ReferenceKind, float]] = {
    ReferenceKind.KEYWORD_ARGUMENT: 1.0,
    ReferenceKind.DICT_KEY: 0.9,
    ReferenceKind.SUBSCRIPT_KEY: 0.8,
    # A wrapper declaring `def generate(..., max_tokens=256)` forwards the field
    # under its own name, so the parameter is part of the surface to migrate.
    ReferenceKind.PARAMETER: 0.7,
    ReferenceKind.ATTRIBUTE: 0.6,
    ReferenceKind.NAME: 0.4,
    ReferenceKind.STRING_LITERAL: 0.3,
}


class Located(BaseModel):
    """Base for anything that knows where in the repository it came from."""

    model_config = ConfigDict(frozen=True)

    #: Repository-relative POSIX path, so indexes compare equal across platforms.
    file: str
    line: int
    column: int = 0
    #: Qualified name of the function or class containing this record, if any.
    enclosing_symbol: str | None = None


class Import(BaseModel):
    """A single imported name.

    ``import os, sys`` and ``from a import b, c`` each produce one record per
    name, so that queries never have to re-split a statement.
    """

    model_config = ConfigDict(frozen=True)

    file: str
    line: int
    column: int = 0
    #: Module the name comes from: ``openai`` in ``from openai import OpenAI``.
    module: str
    #: Name imported from the module; ``None`` for a plain ``import x``.
    name: str | None = None
    alias: str | None = None
    #: Number of leading dots in a relative import; 0 for absolute.
    level: int = 0

    @property
    def local_name(self) -> str:
        """The name actually bound in the importing module's namespace."""
        if self.alias:
            return self.alias
        if self.name:
            return self.name
        # `import a.b.c` binds `a`, not `a.b.c`.
        return self.module.split(".", maxsplit=1)[0]

    @property
    def qualified_name(self) -> str:
        """Fully qualified origin of the imported name."""
        prefix = "." * self.level
        return f"{prefix}{self.module}.{self.name}" if self.name else f"{prefix}{self.module}"

    @property
    def is_relative(self) -> bool:
        """Whether this is a relative (dotted) import."""
        return self.level > 0

    def is_from(self, module: str) -> bool:
        """Whether this import comes from ``module`` or one of its submodules."""
        return self.module == module or self.module.startswith(f"{module}.")


class Symbol(Located):
    """A definition: a class, function, method or module-level assignment."""

    name: str
    #: Dotted path including the module, e.g. ``app.client.Wrapper.generate``.
    qualified_name: str
    kind: SymbolKind
    end_line: int
    decorators: tuple[str, ...] = ()
    #: Names of the parameters this symbol declares, for functions and methods.
    parameters: tuple[str, ...] = ()

    @property
    def is_callable(self) -> bool:
        """Whether calling this symbol makes sense."""
        return self.kind in _CALLABLE_KINDS

    def contains_line(self, line: int) -> bool:
        """Whether ``line`` falls inside this symbol's definition."""
        return self.line <= line <= self.end_line


_CALLABLE_KINDS: Final[frozenset[SymbolKind]] = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.FUNCTION,
        SymbolKind.ASYNC_FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.ASYNC_METHOD,
    }
)


class FunctionCall(Located):
    """A call site, with its target resolved as far as static analysis allows."""

    #: The call target exactly as written, e.g. ``client.chat.completions.create``.
    callee: str
    #: Last line of the call expression. Real SDK calls span several lines, and
    #: matching an argument to its call needs the range, not just the start.
    end_line: int = 0
    #: The same target with its root name resolved through imports and
    #: assignments, e.g. ``openai.OpenAI.chat.completions.create``. ``None`` when
    #: the root could not be traced to an import.
    resolved_callee: str | None = None
    #: Names of keyword arguments passed at this call site. This is the join key
    #: to Phase 1: an API field named ``max_tokens`` shows up here.
    keywords: tuple[str, ...] = ()
    positional_count: int = 0
    #: Whether the call forwards ``*args``/``**kwargs``, which hides arguments
    #: from static analysis and caps the confidence of any conclusion about it.
    has_star_args: bool = False

    @property
    def root_name(self) -> str:
        """First segment of the written callee."""
        return self.callee.split(".", maxsplit=1)[0]

    def spans(self, line: int) -> bool:
        """Whether ``line`` falls inside this call expression."""
        return self.line <= line <= max(self.end_line, self.line)

    def matches(self, pattern: str) -> bool:
        """Whether this call matches ``pattern``.

        A pattern matches if it equals either the written or the resolved
        callee, or is a trailing segment of either. Suffix matching is what makes
        ``find_calls("chat.completions.create")`` work regardless of what the
        caller named their client instance.
        """
        return any(
            candidate == pattern or candidate.endswith(f".{pattern}")
            for candidate in (self.callee, self.resolved_callee)
            if candidate
        )


class Reference(Located):
    """A textual or syntactic occurrence of a name."""

    name: str
    kind: ReferenceKind
    #: For keyword arguments and subscripts, the expression being called or
    #: indexed, so Phase 3 can tell ``client.create(max_tokens=...)`` from
    #: ``logger.info(max_tokens=...)``.
    context: str | None = None

    @property
    def evidence(self) -> float:
        """How strongly this reference suggests genuine API usage, in ``[0, 1]``."""
        return REFERENCE_EVIDENCE[self.kind]


class EnvVarUsage(Located):
    """A read of an environment variable."""

    name: str
    #: How it was read: ``os.environ``, ``os.getenv``, ``os.environ.get``.
    accessor: str


class Dependency(BaseModel):
    """A declared third-party dependency."""

    model_config = ConfigDict(frozen=True)

    name: str
    #: Version constraint as written, e.g. ``>=1.0,<2``. Empty when unpinned.
    specifier: str = ""
    #: File the declaration came from, repository-relative.
    source: str
    #: Whether it came from a dev/test/optional group rather than the main set.
    is_optional: bool = False
    extra: str | None = None

    @property
    def normalised_name(self) -> str:
        """PEP 503 normalised name, so ``types-PyYAML`` matches ``types_pyyaml``."""
        return self.name.lower().replace("_", "-").replace(".", "-")


class EntryPointKind(StrEnum):
    """How an entry point was identified."""

    CONSOLE_SCRIPT = "console_script"
    MAIN_GUARD = "main_guard"
    WELL_KNOWN_FILENAME = "well_known_filename"
    WEB_APPLICATION = "web_application"


class EntryPoint(BaseModel):
    """A plausible way the repository's code gets executed."""

    model_config = ConfigDict(frozen=True)

    kind: EntryPointKind
    file: str
    line: int | None = None
    #: Script name or object name, when there is one.
    name: str | None = None
    detail: str = ""


class FileInfo(BaseModel):
    """Everything the analyser extracted from one source file."""

    model_config = ConfigDict(frozen=True)

    path: str
    #: Dotted module path derived from the file's location in the repository.
    module: str
    is_test: bool = False
    line_count: int = 0
    size_bytes: int = 0
    imports: tuple[Import, ...] = ()
    symbols: tuple[Symbol, ...] = ()
    calls: tuple[FunctionCall, ...] = ()
    references: tuple[Reference, ...] = ()
    env_vars: tuple[EnvVarUsage, ...] = ()
    #: Populated when the file could not be parsed; every other field is then
    #: empty. Unparseable files are kept in the index rather than dropped, so
    #: that coverage gaps are visible instead of silent.
    parse_error: str | None = None

    @property
    def parsed(self) -> bool:
        """Whether the file was analysed successfully."""
        return self.parse_error is None


class IndexStats(BaseModel):
    """Aggregate counts describing an index."""

    model_config = ConfigDict(frozen=True)

    files_indexed: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    test_files: int = 0
    total_lines: int = 0
    total_bytes: int = 0
    symbols: int = 0
    imports: int = 0
    calls: int = 0
    references: int = 0
    duration_seconds: float = 0.0

    @classmethod
    def from_files(cls, files: list[FileInfo], *, skipped: int, duration: float) -> IndexStats:
        """Compute statistics over analysed files."""
        parsed = [file for file in files if file.parsed]
        return cls(
            files_indexed=len(parsed),
            files_failed=len(files) - len(parsed),
            files_skipped=skipped,
            test_files=sum(1 for file in files if file.is_test),
            total_lines=sum(file.line_count for file in files),
            total_bytes=sum(file.size_bytes for file in files),
            symbols=sum(len(file.symbols) for file in files),
            imports=sum(len(file.imports) for file in files),
            calls=sum(len(file.calls) for file in files),
            references=sum(len(file.references) for file in files),
            duration_seconds=duration,
        )


class RepositoryIndex(BaseModel):
    """A queryable, deterministic snapshot of a repository's Python code."""

    model_config = ConfigDict(frozen=True)

    root: str
    files: tuple[FileInfo, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    entry_points: tuple[EntryPoint, ...] = ()
    stats: IndexStats = Field(default_factory=IndexStats)

    # ------------------------------------------------------------- queries ---

    def find_imports(self, module: str) -> list[Import]:
        """Return every import of ``module`` or one of its submodules."""
        return [record for file in self.files for record in file.imports if record.is_from(module)]

    def find_calls(self, pattern: str) -> list[FunctionCall]:
        """Return every call whose target matches ``pattern``.

        Matching is exact or trailing-segment, against both the written and the
        resolved callee. See :meth:`FunctionCall.matches`.
        """
        return [call for file in self.files for call in file.calls if call.matches(pattern)]

    def find_references(
        self, name: str, *, kinds: frozenset[ReferenceKind] | None = None
    ) -> list[Reference]:
        """Return every reference to ``name``, optionally filtered by kind."""
        return [
            reference
            for file in self.files
            for reference in file.references
            if reference.name == name and (kinds is None or reference.kind in kinds)
        ]

    def find_symbol(self, name: str) -> list[Symbol]:
        """Return symbols whose name or qualified name matches ``name``."""
        return [
            symbol
            for file in self.files
            for symbol in file.symbols
            if name in (symbol.name, symbol.qualified_name)
        ]

    def files_importing(self, module: str) -> list[FileInfo]:
        """Return files that import ``module`` or one of its submodules."""
        return [
            file for file in self.files if any(record.is_from(module) for record in file.imports)
        ]

    def file(self, path: str) -> FileInfo | None:
        """Return the indexed file at ``path``, if present."""
        wanted = PurePosixPath(path).as_posix()
        return next((file for file in self.files if file.path == wanted), None)

    # ------------------------------------------------------------- rollups ---

    @property
    def source_files(self) -> list[FileInfo]:
        """Non-test files."""
        return [file for file in self.files if not file.is_test]

    @property
    def test_files(self) -> list[FileInfo]:
        """Files identified as tests."""
        return [file for file in self.files if file.is_test]

    @property
    def failed_files(self) -> list[FileInfo]:
        """Files that could not be parsed."""
        return [file for file in self.files if not file.parsed]

    def imported_modules(self) -> dict[str, int]:
        """Top-level imported module names, with usage counts, most used first."""
        counts = Counter(
            record.module.split(".", maxsplit=1)[0]
            for file in self.files
            for record in file.imports
            if not record.is_relative
        )
        return dict(counts.most_common())

    def declared_dependency_names(self) -> set[str]:
        """Normalised names of every declared dependency."""
        return {dependency.normalised_name for dependency in self.dependencies}


__all__ = [
    "REFERENCE_EVIDENCE",
    "Dependency",
    "EntryPoint",
    "EntryPointKind",
    "EnvVarUsage",
    "FileInfo",
    "FunctionCall",
    "Import",
    "IndexStats",
    "Located",
    "Reference",
    "ReferenceKind",
    "RepositoryIndex",
    "Symbol",
    "SymbolKind",
]
