"""Sandboxed verification: run a repository's own checks against a patch.

The sandbox is what separates Rewire from an agent that merely sounds
confident. A patch leaves the agent as a *candidate*; it becomes *verified*
only by surviving the repository's own test suite, linter and type checker,
executed twice — before and after — inside a container with no network, no
privileges and hard resource limits.
"""

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
from rewire.sandbox.scripted import ScriptedRunner
from rewire.sandbox.staging import StagedRepository, stage_repository
from rewire.sandbox.toolchain import Check, ToolchainPlan, plan_checks
from rewire.sandbox.verifier import verify

__all__ = [
    "Check",
    "CheckKind",
    "CheckResult",
    "CheckStatus",
    "CommandOutcome",
    "DockerRunner",
    "SandboxRunner",
    "ScriptedRunner",
    "StagedRepository",
    "ToolchainPlan",
    "Verdict",
    "VerificationReport",
    "VerificationRequest",
    "plan_checks",
    "stage_repository",
    "verify",
]
