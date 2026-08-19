"""Tests for the text search backends.

The two backends must be interchangeable, so most behaviour is asserted against
both. ripgrep is optional, so its tests skip when it is not installed and its
output parsing is exercised through a stubbed subprocess instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rewire.analyzers.search import (
    PythonBackend,
    RipgrepBackend,
    SearchBackend,
    TextMatch,
    get_backend,
)
from rewire.core.errors import RepositoryError

BACKENDS = [PythonBackend(), RipgrepBackend()]


def available_backends() -> list[SearchBackend]:
    return [backend for backend in BACKENDS if backend.available()]


@pytest.fixture(params=[backend.name for backend in BACKENDS])
def backend(request: pytest.FixtureRequest) -> SearchBackend:
    selected = next(item for item in BACKENDS if item.name == request.param)
    if not selected.available():
        pytest.skip(f"{selected.name} is not installed")
    return selected


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "max_tokens = 1\n# a comment about max_tokens\nother = 2\n", encoding="utf-8"
    )
    (tmp_path / "src" / "b.py").write_text("nothing here\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("docs mention max_tokens too\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "dep.py").write_text("max_tokens = 99\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01max_tokens\x00")
    return tmp_path


# ------------------------------------------------------- shared behaviour ---


def test_finds_matches(backend: SearchBackend, tree: Path) -> None:
    matches = backend.search(tree, "max_tokens")
    assert {(m.file, m.line) for m in matches} == {
        ("src/a.py", 1),
        ("src/a.py", 2),
        ("notes.md", 1),
    }


def test_searches_beyond_python(backend: SearchBackend, tree: Path) -> None:
    """Covering files the AST cannot parse is the point of the text backend."""
    assert any(match.file == "notes.md" for match in backend.search(tree, "max_tokens"))


def test_ignored_directories_are_skipped(backend: SearchBackend, tree: Path) -> None:
    assert not [m for m in backend.search(tree, "max_tokens") if m.file.startswith(".venv")]


def test_binary_files_are_skipped(backend: SearchBackend, tree: Path) -> None:
    assert not [m for m in backend.search(tree, "max_tokens") if m.file == "binary.bin"]


def test_no_matches_returns_empty(backend: SearchBackend, tree: Path) -> None:
    assert backend.search(tree, "definitely_absent_name") == []


def test_results_are_sorted(backend: SearchBackend, tree: Path) -> None:
    matches = backend.search(tree, "max_tokens")
    assert matches == sorted(matches, key=lambda match: match.sort_key)


def test_regex_mode(backend: SearchBackend, tree: Path) -> None:
    assert backend.search(tree, r"max_\w+", regex=True)


def test_fixed_string_mode_does_not_interpret_metacharacters(
    backend: SearchBackend, tmp_path: Path
) -> None:
    (tmp_path / "a.py").write_text("value = a.b\nvalue = axb\n", encoding="utf-8")
    matches = backend.search(tmp_path, "a.b")
    assert [match.line for match in matches] == [1]


def test_max_matches_is_respected(backend: SearchBackend, tree: Path) -> None:
    assert len(backend.search(tree, "max_tokens", max_matches=1)) == 1


def test_columns_are_one_based(backend: SearchBackend, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("xx target\n", encoding="utf-8")
    assert backend.search(tmp_path, "target")[0].column == 4


@pytest.mark.skipif(len(available_backends()) < 2, reason="needs both backends installed")
def test_backends_agree(tree: Path) -> None:
    """The Python fallback is held to ripgrep's contract, not a weaker one."""
    results = [
        [(m.file, m.line, m.column) for m in backend.search(tree, "max_tokens")]
        for backend in available_backends()
    ]
    assert results[0] == results[1]


# ----------------------------------------------------------- python backend ---


def test_python_backend_is_always_available() -> None:
    assert PythonBackend().available()


def test_python_backend_rejects_invalid_regex(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="invalid search pattern"):
        PythonBackend().search(tmp_path, "(unclosed", regex=True)


