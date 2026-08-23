"""The restricted tool surface offered to the migration agent.

The agent does not get a shell, a filesystem, or a general "edit anything" call.
It gets a small set of named operations over a confined workspace, each with a
declared schema, and every one of them is either read-only or produces an edit
*proposal* that Rewire holds in memory. There is no tool that writes to disk.

Two further properties are deliberate:

* **Tool failures are returned, not raised.** A missing file or an ambiguous
  edit comes back to the model as an error result it can act on. Crashing the
  run on the model's first mistake would make the agent brittle for no benefit.
* **Every result is data, never instruction.** File contents are returned inside
  an explicit envelope (see :mod:`rewire.agents.prompts`), because repository
  content is untrusted input that may contain text engineered to look like an
  instruction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from rewire.agents.patch import FileEdit, PatchBuilder
from rewire.agents.workspace import MAX_READ_LINES, Workspace
from rewire.analyzers.models import RepositoryIndex
from rewire.changes.models import ChangeReport
from rewire.core.errors import PatchError, RepositoryError, RewireError
from rewire.impact.models import ImpactReport
from rewire.llm.models import ToolSpec

#: Most matches returned by a search tool in one call.
MAX_SEARCH_RESULTS: Final[int] = 50

#: Most locations described for a single API change.
MAX_IMPACT_LOCATIONS: Final[int] = 25


@dataclass(slots=True)
class ToolResult:
    """The outcome of one tool invocation."""

    content: str
    is_error: bool = False


@dataclass(slots=True)
class ToolContext:
    """Everything the tools operate on for one run."""

    workspace: Workspace
    index: RepositoryIndex
    changes: ChangeReport
    impact: ImpactReport
    patch: PatchBuilder


ToolHandler = Callable[[ToolContext, dict[str, Any]], str]


@dataclass(frozen=True, slots=True)
class Tool:
    """A named operation the agent may invoke."""

    spec: ToolSpec
    handler: ToolHandler

    @property
    def name(self) -> str:
        """The tool's name, as the model sees it."""
        return self.spec.name


def _string(arguments: dict[str, Any], key: str, default: str | None = None) -> str:
    value = arguments.get(key, default)
    if not isinstance(value, str):
        raise RepositoryError(f"argument {key!r} must be a string", got=type(value).__name__)
    return value


def _integer(arguments: dict[str, Any], key: str, default: int) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return default
    return value


# ----------------------------------------------------------------- handlers ---


def _list_files(context: ToolContext, arguments: dict[str, Any]) -> str:
    directory = _string(arguments, "directory", ".")
    files = context.workspace.list_files(directory)
    if not files:
        return f"No files under {directory!r}."
    return "\n".join(files)


def _read_file(context: ToolContext, arguments: dict[str, Any]) -> str:
    path = _string(arguments, "path")
    start = _integer(arguments, "start_line", 1)
    limit = min(_integer(arguments, "limit", 400), MAX_READ_LINES)
    result = context.workspace.read(path, start_line=start, limit=limit)

    numbered = "\n".join(
        f"{number:>5}| {line}"
        for number, line in enumerate(result.text.splitlines(), start=result.start_line)
    )
    header = f"{result.path} lines {result.start_line}-{result.end_line} of {result.total_lines}"
    footer = "\n(truncated; call again with a later start_line)" if result.truncated else ""
    return f"{header}\n{numbered}{footer}"


def _search_code(context: ToolContext, arguments: dict[str, Any]) -> str:
    name = _string(arguments, "name")
    references = context.index.find_references(name)[:MAX_SEARCH_RESULTS]
    if not references:
        return f"No references to {name!r} found."
    return "\n".join(
        f"{reference.file}:{reference.line} [{reference.kind.value}]"
        f" in {reference.enclosing_symbol or 'module scope'}"
        for reference in references
    )


def _find_calls(context: ToolContext, arguments: dict[str, Any]) -> str:
    pattern = _string(arguments, "pattern")
    calls = context.index.find_calls(pattern)[:MAX_SEARCH_RESULTS]
    if not calls:
        return f"No calls matching {pattern!r} found."
    return "\n".join(
        f"{call.file}:{call.line} {call.callee} (keywords: {', '.join(call.keywords) or 'none'})"
        for call in calls
    )


def _find_symbol(context: ToolContext, arguments: dict[str, Any]) -> str:
    name = _string(arguments, "name")
    symbols = context.index.find_symbol(name)[:MAX_SEARCH_RESULTS]
    if not symbols:
        return f"No symbol named {name!r} found."
    return "\n".join(
        f"{symbol.file}:{symbol.line}-{symbol.end_line} {symbol.kind.value} {symbol.qualified_name}"
        for symbol in symbols
    )


def _inspect_api_change(context: ToolContext, arguments: dict[str, Any]) -> str:
    field = arguments.get("field")
    matches = [
        impact
        for impact in context.impact.impacts
        if field is None or impact.change.field == field or impact.change.field_path == field
    ]
    if not matches:
        return f"No detected API change matching {field!r}."

    lines: list[str] = []
    for impact in matches:
        change = impact.change
        lines.append(
            f"{change.type.value} [{change.severity.value}] {change.endpoint or ''}"
            f" field={change.field_path or '-'}"
            + (f" replacement={change.replacement}" if change.replacement else "")
        )
        if change.detail:
            lines.append(f"  {change.detail}")
        for location in impact.locations[:MAX_IMPACT_LOCATIONS]:
            lines.append(
                f"  affected {location.file}:{location.line}"
                f" (confidence {location.confidence:.2f})"
                f" in {location.symbol or 'module scope'}"
            )
    return "\n".join(lines)


