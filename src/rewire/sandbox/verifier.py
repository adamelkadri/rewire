"""Turning a candidate patch into evidence, or into a reason it has none.

This module is the answer to the question the rest of Rewire exists to make
answerable: *did the patch work?* The answer is never the agent's opinion. It
is a set of exit codes from commands that ran twice — once before the patch and
once after — inside a container that could not reach the network.

The two-run design is the important part. Running the checks only after the
patch conflates "the agent broke this" with "this was already broken", and a
migration benchmark built on that conflation reports whatever the sample
repositories happened to be doing. Rewire compares against the baseline it
measured minutes earlier on the same machine in the same image, so a regression
means the patch caused it.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from rewire.agents.patch import CandidatePatch, write_patch
from rewire.core.errors import PatchError, SandboxError
from rewire.core.logging import get_logger
from rewire.sandbox.models import (
    CheckKind,
    CheckResult,
    CheckStatus,
    CommandOutcome,
    Verdict,
    VerificationReport,
    VerificationRequest,
)
from rewire.sandbox.runner import DockerRunner, SandboxRunner
from rewire.sandbox.staging import stage_repository
from rewire.sandbox.toolchain import Check, ToolchainPlan, plan_checks

logger = get_logger(__name__)

#: Builds the runner for a staged repository. Injected so that the pipeline can
#: be tested end to end without Docker.
RunnerFactory = Callable[[Path, VerificationRequest], SandboxRunner]

#: Exit code meaning "the interpreter could not find the tool at all".
EXIT_COMMAND_NOT_FOUND = 127

#: pytest's exit code for a successful run that collected nothing. Not a pass:
#: it is the absence of tests wearing a zero-adjacent exit code.
PYTEST_EXIT_NO_TESTS = 5

#: Substrings that mean the tool is missing rather than the code being wrong.
_MISSING_TOOL_MARKERS = (
    "No module named",
    "command not found",
    "executable file not found",
    "not found in $PATH",
)


def classify(check: Check, outcome: CommandOutcome) -> tuple[CheckStatus, str]:
    """Map a command outcome onto a status and a human-readable reason.

    The care here is entirely about not turning "we learned nothing" into
    "it failed" or, far worse, into "it passed".
    """
    if outcome.timed_out:
        return CheckStatus.TIMED_OUT, "exceeded its time budget and was killed"
    if outcome.exit_code == 0:
        return CheckStatus.PASSED, check.reason
    if outcome.exit_code == EXIT_COMMAND_NOT_FOUND or any(
        marker in outcome.output for marker in _MISSING_TOOL_MARKERS
    ):
        return CheckStatus.UNAVAILABLE, f"{check.name} is not installed in the sandbox image"
    if check.name == "pytest" and outcome.exit_code == PYTEST_EXIT_NO_TESTS:
        return CheckStatus.SKIPPED, "pytest collected no tests"
    return CheckStatus.FAILED, f"{check.name} exited {outcome.exit_code}"


def _run_checks(runner: SandboxRunner, plan: ToolchainPlan, timeout: float) -> list[CheckResult]:
    """Run every planned check with the network off, and record each outcome."""
    results: list[CheckResult] = []
    for check in plan.checks:
        outcome = runner.run(check.command, timeout=timeout, network="none")
        status, reason = classify(check, outcome)
        logger.debug("sandbox.check", check=check.name, status=status.value)
        results.append(
            CheckResult(
                kind=check.kind, name=check.name, status=status, outcome=outcome, reason=reason
            )
        )
    return results


def _install(
    runner: SandboxRunner, plan: ToolchainPlan, request: VerificationRequest
) -> CheckResult | None:
    """Install the project and its check tools, the one step allowed the network.

    Returns ``None`` when there is nothing to install, which leaves the entire
    run offline. A failure here is reported rather than raised: the checks that
    need no dependencies still produce evidence, and the ones that do will
    report themselves unavailable, which is the truthful outcome.
    """
    if not plan.install or not request.install:
        return None

    last: CommandOutcome | None = None
    for step in plan.install:
        # Installing a repository executes its build backend. That is why this
        # step runs under exactly the same confinement as everything else, with
        # network access as the single relaxation.
        last = runner.run(step.command, timeout=request.install_timeout_seconds, network="bridge")
        if not last.succeeded:
            status, reason = classify(step, last)
            return CheckResult(
                kind=CheckKind.INSTALL,
                name=step.name,
                status=CheckStatus.FAILED if status is CheckStatus.PASSED else status,
                outcome=last,
                reason=f"dependency installation failed: {reason}",
            )

    return CheckResult(
        kind=CheckKind.INSTALL,
        name="pip",
        status=CheckStatus.PASSED,
        outcome=last,
        reason="project and check tools installed",
    )


def _assert_patch_matches(root: Path, patch: CandidatePatch) -> None:
    """Refuse to apply a patch whose assumptions no longer hold.

    The agent proposed edits against the content it read. If the staged copy
    differs — because the working tree changed, or because the patch was saved
    and replayed later — writing the ``after`` text would silently discard
    whatever else is in the file.

    Raises:
        SandboxError: A file's current content is not what the patch expects.
    """
    for change in patch.changes:
        if not change.changed:
            continue
        target = root / change.file
        current = target.read_bytes().decode("utf-8", errors="replace") if target.is_file() else ""
        if current != change.before:
            raise SandboxError(
                "the file changed since the patch was proposed; refusing to apply it",
                file=change.file,
            )


def decide(
    baseline: list[CheckResult], patched: list[CheckResult], *, install: CheckResult | None
) -> tuple[Verdict, str, tuple[CheckKind, ...], tuple[CheckKind, ...]]:
    """Compare the two runs and reach a verdict.

    Returns the verdict, its justification, the regressions, and the failures
    that predate the patch.
    """
    before = {result.kind: result for result in baseline}
    after = {result.kind: result for result in patched}

    regressions = tuple(
        kind
        for kind, result in sorted(before.items())
        if result.passed and not after.get(kind, result).passed
    )
    pre_existing = tuple(
        kind for kind, result in sorted(before.items()) if result.status is CheckStatus.FAILED
    )

    if regressions:
        names = ", ".join(kind.value for kind in regressions)
        return (
            Verdict.REGRESSED,
            f"the patch broke checks that passed before it: {names}",
            regressions,
            pre_existing,
        )

    tests = after.get(CheckKind.TESTS)
    if tests is not None and tests.passed:
        return (
            Verdict.VERIFIED,
            "the test suite passed after the patch and no check regressed",
            (),
            pre_existing,
        )

    if tests is None:
        detail = "the repository has no test suite, so nothing could confirm the patch behaves"
    elif tests.status is CheckStatus.UNAVAILABLE:
        detail = "pytest is not available in the sandbox, so the tests were never run"
    elif tests.status is CheckStatus.TIMED_OUT:
        detail = "the test suite exceeded its time budget, so its result is unknown"
    elif tests.status is CheckStatus.SKIPPED:
        detail = "pytest collected no tests, so nothing was exercised"
    else:
        detail = (
            "the test suite was already failing before the patch, so it cannot confirm anything"
        )

    if install is not None and not install.passed:
        detail = f"{detail} ({install.reason})"
    return Verdict.INCONCLUSIVE, detail, (), pre_existing


def _docker_factory(root: Path, request: VerificationRequest) -> SandboxRunner:
    """Default runner factory: a Docker container per command."""
    runner = DockerRunner(
        workspace=root,
        image=request.image,
        memory_limit_mb=request.memory_limit_mb,
        cpu_limit=request.cpu_limit,
        pids_limit=request.pids_limit,
        read_only_rootfs=request.read_only_rootfs,
    )
    runner.preflight()
    runner.ensure_image()
    return runner


def verify(
    repository: Path | str,
    patch: CandidatePatch | None = None,
    *,
    request: VerificationRequest | None = None,
    runner_factory: RunnerFactory | None = None,
) -> VerificationReport:
    """Measure a repository, apply a patch, measure it again, and compare.

    Passing ``patch=None`` performs the baseline measurement alone, which is
    how a repository's verifiability is inspected before an agent is ever
    involved.

    Args:
        repository: The repository to verify. Never modified.
        patch: The candidate to apply inside the sandbox copy.
        request: Isolation and resource policy.
        runner_factory: Builds the execution backend; defaults to Docker.

    Raises:
        SandboxError: Staging failed, the sandbox could not run, or the patch
            no longer matches the repository it was proposed against.
        ToolchainError: Docker is unavailable.
    """
    settings = request or VerificationRequest()
    factory = runner_factory or _docker_factory
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="rewire-sandbox-") as scratch:
        staged = stage_repository(
            repository, Path(scratch) / "repo", max_bytes=settings.max_repo_size_mb * 1024 * 1024
        )
        plan = plan_checks(staged.root)
        runner = factory(staged.root, settings)
        logger.info(
            "sandbox.start",
            repository=str(repository),
            files=staged.files,
            checks=[check.name for check in plan.checks],
        )

        try:
            install = _install(runner, plan, settings)
            baseline = _run_checks(runner, plan, settings.check_timeout_seconds)

            if patch is None:
                return _report(
                    Verdict.INCONCLUSIVE,
                    "baseline measurement only; no patch was supplied",
                    baseline=baseline,
                    patched=[],
                    plan=plan,
                    install=install,
                    settings=settings,
                    files=(),
                    started=started,
                )

            if patch.is_empty:
                return _report(
                    Verdict.INCONCLUSIVE,
                    "the patch changes nothing, so there is nothing to verify",
                    baseline=baseline,
                    patched=[],
                    plan=plan,
                    install=install,
                    settings=settings,
                    files=(),
                    started=started,
                )

            _assert_patch_matches(staged.root, patch)
            try:
                written = write_patch(patch, staged.root)
            except PatchError as exc:
                return _report(
                    Verdict.ERRORED,
                    f"the patch could not be applied: {exc}",
                    baseline=baseline,
                    patched=[],
                    plan=plan,
                    install=install,
                    settings=settings,
                    files=(),
                    started=started,
                )

            patched = _run_checks(runner, plan, settings.check_timeout_seconds)
            verdict, reason, regressions, pre_existing = decide(baseline, patched, install=install)
            return _report(
                verdict,
                reason,
                baseline=baseline,
                patched=patched,
                plan=plan,
                install=install,
                settings=settings,
                files=tuple(written),
                started=started,
                regressions=regressions,
                pre_existing=pre_existing,
            )
        finally:
            if isinstance(runner, DockerRunner):
                runner.cleanup()


def _report(
    verdict: Verdict,
    reason: str,
    *,
    baseline: list[CheckResult],
    patched: list[CheckResult],
    plan: ToolchainPlan,
    install: CheckResult | None,
    settings: VerificationRequest,
    files: tuple[str, ...],
    started: float,
    regressions: tuple[CheckKind, ...] = (),
    pre_existing: tuple[CheckKind, ...] = (),
) -> VerificationReport:
    """Assemble a report, folding in the checks that were never run."""
    return VerificationReport(
        verdict=verdict,
        reason=reason,
        baseline=(*baseline, *plan.skipped),
        patched=(*patched, *plan.skipped) if patched else (),
        regressions=regressions,
        pre_existing_failures=pre_existing,
        install=install,
        image=settings.image,
        files_changed=files,
        duration_seconds=round(time.monotonic() - started, 2),
    )


__all__ = [
    "EXIT_COMMAND_NOT_FOUND",
    "PYTEST_EXIT_NO_TESTS",
    "RunnerFactory",
    "classify",
    "decide",
    "verify",
]
