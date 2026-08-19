"""Tests for building and querying a repository index.

Assertions run against a sample application that calls the OpenAI SDK three
different ways on purpose — through an instance attribute, a module-level
client and a local alias. Any one of those spellings is easy to find; finding
all three is the point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.analyzers import (
    DiscoveryLimits,
    EntryPointKind,
    ReferenceKind,
    RepositoryIndex,
    build_index,
    source_line,
)
from rewire.core.errors import RepositoryError

# ------------------------------------------------------------------- build ---


def test_index_covers_the_repository(sample_index: RepositoryIndex) -> None:
    paths = {file.path for file in sample_index.files}
    assert paths == {
        "scripts/backfill.py",
        "src/chatapp/__init__.py",
        "src/chatapp/api.py",
        "src/chatapp/broken.py",
        "src/chatapp/cli.py",
        "src/chatapp/client.py",
        "src/chatapp/config.py",
        "tests/test_client.py",
    }


def test_noise_directories_are_absent(sample_index: RepositoryIndex) -> None:
    """.venv contains a module named `openai`; indexing it would poison results."""
    assert not [f for f in sample_index.files if ".venv" in f.path or "node_modules" in f.path]
    assert not [f for f in sample_index.files if f.path.startswith("build/")]


def test_unparseable_files_are_kept_and_flagged(sample_index: RepositoryIndex) -> None:
    failed = sample_index.failed_files
    assert [file.path for file in failed] == ["src/chatapp/broken.py"]
    assert "SyntaxError" in (failed[0].parse_error or "")
    assert sample_index.stats.files_failed == 1


def test_test_files_are_distinguished(sample_index: RepositoryIndex) -> None:
    assert [file.path for file in sample_index.test_files] == ["tests/test_client.py"]
    assert "tests/test_client.py" not in {f.path for f in sample_index.source_files}


def test_stats_are_consistent(sample_index: RepositoryIndex) -> None:
    stats = sample_index.stats
    assert stats.files_indexed + stats.files_failed == len(sample_index.files)
    assert stats.symbols == sum(len(f.symbols) for f in sample_index.files)
    assert stats.duration_seconds >= 0


def test_indexing_is_deterministic(sample_repo: Path) -> None:
    """Downstream evaluation numbers are meaningless if the index varies."""
    first = build_index(sample_repo)
    second = build_index(sample_repo)
    assert first.model_dump_json(exclude={"stats"}) == second.model_dump_json(exclude={"stats"})


def test_index_round_trips_through_json(sample_index: RepositoryIndex) -> None:
    assert RepositoryIndex.model_validate_json(sample_index.model_dump_json()) == sample_index


def test_tests_can_be_excluded(sample_repo: Path) -> None:
    index = build_index(sample_repo, limits=DiscoveryLimits(include_tests=False))
    assert index.test_files == []
    assert index.stats.files_skipped >= 1


def test_missing_repository_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError):
        build_index(tmp_path / "absent")


def test_empty_repository_indexes_to_nothing(tmp_path: Path) -> None:
    index = build_index(tmp_path)
    assert index.files == ()
    assert index.stats.files_indexed == 0


# ----------------------------------------------------------------- queries ---


def test_find_calls_across_three_spellings(sample_index: RepositoryIndex) -> None:
    """The whole case for AST analysis: one query, three different call styles."""
    calls = sample_index.find_calls("chat.completions.create")
    assert {call.callee for call in calls} == {
        "self._client.chat.completions.create",
        "client.chat.completions.create",
    }
    assert len(calls) == 3


def test_find_calls_by_resolved_library_path(sample_index: RepositoryIndex) -> None:
    """Callers can query by the SDK's real name, not the local variable's."""
    calls = sample_index.find_calls("openai.OpenAI.chat.completions.create")
    assert len(calls) == 3
    assert all(call.resolved_callee is not None for call in calls)


def test_find_calls_captures_keyword_arguments(sample_index: RepositoryIndex) -> None:
    """This is the join key to Phase 1: a removed API field is a keyword here."""
    calls = sample_index.find_calls("chat.completions.create")
    assert all("max_tokens" in call.keywords for call in calls)


def test_find_calls_on_an_absent_target(sample_index: RepositoryIndex) -> None:
    assert sample_index.find_calls("chat.completions.delete") == []


def test_find_imports_matches_submodules(sample_index: RepositoryIndex) -> None:
    imports = sample_index.find_imports("openai")
    assert {record.qualified_name for record in imports} == {"openai", "openai.OpenAI"}


def test_find_imports_does_not_match_prefixes(sample_index: RepositoryIndex) -> None:
    assert sample_index.find_imports("open") == []


def test_files_importing(sample_index: RepositoryIndex) -> None:
    assert [file.path for file in sample_index.files_importing("openai")] == [
        "src/chatapp/client.py"
    ]


def test_find_references_grades_by_kind(sample_index: RepositoryIndex) -> None:
    references = sample_index.find_references("max_tokens")
    kinds = {reference.kind for reference in references}
    assert ReferenceKind.KEYWORD_ARGUMENT in kinds
    assert ReferenceKind.DICT_KEY in kinds
    assert ReferenceKind.PARAMETER in kinds


def test_find_references_can_filter_by_kind(sample_index: RepositoryIndex) -> None:
    strong = sample_index.find_references(
        "max_tokens", kinds=frozenset({ReferenceKind.KEYWORD_ARGUMENT})
    )
    assert strong
    assert all(r.kind is ReferenceKind.KEYWORD_ARGUMENT for r in strong)
    assert len(strong) < len(sample_index.find_references("max_tokens"))


def test_find_references_includes_test_call_sites(sample_index: RepositoryIndex) -> None:
    """Tests exercise the API too, and a migration has to update them."""
    files = {reference.file for reference in sample_index.find_references("max_tokens")}
    assert "tests/test_client.py" in files


def test_find_symbol_by_short_and_qualified_name(sample_index: RepositoryIndex) -> None:
    assert sample_index.find_symbol("ChatClient")
    assert sample_index.find_symbol("src.chatapp.client.ChatClient.generate")


def test_file_lookup(sample_index: RepositoryIndex) -> None:
    assert sample_index.file("src/chatapp/client.py") is not None
    assert sample_index.file("does/not/exist.py") is None


def test_imported_modules_are_counted_and_ranked(sample_index: RepositoryIndex) -> None:
    modules = sample_index.imported_modules()
    assert modules["openai"] == 2
    counts = list(modules.values())
    assert counts == sorted(counts, reverse=True)


# ------------------------------------------------------------ dependencies ---


def test_dependencies_are_collected(sample_index: RepositoryIndex) -> None:
    names = {dependency.name for dependency in sample_index.dependencies}
    assert {"openai", "fastapi", "httpx", "pytest", "mypy"} <= names


def test_imports_can_be_matched_against_declared_dependencies(
    sample_index: RepositoryIndex,
) -> None:
    declared = sample_index.declared_dependency_names()
    assert "openai" in declared
    # The repository's own package is imported but never declared.
    assert "chatapp" not in declared


# ------------------------------------------------------------ entry points ---


def test_console_script_entry_point(sample_index: RepositoryIndex) -> None:
    scripts = [e for e in sample_index.entry_points if e.kind is EntryPointKind.CONSOLE_SCRIPT]
    assert [(e.name, e.detail) for e in scripts] == [("chatapp", "chatapp.cli:main")]


def test_main_guard_entry_points(sample_index: RepositoryIndex) -> None:
    guards = {e.file for e in sample_index.entry_points if e.kind is EntryPointKind.MAIN_GUARD}
    assert guards == {"scripts/backfill.py", "src/chatapp/cli.py"}


def test_web_application_entry_point(sample_index: RepositoryIndex) -> None:
    web = [e for e in sample_index.entry_points if e.kind is EntryPointKind.WEB_APPLICATION]
    assert [(e.file, e.name, e.detail) for e in web] == [
        ("src/chatapp/api.py", "app", "fastapi.FastAPI")
    ]


def test_entry_points_are_sorted(sample_index: RepositoryIndex) -> None:
    keys = [(e.file, e.kind.value, e.name or "") for e in sample_index.entry_points]
    assert keys == sorted(keys)


def test_well_known_filename_entry_point(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("x = 1\n", encoding="utf-8")
    index = build_index(tmp_path)
    assert [e.kind for e in index.entry_points] == [EntryPointKind.WELL_KNOWN_FILENAME]


def test_test_files_are_not_entry_points(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "main.py").write_text("x = 1\n", encoding="utf-8")
    index = build_index(tmp_path)
    assert not [e for e in index.entry_points if e.kind is EntryPointKind.WELL_KNOWN_FILENAME]


def test_a_main_guard_inside_a_function_is_not_confused(tmp_path: Path) -> None:
    """`__name__` and `"__main__"` must appear on the same line to count."""
    (tmp_path / "a.py").write_text('name = __name__\nlabel = "__main__"\n', encoding="utf-8")
    index = build_index(tmp_path)
    assert not [e for e in index.entry_points if e.kind is EntryPointKind.MAIN_GUARD]


def test_locally_constructed_web_app_is_not_an_entry_point(tmp_path: Path) -> None:
    """Only a module-level assignment is a deployable application object."""
    (tmp_path / "a.py").write_text(
        "from fastapi import FastAPI\n\ndef make():\n    return FastAPI()\n", encoding="utf-8"
    )
    index = build_index(tmp_path)
    assert not [e for e in index.entry_points if e.kind is EntryPointKind.WEB_APPLICATION]


# ------------------------------------------------------------- environment ---


def test_environment_variables_are_found(sample_index: RepositoryIndex) -> None:
    names = {usage.name for file in sample_index.files for usage in file.env_vars}
    assert names == {"OPENAI_API_KEY", "OPENAI_BASE_URL", "REQUEST_TIMEOUT"}


# ----------------------------------------------------------------- context ---


def test_source_line_reads_a_specific_line(sample_repo: Path) -> None:
    line = source_line(sample_repo, "src/chatapp/client.py", 1)
    assert line is not None
    assert line.startswith('"""Thin wrapper')


def test_source_line_returns_none_past_the_end(sample_repo: Path) -> None:
    assert source_line(sample_repo, "src/chatapp/client.py", 99_999) is None


def test_source_line_returns_none_for_a_missing_file(sample_repo: Path) -> None:
    """A file may have changed since indexing; that is a display gap, not a failure."""
    assert source_line(sample_repo, "gone.py", 1) is None


# -------------------------------------------------------------- resilience ---


def test_unreadable_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    locked = tmp_path / "locked.py"
    locked.write_text("y = 2\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        index = build_index(tmp_path)
        assert [file.path for file in index.files] == ["ok.py"]
        assert index.stats.files_skipped == 1
    finally:
        locked.chmod(0o644)


def test_malformed_pyproject_does_not_break_entry_points(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project\nbroken", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert build_index(tmp_path).entry_points == ()


def test_pyproject_without_scripts_yields_no_console_entry_points(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "a"\n', encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    index = build_index(tmp_path)
    assert not [e for e in index.entry_points if e.kind is EntryPointKind.CONSOLE_SCRIPT]