def _propose_edit(context: ToolContext, arguments: dict[str, Any]) -> str:
    edit = FileEdit(
        file=_string(arguments, "file"),
        old_text=_string(arguments, "old_text"),
        new_text=_string(arguments, "new_text"),
        rationale=_string(arguments, "rationale", ""),
    )
    context.patch.add(edit)
    return (
        f"Edit staged for {edit.file}. It is held in memory and has not been written "
        f"to disk. Files edited so far: {', '.join(context.patch.edited_files)}."
    )


def _show_diff(context: ToolContext, _arguments: dict[str, Any]) -> str:
    patch = context.patch.build()
    if patch.is_empty:
        return "No edits have been staged yet."
    return patch.unified_diff()


# -------------------------------------------------------------------- specs ---


TOOLS: Final[tuple[Tool, ...]] = (
    Tool(
        spec=ToolSpec(
            name="list_files",
            description=(
                "List Python and other source files in the repository, "
                "optionally under a specific directory."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Repository-relative directory. Defaults to the root.",
                    }
                },
            },
        ),
        handler=_list_files,
    ),
    Tool(
        spec=ToolSpec(
            name="read_file",
            description=(
                "Read a slice of a file, with line numbers. Use this before proposing "
                "an edit so the text you replace matches the file exactly."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repository-relative path."},
                    "start_line": {"type": "integer", "description": "First line, 1-indexed."},
                    "limit": {"type": "integer", "description": "How many lines to return."},
                },
                "required": ["path"],
            },
        ),
        handler=_read_file,
    ),
    Tool(
        spec=ToolSpec(
            name="search_code",
            description=(
                "Find every place a name appears, classified by how it is used "
                "(keyword argument, dict key, attribute, and so on)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact name to look for."}
                },
                "required": ["name"],
            },
        ),
        handler=_search_code,
    ),
    Tool(
        spec=ToolSpec(
            name="find_calls",
            description=(
                "Find call sites whose target matches a dotted pattern, resolved "
                "through imports and assignments. Reports the keyword arguments "
                "passed at each site."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Dotted call target, e.g. chat.completions.create.",
                    }
                },
                "required": ["pattern"],
            },
        ),
        handler=_find_calls,
    ),
    Tool(
        spec=ToolSpec(
            name="find_symbol",
            description="Locate the definition of a function, class or method by name.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Symbol name."}},
                "required": ["name"],
            },
        ),
        handler=_find_symbol,
    ),
    Tool(
        spec=ToolSpec(
            name="inspect_api_change",
            description=(
                "Show the detected API changes and the code locations Rewire "
                "believes they affect, with confidence scores. Omit the field to "
                "see every change."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "description": "Restrict to one field name or field path.",
                    }
                },
            },
        ),
        handler=_inspect_api_change,
    ),
    Tool(
        spec=ToolSpec(
            name="propose_edit",
            description=(
                "Stage an exact string replacement in one file. The old_text must "
                "appear exactly once in the file: include surrounding lines to make "
                "it unique. Nothing is written to disk."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Repository-relative path."},
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to replace, unique within the file.",
                    },
                    "new_text": {"type": "string", "description": "Replacement text."},
                    "rationale": {
                        "type": "string",
                        "description": "Why this edit is needed, for the human reviewer.",
                    },
                },
                "required": ["file", "old_text", "new_text"],
            },
        ),
        handler=_propose_edit,
    ),
    Tool(
        spec=ToolSpec(
            name="show_diff",
            description="Show the unified diff of every edit staged so far.",
            parameters={"type": "object", "properties": {}},
        ),
        handler=_show_diff,
    ),
)

TOOLS_BY_NAME: Final[dict[str, Tool]] = {tool.name: tool for tool in TOOLS}


def tool_specs() -> tuple[ToolSpec, ...]:
    """Every tool specification, in a stable order.

    Stable because tool order forms part of a cached prompt prefix, and
    reordering it would silently invalidate the cache on every request.
    """
    return tuple(tool.spec for tool in TOOLS)


def invoke(name: str, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    """Run a tool, converting expected failures into results the model can read.

    Only :class:`~rewire.core.errors.RewireError` is caught. An unexpected
    exception is a bug in Rewire, and swallowing it into the conversation would
    hide it behind whatever the model did next.
    """
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return ToolResult(
            content=f"Unknown tool {name!r}. Available: {', '.join(sorted(TOOLS_BY_NAME))}.",
            is_error=True,
        )
    try:
        return ToolResult(content=tool.handler(context, arguments))
    except (PatchError, RepositoryError) as exc:
        return ToolResult(content=str(exc), is_error=True)
    except RewireError as exc:
        return ToolResult(content=f"{exc.code}: {exc}", is_error=True)


__all__ = [
    "MAX_IMPACT_LOCATIONS",
    "MAX_SEARCH_RESULTS",
    "TOOLS",
    "TOOLS_BY_NAME",
    "Tool",
    "ToolContext",
    "ToolResult",
    "invoke",
    "tool_specs",
]
