"""Domain exception hierarchy for Rewire.

Every failure Rewire can produce should be expressible as a :class:`RewireError`
subclass. Callers (the CLI, the future HTTP API, the agent loop) match on these
types rather than on strings, and each error carries a stable ``code`` so that
failures stay machine-readable across the CLI/API boundary.
"""

from __future__ import annotations

from typing import Any


class RewireError(Exception):
    """Base class for all errors raised by Rewire.

    Attributes:
        code: Stable, machine-readable identifier for the failure class.
        message: Human-readable description of what went wrong.
        details: Structured, non-secret context about the failure.
    """

    code: str = "rewire_error"

    def __init__(self, message: str, /, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def __str__(self) -> str:
        """Render the message with any structured details appended."""
        if not self.details:
            return self.message
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(self.details.items()))
        return f"{self.message} ({rendered})"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the error."""
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigurationError(RewireError):
    """Rewire is misconfigured: bad settings, missing credentials, bad paths."""

    code = "configuration_error"


class ToolchainError(RewireError):
    """A required external tool (Git, Docker, ripgrep) is missing or unusable."""

    code = "toolchain_error"


class SpecParseError(RewireError):
    """An API specification could not be parsed."""

    code = "spec_parse_error"


class RepositoryError(RewireError):
    """A repository could not be read, indexed or is otherwise unusable."""

    code = "repository_error"


class AnalysisError(RewireError):
    """Static analysis failed in a way that is not attributable to user input."""

    code = "analysis_error"


class LLMError(RewireError):
    """An LLM provider call failed."""

    code = "llm_error"


class LLMRateLimitError(LLMError):
    """An LLM provider rejected the request because of rate limiting."""

    code = "llm_rate_limit_error"


class AgentError(RewireError):
    """The agent loop failed or exhausted its budget without a result."""

    code = "agent_error"


class SandboxError(RewireError):
    """Sandboxed execution failed to start, run or clean up."""

    code = "sandbox_error"


class SandboxTimeoutError(SandboxError):
    """Sandboxed execution exceeded its wall-clock budget."""

    code = "sandbox_timeout_error"


class PatchError(RewireError):
    """A patch could not be generated, parsed or applied."""

    code = "patch_error"


class GitError(RewireError):
    """A Git operation failed."""

    code = "git_error"


class EvaluationError(RewireError):
    """An evaluation task or runner failed."""

    code = "evaluation_error"


__all__ = [
    "AgentError",
    "AnalysisError",
    "ConfigurationError",
    "EvaluationError",
    "GitError",
    "LLMError",
    "LLMRateLimitError",
    "PatchError",
    "RepositoryError",
    "RewireError",
    "SandboxError",
    "SandboxTimeoutError",
    "SpecParseError",
    "ToolchainError",
]
