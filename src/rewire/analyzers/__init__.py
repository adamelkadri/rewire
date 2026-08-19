"""Repository analysis: AST indexing, symbol and usage extraction.

Deterministic and LLM-free. The primary backend is Python's own AST, because a
keyword argument named ``max_tokens`` is only distinguishable from the word
``max_tokens`` in a comment by parsing. Text search is available as a fallback
for files the AST cannot cover.

    >>> from rewire.analyzers import build_index
    >>> index = build_index("./example-repo")
    >>> [call.file for call in index.find_calls("chat.completions.create")]
    ['src/chatapp/client.py']
"""

from rewire.analyzers.bindings import BindingTable, attribute_chain
from rewire.analyzers.dependencies import collect_dependencies
from rewire.analyzers.discovery import (
    DiscoveryLimits,
    discover_python_files,
    is_test_path,
    module_path_for,
    resolve_repository_root,
)
from rewire.analyzers.index import build_index, detect_entry_points, source_line
from rewire.analyzers.models import (
    Dependency,
    EntryPoint,
    EntryPointKind,
    EnvVarUsage,
    FileInfo,
    FunctionCall,
    Import,
    IndexStats,
    Reference,
    ReferenceKind,
    RepositoryIndex,
    Symbol,
    SymbolKind,
)
from rewire.analyzers.python_ast import analyse_source
from rewire.analyzers.search import (
    PythonBackend,
    RipgrepBackend,
    SearchBackend,
    TextMatch,
    get_backend,
)

__all__ = [
    "BindingTable",
    "Dependency",
    "DiscoveryLimits",
    "EntryPoint",
    "EntryPointKind",
    "EnvVarUsage",
    "FileInfo",
    "FunctionCall",
    "Import",
    "IndexStats",
    "PythonBackend",
    "Reference",
    "ReferenceKind",
    "RepositoryIndex",
    "RipgrepBackend",
    "SearchBackend",
    "Symbol",
    "SymbolKind",
    "TextMatch",
    "analyse_source",
    "attribute_chain",
    "build_index",
    "collect_dependencies",
    "detect_entry_points",
    "discover_python_files",
    "get_backend",
    "is_test_path",
    "module_path_for",
    "resolve_repository_root",
    "source_line",
]