def test_python_backend_skips_symlinks(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.py").write_text("max_tokens\n", encoding="utf-8")
    (tmp_path / "repo" / "link").symlink_to(tmp_path / "outside", target_is_directory=True)
    assert PythonBackend().search(tmp_path / "repo", "max_tokens") == []


def test_python_backend_glob_filter(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("target\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("target\n", encoding="utf-8")
    matches = PythonBackend().search(tmp_path, "target", globs=("*.py",))
    assert [match.file for match in matches] == ["a.py"]


def test_python_backend_tolerates_unreadable_files(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("target\n", encoding="utf-8")
    locked = tmp_path / "locked.py"
    locked.write_text("target\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        assert [m.file for m in PythonBackend().search(tmp_path, "target")] == ["ok.py"]
    finally:
        locked.chmod(0o644)


# ---------------------------------------------------------- ripgrep backend ---


def stub_ripgrep(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", returncode: int = 0, stderr: str = ""
) -> None:
    monkeypatch.setattr("rewire.analyzers.search.shutil.which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(
        "rewire.analyzers.search.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["rg"], returncode=returncode, stdout=stdout, stderr=stderr
        ),
    )


RG_MATCH = (
    '{"type":"match","data":{"path":{"text":"%s/src/a.py"},'
    '"lines":{"text":"max_tokens = 1\\n"},"line_number":1,'
    '"submatches":[{"start":0,"end":10}]}}'
)


def test_ripgrep_parses_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub_ripgrep(monkeypatch, stdout=RG_MATCH % tmp_path)
    match = RipgrepBackend().search(tmp_path, "max_tokens")[0]
    assert match == TextMatch(file="src/a.py", line=1, column=1, text="max_tokens = 1")


def test_ripgrep_exit_code_1_means_no_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_ripgrep(monkeypatch, returncode=1)
    assert RipgrepBackend().search(tmp_path, "absent") == []


def test_ripgrep_real_failure_is_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub_ripgrep(monkeypatch, returncode=2, stderr="regex parse error\n")
    with pytest.raises(RepositoryError, match="ripgrep failed"):
        RipgrepBackend().search(tmp_path, "(")


def test_ripgrep_ignores_non_match_events(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub_ripgrep(monkeypatch, stdout='{"type":"begin","data":{}}\nnot json\n')
    assert RipgrepBackend().search(tmp_path, "x") == []


def test_ripgrep_timeout_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("rewire.analyzers.search.shutil.which", lambda _name: "/usr/bin/rg")

    def _timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="rg", timeout=60.0)

    monkeypatch.setattr("rewire.analyzers.search.subprocess.run", _timeout)
    with pytest.raises(RepositoryError, match="timed out"):
        RipgrepBackend().search(tmp_path, "x")


def test_ripgrep_missing_binary_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("rewire.analyzers.search.shutil.which", lambda _name: "/usr/bin/rg")

    def _oserror(*_args: object, **_kwargs: object) -> None:
        raise OSError("no such file")

    monkeypatch.setattr("rewire.analyzers.search.subprocess.run", _oserror)
    with pytest.raises(RepositoryError, match="could not run ripgrep"):
        RipgrepBackend().search(tmp_path, "x")


def test_ripgrep_handles_paths_outside_the_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_ripgrep(monkeypatch, stdout=RG_MATCH % "/elsewhere")
    assert RipgrepBackend().search(tmp_path, "x")[0].file == "/elsewhere/src/a.py"


def test_ripgrep_skips_non_utf8_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stdout = (
        '{"type":"match","data":{"path":{"bytes":"eA=="},"lines":{"text":"x"},"line_number":1}}'
    )
    stub_ripgrep(monkeypatch, stdout=stdout)
    assert RipgrepBackend().search(tmp_path, "x") == []


# ------------------------------------------------------------ backend choice --


def test_auto_prefers_ripgrep_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rewire.analyzers.search.shutil.which", lambda _name: "/usr/bin/rg")
    assert get_backend("auto").name == "ripgrep"


def test_auto_falls_back_to_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ripgrep is optional, so the fallback has to be a real implementation."""
    monkeypatch.setattr("rewire.analyzers.search.shutil.which", lambda _name: None)
    assert get_backend("auto").name == "python"


def test_explicit_backend_selection() -> None:
    assert get_backend("python").name == "python"


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(RepositoryError, match="unknown search backend"):
        get_backend("grep")


def test_demanding_an_uninstalled_backend_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rewire.analyzers.search.shutil.which", lambda _name: None)
    with pytest.raises(RepositoryError, match="not installed"):
        get_backend("ripgrep")


def test_python_backend_tolerates_unreadable_directories(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("target\n", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "inner.py").write_text("target\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        assert [m.file for m in PythonBackend().search(tmp_path, "target")] == ["ok.py"]
    finally:
        locked.chmod(0o755)


def test_ripgrep_passes_globs_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def _capture(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="")

    monkeypatch.setattr("rewire.analyzers.search.shutil.which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr("rewire.analyzers.search.subprocess.run", _capture)

    RipgrepBackend().search(tmp_path, "x", globs=("*.py",))
    command = captured[0]
    assert "*.py" in command
    # The pattern must follow `--` so a leading dash cannot be read as an option.
    assert command[command.index("--") + 1] == "x"


def test_ripgrep_uses_fixed_strings_unless_regex_is_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[list[str]] = []

    def _capture(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="")

    monkeypatch.setattr("rewire.analyzers.search.shutil.which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr("rewire.analyzers.search.subprocess.run", _capture)

    RipgrepBackend().search(tmp_path, "a.b")
    RipgrepBackend().search(tmp_path, "a.b", regex=True)
    assert "--fixed-strings" in captured[0]
    assert "--fixed-strings" not in captured[1]
