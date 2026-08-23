"""A runner that replays canned outcomes instead of starting containers.

The verification logic — baseline, apply, re-run, compare — is where the
interesting mistakes live, and it has nothing to do with Docker. Testing it
through real containers would make the suite slow, host-dependent and quietly
skipped in CI, which is how verification logic ends up unverified.

This runner makes the whole pipeline exercisable offline and records every
command it was asked to run, so tests can assert on isolation policy (that
checks ran with the network off, for instance) rather than trusting it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from rewire.sandbox.models import CommandOutcome
from rewire.sandbox.runner import Network

#: Signature of a rule: given the command, return an outcome or ``None`` to
#: decline and let the next rule try.
Rule = Callable[[tuple[str, ...]], CommandOutcome | None]


@dataclass(frozen=True, slots=True)
class RecordedCommand:
    """One command the runner was asked to execute."""

    command: tuple[str, ...]
    network: Network
    timeout: float


@dataclass(slots=True)
class ScriptedRunner:
    """Returns pre-arranged outcomes and records what it was asked to do."""

    rules: list[Rule] = field(default_factory=list)
    #: Outcome for a command no rule matches. Success by default, so a test
    #: only has to describe the failures it cares about.
    default_exit_code: int = 0
    calls: list[RecordedCommand] = field(default_factory=list)

    def run(
        self, command: tuple[str, ...], *, timeout: float, network: Network = "none"
    ) -> CommandOutcome:
        """Return the first matching scripted outcome, or the default."""
        self.calls.append(RecordedCommand(command=command, network=network, timeout=timeout))
        for rule in self.rules:
            outcome = rule(command)
            if outcome is not None:
                return outcome
        return CommandOutcome(command=command, exit_code=self.default_exit_code)

    # ------------------------------------------------------- scripting ---

    def when(
        self,
        contains: str,
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        times: int | None = None,
    ) -> ScriptedRunner:
        """Script an outcome for commands containing ``contains``.

        Note that matching is a plain substring test over the whole command
        line, so ``"pytest"`` also matches the ``pip install ... pytest``
        step. Target a check with something only it contains, such as
        ``"-m pytest"``.

        Args:
            contains: Matched against the joined command line.
            exit_code: Exit status the scripted command reports.
            stdout: Standard output the scripted command produces.
            stderr: Standard error the scripted command produces.
            timed_out: Report the command as killed by its time budget.
            times: Apply only to the first ``times`` matches, so a command can
                behave differently before and after the patch is applied.
        """
        remaining = times

        def rule(command: tuple[str, ...]) -> CommandOutcome | None:
            nonlocal remaining
            if contains not in " ".join(command):
                return None
            if remaining is not None:
                if remaining <= 0:
                    return None
                remaining -= 1
            return CommandOutcome(
                command=command,
                exit_code=None if timed_out else exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
            )

        self.rules.append(rule)
        return self

    def commands(self) -> list[str]:
        """Every command line seen so far, joined for readable assertions."""
        return [" ".join(call.command) for call in self.calls]


__all__ = ["RecordedCommand", "Rule", "ScriptedRunner"]
