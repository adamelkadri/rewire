"""Tests for the Docker backend.

Split in two. The first half asserts the container is *configured* correctly by
inspecting the argv, which needs no daemon and therefore always runs. The second
half is marked ``integration`` and proves the configuration actually holds by
trying to break out of it — the isolation claims in the module docstring of
``rewire.sandbox.runner`` are only worth making because these tests execute them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from rewire.agents.patch import FileEdit, PatchBuilder
from rewire.core.errors import SandboxError, ToolchainError
from rewire.sandbox.models import CheckKind, CheckStatus, Verdict, VerificationRequest
from rewire.sandbox.runner import CONTAINER_ENV, DockerRunner
from rewire.sandbox.toolchain import WORKSPACE_DIR
from rewire.sandbox.verifier import verify

IMAGE = "python:3.12-slim"


def docker_available() -> bool:
    """Whether a Docker daemon is reachable, without raising."""
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],  # noqa: S607 - from PATH
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


needs_docker = pytest.mark.skipif(not docker_available(), reason="Docker daemon is not reachable")


@pytest.fixture
def runner(tmp_path: Path) -> DockerRunner:
    return DockerRunner(workspace=tmp_path, image=IMAGE)


# ------------------------------------------------------------ configuration ---


def test_the_container_is_isolated_by_default(runner: DockerRunner) -> None:
    """Every one of these flags is load-bearing; a missing one is a silent hole."""
    argv = runner._docker_argv("name", "none")
    joined = " ".join(argv)
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--read-only" in argv
    assert "--pids-limit 512" in joined
    assert "--memory 2048m" in joined
    # Equal to --memory, or the ceiling can be escaped by swapping.
    assert "--memory-swap 2048m" in joined
    assert "--rm" in argv


def test_the_workspace_is_the_only_mount(runner: DockerRunner, tmp_path: Path) -> None:
    argv = runner._docker_argv("name", "none")
    mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "--volume"]
    assert mounts == [f"{tmp_path}:{WORKSPACE_DIR}"]


def test_the_read_only_root_can_be_relaxed_explicitly(tmp_path: Path) -> None:
    runner = DockerRunner(workspace=tmp_path, image=IMAGE, read_only_rootfs=False)
    assert "--read-only" not in runner._docker_argv("name", "none")


def test_containers_are_labelled_so_strays_can_be_found(runner: DockerRunner) -> None:
    assert "rewire.sandbox=1" in runner._docker_argv("name", "none")


def test_the_environment_points_writes_at_the_tmpfs(runner: DockerRunner) -> None:
    """The root filesystem is read only, so pip needs somewhere else to live."""
    assert CONTAINER_ENV["HOME"] == "/tmp"  # noqa: S108 - container path
    assert "--tmpfs" in runner._docker_argv("name", "none")


def test_a_missing_docker_binary_is_a_clear_error(
    runner: DockerRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ToolchainError, match="not found on PATH"):
        runner.preflight()


def test_an_unreachable_daemon_is_a_clear_error(
    runner: DockerRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "Cannot connect to the daemon"),
    )
    with pytest.raises(ToolchainError, match="is Docker running"):
        runner.preflight()


def test_docker_failing_to_start_is_a_sandbox_error(
    runner: DockerRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", explode)
    with pytest.raises(SandboxError, match="could not execute docker"):
        runner.run(("true",), timeout=5)


def test_an_image_that_cannot_be_pulled_is_a_sandbox_error(
    runner: DockerRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "manifest unknown")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(SandboxError, match="could not pull"):
        runner.ensure_image()


def test_a_present_image_is_not_pulled(
    runner: DockerRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def probe(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv[1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", probe)
    runner.ensure_image()
    assert seen == ["image"]


def test_a_timeout_kills_the_container_and_keeps_partial_output(
    runner: DockerRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[1] == "rm":
            killed.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        raise subprocess.TimeoutExpired(argv, 1.0, output=b"partial", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = runner.run(("sleep", "100"), timeout=1)
    assert outcome.timed_out
    assert outcome.exit_code is None
    assert outcome.stdout == "partial"
    assert killed and "--force" in killed[0]


def test_cleanup_removes_containers_that_were_never_reaped(
    runner: DockerRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed: list[str] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **k: (
            removed.append(argv[-1]),
            subprocess.CompletedProcess(argv, 0, b"", b""),
        )[1],
    )
    runner._containers.append("rewire-stray")
    runner.cleanup()
    assert removed == ["rewire-stray"]
    assert runner._containers == []


# -------------------------------------------------------------- integration ---


@needs_docker
@pytest.mark.integration
@pytest.mark.slow
def test_the_isolation_boundary_actually_holds(tmp_path: Path) -> None:
    """Try to break out of each restriction, and confirm each attempt fails.

    Asserting that a flag is present in an argv proves only that Rewire meant
    to be isolated. This proves it is.
    """
    runner = DockerRunner(workspace=tmp_path, image=IMAGE)
    runner.preflight()
    runner.ensure_image()

    network = runner.run(
        ("python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), timeout=4)"),
        timeout=60,
    )
    assert network.exit_code != 0
    assert "unreachable" in network.output.lower() or "refused" in network.output.lower()

    outside = runner.run(("python3", "-c", "open('/usr/local/pwned', 'w')"), timeout=60)
    assert outside.exit_code != 0
    assert "read-only" in outside.output.lower()

    inside = runner.run(("python3", "-c", "open('/workspace/ok', 'w').write('x')"), timeout=60)
    assert inside.succeeded
    assert (tmp_path / "ok").is_file()

    # Written as the host user, or the temporary directory could not be removed.
    assert (tmp_path / "ok").stat().st_uid == Path.cwd().stat().st_uid


@needs_docker
@pytest.mark.integration
@pytest.mark.slow
def test_the_process_ceiling_stops_a_fork_bomb(tmp_path: Path) -> None:
    runner = DockerRunner(workspace=tmp_path, image=IMAGE, pids_limit=64)
    runner.ensure_image()
    outcome = runner.run(
        (
            "python3",
            "-c",
            "import os\nn = 0\ntry:\n"
            "    while n < 4000:\n"
            "        if os.fork() == 0: os._exit(0)\n"
            "        n += 1\n"
            "except OSError:\n    print('BLOCKED', n)\nelse:\n    print('SPAWNED', n)",
        ),
        timeout=120,
    )
    assert "BLOCKED" in outcome.output


@needs_docker
@pytest.mark.integration
@pytest.mark.slow
def test_a_runaway_command_is_killed(tmp_path: Path) -> None:
    runner = DockerRunner(workspace=tmp_path, image=IMAGE)
    runner.ensure_image()
    outcome = runner.run(("python3", "-c", "import time; time.sleep(600)"), timeout=5)
    assert outcome.timed_out
    runner.cleanup()
    surviving = subprocess.run(
        ["docker", "ps", "--filter", "label=rewire.sandbox=1", "-q"],  # noqa: S607 - from PATH
        capture_output=True,
        text=True,
        check=True,
    )
    assert surviving.stdout.strip() == ""


@needs_docker
@pytest.mark.integration
@pytest.mark.slow
def test_a_real_repository_is_verified_and_a_real_regression_is_caught(tmp_path: Path) -> None:
    """The end-to-end claim, executed: same repository, two patches, two verdicts."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['hatchling']\nbuild-backend = 'hatchling.build'\n\n"
        "[project]\nname = 'demo'\nversion = '0.1.0'\n\n"
        "[tool.hatch.build.targets.wheel]\npackages = ['demo']\n",
        encoding="utf-8",
    )
    (repo / "demo").mkdir()
    (repo / "demo" / "__init__.py").write_text(
        'def payload():\n    return {"max_tokens": 16}\n', encoding="utf-8"
    )
    (repo / "tests" / "test_demo.py").write_text(
        'from demo import payload\n\n\ndef test_field():\n    assert "max_tokens" in payload()\n',
        encoding="utf-8",
    )

    def patch_for(edits: list[FileEdit]):
        builder = PatchBuilder(read_file=lambda p: (repo / p).read_text(encoding="utf-8"))
        for edit in edits:
            builder.add(edit)
        return builder.build("rename the field")

    rename_source = FileEdit(
        file="demo/__init__.py", old_text='"max_tokens"', new_text='"max_completion_tokens"'
    )
    rename_test = FileEdit(
        file="tests/test_demo.py", old_text='"max_tokens"', new_text='"max_completion_tokens"'
    )
    request = VerificationRequest(check_timeout_seconds=300)

    incomplete = verify(repo, patch_for([rename_source]), request=request)
    assert incomplete.verdict is Verdict.REGRESSED
    assert incomplete.regressions == (CheckKind.TESTS,)
    assert incomplete.check(CheckKind.TESTS, patched=False).status is CheckStatus.PASSED
    assert incomplete.check(CheckKind.TESTS).status is CheckStatus.FAILED

    complete = verify(repo, patch_for([rename_source, rename_test]), request=request)
    assert complete.verdict is Verdict.VERIFIED
    assert complete.verified

    # The user's checkout is untouched by either run.
    assert '"max_tokens"' in (repo / "demo" / "__init__.py").read_text(encoding="utf-8")
