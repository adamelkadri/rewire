"""Typed application settings.

Settings are loaded from, in decreasing priority: explicit constructor
arguments, environment variables, a local ``.env`` file, then defaults. Every
variable is namespaced with the ``REWIRE_`` prefix; nested sections use a double
underscore, e.g. ``REWIRE_SANDBOX__MEMORY_LIMIT_MB=2048``.

Secrets are held as :class:`pydantic.SecretStr` so that they are redacted by
``repr``/``model_dump`` and can never be logged accidentally.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogFormat(StrEnum):
    """Rendering mode for the structured logger."""

    CONSOLE = "console"
    JSON = "json"


class LogLevel(StrEnum):
    """Supported log levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LLMSettings(BaseModel):
    """Configuration for the LLM provider abstraction.

    No provider-specific code lives outside ``rewire.llm``; this block only
    carries the values that a provider adapter needs to construct itself.
    """

    provider: Literal["anthropic", "openai", "openrouter", "null"] = "null"
    model: str = "claude-sonnet-5"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=8192, gt=0)
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None


class SandboxSettings(BaseModel):
    """Resource and isolation policy for sandboxed patch verification.

    These are hard limits enforced by the sandbox runner, not hints. They exist
    because Rewire executes code it did not write against repositories it does
    not trust.
    """

    backend: Literal["docker"] = "docker"
    image: str = "python:3.12-slim"
    timeout_seconds: int = Field(default=600, gt=0, le=7200)
    memory_limit_mb: int = Field(default=2048, ge=256)
    cpu_limit: float = Field(default=2.0, gt=0)
    pids_limit: int = Field(default=512, gt=0)
    network: Literal["none", "bridge"] = "none"
    read_only_rootfs: bool = True
    max_repo_size_mb: int = Field(default=512, gt=0)
    max_file_size_kb: int = Field(default=1024, gt=0)


class AgentSettings(BaseModel):
    """Budgets and guardrails for the migration agent loop."""

    max_iterations: int = Field(default=3, ge=1, le=10)
    max_tool_calls: int = Field(default=40, ge=1)
    max_tokens_per_task: int = Field(default=250_000, gt=0)
    max_files_per_patch: int = Field(default=25, ge=1)


class WatchSettings(BaseModel):
    """Network policy for following upstream specifications.

    A monitor polls something it does not control, unattended, on a schedule.
    Every value here is a ceiling on how much it will believe.
    """

    timeout_seconds: float = Field(default=30.0, gt=0)
    #: Largest response body accepted, matching the specification loader's cap.
    max_spec_mb: int = Field(default=32, gt=0)
    #: Permit plain HTTP, before and after redirects. Off, because a monitor
    #: that goes on to call a model and open a pull request should not trust an
    #: unauthenticated document over an unauthenticated channel.
    allow_http: bool = False

    @property
    def max_spec_bytes(self) -> int:
        """The body ceiling in bytes."""
        return self.max_spec_mb * 1024 * 1024


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_prefix="REWIRE_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
    )

    environment: Literal["local", "ci", "production"] = "local"
    debug: bool = False

    log_level: LogLevel = LogLevel.INFO
    log_format: LogFormat = LogFormat.CONSOLE

    #: Root for run artefacts: indexes, agent traces, patches, eval results.
    data_dir: Path = Path(".rewire")

    #: SQLAlchemy URL. SQLite by default so the MVP has no infra dependency;
    #: models are written to be Postgres-compatible.
    database_url: str = "sqlite+aiosqlite:///.rewire/rewire.db"

    github_token: SecretStr | None = None

    llm: LLMSettings = Field(default_factory=LLMSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    watch: WatchSettings = Field(default_factory=WatchSettings)

    @field_validator("data_dir")
    @classmethod
    def _expand_data_dir(cls, value: Path) -> Path:
        return value.expanduser()

    @property
    def index_dir(self) -> Path:
        """Directory holding cached repository indexes."""
        return self.data_dir / "index"

    @property
    def runs_dir(self) -> Path:
        """Directory holding per-migration run artefacts and agent traces."""
        return self.data_dir / "runs"

    @property
    def watch_dir(self) -> Path:
        """Directory holding watch declarations, baselines and check state."""
        return self.data_dir / "watch"

    @property
    def jobs_path(self) -> Path:
        """The queue database.

        A file rather than ``database_url``, which has described a SQLAlchemy
        setup this project never had — see ADR-065.
        """
        return self.data_dir / "jobs.db"

    def ensure_data_dirs(self) -> None:
        """Create the data directories if they do not already exist."""
        for directory in (self.data_dir, self.index_dir, self.runs_dir, self.watch_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that repeated access is cheap and consistent. Tests that need a
    different configuration should construct :class:`Settings` directly, or call
    ``get_settings.cache_clear()``.
    """
    return Settings()


__all__ = [
    "AgentSettings",
    "LLMSettings",
    "LogFormat",
    "LogLevel",
    "SandboxSettings",
    "Settings",
    "WatchSettings",
    "get_settings",
]
