"""Executing commands inside a disposable, locked-down container.

Rewire runs code it did not write, from repositories it does not trust, having
been edited by a language model that read those same repositories. The
container is where that risk is contained, so its configuration is not a
convenience — it is the security boundary, and every flag below is load-bearing:

* **No network.** Checks run with ``--network none``. The single exception is
  the install step, which cannot work offline; it is opt-out, time-boxed, and
  reported separately in the verification report so that the one moment the
  sandbox touches the network is visible rather than implied.
* **No privileges.** All capabilities dropped, ``no-new-privileges`` set, and
  the process runs as the invoking user rather than root so that files written
  into the bind mount stay owned by the host user and can be deleted.
* **Read-only root filesystem**, with the staged repository as the only
  writable path and a size-capped ``tmpfs`` for ``/tmp``.
* **Hard resource ceilings** on memory, CPU and process count, so a fork bomb
  or a runaway allocation in a test suite costs a container rather than a host.
* **A wall-clock timeout** enforced by the host, followed by ``docker rm -f``.
  A container that ignores its own limits is still killed.

Nothing here trusts the container to stop on its own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from rewire.core.errors import SandboxError, ToolchainError
from rewire.core.logging import get_logger
from rewire.sandbox.models import CommandOutcome, truncate_output
from rewire.sandbox.toolchain import CONTAINER_PATH, WORKSPACE_DIR

logger = get_logger(__name__)

Network = Literal["none", "bridge"]

#: Environment given to every container. ``HOME`` points at the tmpfs because
#: the root filesystem is read only and pip insists on a home directory.
CONTAINER_ENV: Final[dict[str, str]] = {
    "PATH": CONTAINER_PATH,
    # Paths inside the container, not on the host: the root filesystem is
    # read only, so the tmpfs is the only place pip and pytest can write.
    "HOME": "/tmp",  # noqa: S108
    "TMPDIR": "/tmp",  # noqa: S108
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_CACHE_DIR": "1",
    # Keep check output stable and free of terminal escapes, which would
    # otherwise end up in stored reports and diffs.
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "100",
}

#: Grace period added to the host-side timeout before the container is killed.
KILL_GRACE_SECONDS: Final[float] = 5.0


class SandboxRunner(Protocol):
    """Anything that can execute a command on behalf of the verifier.

    Declared as a protocol so that the verification logic can be tested without
    Docker, and so that a future backend (a remote executor, a VM) is a drop-in
    replacement rather than a rewrite.
    """

    def run(
        self, command: tuple[str, ...], *, timeout: float, network: Network = "none"
    ) -> CommandOutcome:
        """Run one command and return what happened. Never raises on non-zero exit."""
        ...  # pragma: no cover - protocol declaration


@dataclass(slots=True)
class DockerRunner:
    """Runs commands in a fresh container per command.

    A container per command rather than a long-lived one: state that survives
    between checks is state that can make a later check pass for reasons the
    report cannot explain. The staged repository is the only thing deliberately
    shared, and it is shared because the install step has to produce something
    the check steps can use.
    """

    workspace: Path
    image: str
    memory_limit_mb: int = 2048
    cpu_limit: float = 2.0
    pids_limit: int = 512
    read_only_rootfs: bool = True
    #: Set for the lifetime of a run so that stray containers can be found.
    label: str = "rewire.sandbox=1"
    docker: str = "docker"
    commands_run: int = 0
    _containers: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- setup ---

    def preflight(self) -> None:
        """Fail early and clearly if Docker cannot be used.

        Raises:
            ToolchainError: The client is missing or the daemon is unreachable.
        """
        if shutil.which(self.docker) is None:
            raise ToolchainError("docker was not found on PATH", tool=self.docker)
        probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [self.docker, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode != 0:
            raise ToolchainError(
                "cannot connect to the Docker daemon; is Docker running?",
                detail=probe.stderr.strip()[:300],
            )

    def ensure_image(self) -> None:
        """Pull the sandbox image if it is not already present.

        Done once and up front rather than lazily: a pull inside a timed check
        would count minutes of download against the check's budget and make a
        slow network look like a slow test suite.

        Raises:
            SandboxError: The image is absent and cannot be pulled.
        """
        present = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [self.docker, "image", "inspect", self.image],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if present.returncode == 0:
            return

        logger.info("sandbox.image.pull", image=self.image)
        pull = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [self.docker, "pull", self.image],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if pull.returncode != 0:
            raise SandboxError(
                "could not pull the sandbox image",
                image=self.image,
                detail=pull.stderr.strip()[:300],
            )

    # ----------------------------------------------------------- running ---

    def _docker_argv(self, name: str, network: Network) -> list[str]:
        """Assemble the ``docker run`` flags that form the security boundary."""
        argv = [
            self.docker,
            "run",
            "--rm",
            "--name",
            name,
            "--label",
            self.label,
            "--network",
            network,
            "--memory",
            f"{self.memory_limit_mb}m",
            # Equal to --memory so the container cannot escape the ceiling by
            # swapping; without it the limit is advisory on many hosts.
            "--memory-swap",
            f"{self.memory_limit_mb}m",
            "--cpus",
            str(self.cpu_limit),
            "--pids-limit",
            str(self.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=512m",  # noqa: S108 - container path
            "--workdir",
            WORKSPACE_DIR,
            "--volume",
            f"{self.workspace}:{WORKSPACE_DIR}",
        ]
        if self.read_only_rootfs:
            argv.append("--read-only")
        if (user := _host_user()) is not None:
            # Run as the invoking user so that files created in the bind mount
            # are owned by the host user; otherwise cleanup of a root-owned
            # virtual environment fails and the temporary directory leaks.
            argv.extend(["--user", user])
        for key, value in CONTAINER_ENV.items():
            argv.extend(["--env", f"{key}={value}"])
        argv.append(self.image)
        return argv

    def run(
        self, command: tuple[str, ...], *, timeout: float, network: Network = "none"
    ) -> CommandOutcome:
        """Run one command in a fresh container.

        A non-zero exit is data, not an error: it is the whole point of running
        the check. Only a failure of the sandbox itself raises.

        Raises:
            SandboxError: Docker could not be executed at all.
        """
        name = f"rewire-{uuid.uuid4().hex[:12]}"
        argv = [*self._docker_argv(name, network), *command]
        self._containers.append(name)
        self.commands_run += 1

        logger.debug("sandbox.command", command=list(command), network=network, timeout=timeout)
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - argv is built here, no shell
                argv,
                capture_output=True,
                timeout=timeout + KILL_GRACE_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as expired:
            self._kill(name)
            stdout, truncated = truncate_output(_decode(expired.stdout))
            stderr, _ = truncate_output(_decode(expired.stderr))
            return CommandOutcome(
                command=command,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=round(time.monotonic() - started, 3),
                timed_out=True,
                truncated=truncated,
            )
        except OSError as exc:
            raise SandboxError(f"could not execute docker: {exc}", command=list(command)) from exc
        finally:
            if name in self._containers:
                self._containers.remove(name)

        stdout, cut_out = truncate_output(completed.stdout.decode("utf-8", errors="replace"))
        stderr, cut_err = truncate_output(completed.stderr.decode("utf-8", errors="replace"))
        return CommandOutcome(
            command=command,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=round(time.monotonic() - started, 3),
            truncated=cut_out or cut_err,
        )

    def _kill(self, name: str) -> None:
        """Remove a container that outlived its budget, ignoring failure."""
        try:
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                [self.docker, "rm", "--force", name],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - best effort
            logger.warning("sandbox.kill.failed", container=name)

    def cleanup(self) -> None:
        """Force-remove any container this runner started and did not reap."""
        for name in list(self._containers):
            self._kill(name)
        self._containers.clear()


def _decode(raw: bytes | str | None) -> str:
    """Decode partial output captured from a killed process."""
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")


def _host_user() -> str | None:
    """Return ``uid:gid`` for the current user, or ``None`` where that has no meaning."""
    if not hasattr(os, "getuid"):  # pragma: no cover - Windows only
        return None
    return f"{os.getuid()}:{os.getgid()}"


__all__ = [
    "CONTAINER_ENV",
    "KILL_GRACE_SECONDS",
    "DockerRunner",
    "Network",
    "SandboxRunner",
]
