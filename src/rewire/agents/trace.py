"""Recording everything a migration run did.

An agent run that cannot be inspected afterwards cannot be debugged, compared or
believed. Every model request, tool call, state change and token is written to a
JSONL trace, which is what Phases 8 through 10 read to produce success rates,
cost per task and ablation results.

Nothing here writes a credential. Traces record prompts and repository content,
which is why they live under the run directory rather than anywhere shared, and
why the API key never appears in a prompt in the first place.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rewire.agents.state import AgentState
from rewire.core.errors import AgentError
from rewire.llm.models import Usage


class EventType(StrEnum):
    """What kind of thing happened."""

    RUN_STARTED = "run_started"
    STATE_CHANGED = "state_changed"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    EDIT_APPLIED = "edit_applied"
    BUDGET_EXCEEDED = "budget_exceeded"
    RUN_FINISHED = "run_finished"
    #: A retry seeded with a previous attempt's sandbox result.
    REPAIR_FEEDBACK = "repair_feedback"
    #: The sandbox's verdict on one attempt's patch.
    VERIFICATION = "verification"


class TraceEvent(BaseModel):
    """One recorded event."""

    model_config = ConfigDict(frozen=True)

    sequence: int
    type: EventType
    timestamp: str
    state: AgentState
    #: Structured payload. Contents vary by event type and are never parsed
    #: back into behaviour -- the trace is an observation, not a control path.
    data: dict[str, Any] = Field(default_factory=dict)


class RunSummary(BaseModel):
    """Aggregate facts about a finished run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    repository: str
    provider: str = ""
    model: str = ""
    final_state: AgentState = AgentState.FAILED
    iterations: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    files_changed: int = 0
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None
    duration_seconds: float = 0.0
    #: Why the run stopped. Always populated, including on success.
    outcome: str = ""

    @property
    def produced_patch(self) -> bool:
        """Whether the run ended with a candidate patch.

        Note the name: a patch was produced, not verified. Nothing in Phase 4
        can justify a stronger claim.
        """
        return self.final_state is AgentState.CANDIDATE


class RunTrace:
    """Collects events for one run and writes them to disk."""

    def __init__(self, run_id: str, directory: Path | None = None) -> None:
        self.run_id = run_id
        self.directory = directory
        self.events: list[TraceEvent] = []
        self._sequence = 0
        self._handle = None
        if directory is not None:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                self._handle = (directory / "trace.jsonl").open("a", encoding="utf-8")
            except OSError as exc:
                raise AgentError(
                    f"could not open run trace: {exc}", directory=str(directory)
                ) from exc

    def record(self, event_type: EventType, state: AgentState, **data: Any) -> TraceEvent:
        """Append an event and flush it to disk immediately.

        Flushed per event rather than at the end so that a run killed mid-flight
        still leaves a usable trace -- which is exactly when one is wanted.
        """
        self._sequence += 1
        event = TraceEvent(
            sequence=self._sequence,
            type=event_type,
            timestamp=datetime.now(UTC).isoformat(),
            state=state,
            data=data,
        )
        self.events.append(event)
        if self._handle is not None:
            self._handle.write(event.model_dump_json() + "\n")
            self._handle.flush()
        return event

    def of_type(self, event_type: EventType) -> list[TraceEvent]:
        """Every recorded event of one type."""
        return [event for event in self.events if event.type is event_type]

    def write_summary(self, summary: RunSummary) -> Path | None:
        """Write the run summary alongside the event log."""
        if self.directory is None:
            return None
        path = self.directory / "summary.json"
        path.write_text(
            json.dumps(summary.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
        return path

    def close(self) -> None:
        """Close the trace file, if one is open."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> RunTrace:
        """Enter a context that closes the trace on exit."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the trace."""
        self.close()


def load_trace(path: Path | str) -> list[TraceEvent]:
    """Read a trace back from a JSONL file.

    Malformed lines are skipped rather than fatal: a trace truncated by a killed
    process is still worth reading.
    """
    events: list[TraceEvent] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(TraceEvent.model_validate_json(line))
        except ValueError:
            continue
    return events


__all__ = ["EventType", "RunSummary", "RunTrace", "TraceEvent", "load_trace"]
