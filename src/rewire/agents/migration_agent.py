"""The migration agent loop.

Drives a model through: read the deterministic analysis, inspect the code, stage
edits, review, stop. Every request, tool call and state change is traced.

The loop is written by hand rather than delegated to an SDK helper because the
things that matter here are exactly the things a generic helper does not do:
a state machine with legal transitions, hard budgets on iterations, tool calls
and tokens, per-event tracing, and a terminal state that means *proposed* rather
than *succeeded*.

The agent cannot mark its own work successful. The strongest outcome available
is :attr:`~rewire.agents.state.AgentState.CANDIDATE` -- a patch that exists.
Whether it is correct is decided in Phase 5 by running it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from rewire.agents.patch import CandidatePatch, PatchBuilder
from rewire.agents.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_review_nudge,
    build_task_prompt,
    wrap_untrusted,
)
from rewire.agents.state import AgentState, can_transition, is_terminal
from rewire.agents.tools import ToolContext, invoke, tool_specs
from rewire.agents.trace import EventType, RunSummary, RunTrace
from rewire.agents.workspace import Workspace
from rewire.analyzers.models import RepositoryIndex
from rewire.changes.models import ChangeReport
from rewire.core.errors import AgentError, LLMError
from rewire.core.logging import get_logger
from rewire.impact.models import ImpactReport
from rewire.llm.base import LLMProvider
from rewire.llm.models import LLMResponse, Message, Usage

logger = get_logger(__name__)

#: Tools whose use means the agent has moved on to editing.
_EDIT_TOOLS = frozenset({"propose_edit"})
_REVIEW_TOOLS = frozenset({"show_diff"})


@dataclass(slots=True)
class AgentBudget:
    """Hard limits on one run.

    Every one is a stop condition, not a hint. An agent that cannot finish
    within them produces no patch, which is a better outcome than an unbounded
    bill.
    """

    max_iterations: int = 12
    max_tool_calls: int = 40
    max_tokens: int = 250_000
    max_files: int = 25
    max_output_tokens: int = 8192


@dataclass(slots=True)
class MigrationResult:
    """What a run produced."""

    summary: RunSummary
    patch: CandidatePatch
    trace: RunTrace
    #: The agent's closing message, if it wrote one.
    final_message: str = ""

    @property
    def verified(self) -> bool:
        """Always ``False``: an agent run is a proposal, never a conclusion.

        Verification is a separate step with separate evidence -- see
        :func:`rewire.sandbox.verify`, whose
        :class:`~rewire.sandbox.VerificationReport` is the only thing entitled
        to say a patch works. This property stays false so that a caller holding
        a :class:`MigrationResult` cannot mistake a produced patch for a working
        one.
        """
        return False


class MigrationAgent:
    """Runs one migration attempt against one repository."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        budget: AgentBudget | None = None,
        runs_dir: Path | None = None,
    ) -> None:
        self._provider = provider
        self._budget = budget or AgentBudget()
        self._runs_dir = runs_dir

    @property
    def runs_dir(self) -> Path | None:
        """Where per-run traces are written, if anywhere."""
        return self._runs_dir

    def run(
        self,
        *,
        workspace: Workspace,
        index: RepositoryIndex,
        changes: ChangeReport,
        impact: ImpactReport,
        run_id: str | None = None,
        feedback: str = "",
    ) -> MigrationResult:
        """Attempt a migration and return the candidate patch.

        Args:
            workspace: The repository, opened read-only.
            index: Its parsed symbol and reference index.
            changes: The detected API changes.
            impact: The changes joined to the code they affect.
            run_id: Identifier for the trace directory; generated if absent.
            feedback: Result of a previous attempt, from
                :func:`~rewire.agents.prompts.build_repair_prompt`. Sent as a
                second opening message so the agent sees the task and the
                failure together. The patch builder is still fresh, so the
                agent restages every edit rather than patching a patch.

        Raises:
            AgentError: The run could not start. Failures *during* the run end
                in :attr:`AgentState.FAILED` with a trace rather than raising,
                because a failed run's trace is the useful artefact.
        """
        identifier = run_id or uuid.uuid4().hex[:12]
        directory = self._runs_dir / identifier if self._runs_dir else None
        started = time.perf_counter()

        builder = PatchBuilder(read_file=workspace.read_full)
        context = ToolContext(
            workspace=workspace, index=index, changes=changes, impact=impact, patch=builder
        )

        state = AgentState.ANALYZE
        usage = Usage()
        cost: float | None = 0.0
        tool_calls = 0
        tool_errors = 0
        iterations = 0
        outcome = ""
        final_message = ""
        nudged = False

        with RunTrace(identifier, directory) as trace:
            trace.record(
                EventType.RUN_STARTED,
                state,
                run_id=identifier,
                repository=str(workspace.root),
                provider=self._provider.name,
                model=self._provider.model,
                prompt_version=PROMPT_VERSION,
                changes=changes.summary.total,
                affected_locations=impact.summary.locations,
            )

            messages: list[Message] = [Message.user(build_task_prompt(changes, impact))]
            if feedback:
                messages.append(Message.user(feedback))
                trace.record(EventType.REPAIR_FEEDBACK, state, feedback_chars=len(feedback))

            while True:
                if iterations >= self._budget.max_iterations:
                    outcome = f"iteration budget exhausted ({self._budget.max_iterations})"
                    trace.record(EventType.BUDGET_EXCEEDED, state, limit="iterations")
                    state = self._transition(trace, state, AgentState.FAILED)
                    break
                if usage.total_tokens >= self._budget.max_tokens:
                    outcome = f"token budget exhausted ({self._budget.max_tokens})"
                    trace.record(EventType.BUDGET_EXCEEDED, state, limit="tokens")
                    state = self._transition(trace, state, AgentState.FAILED)
                    break

                iterations += 1
                trace.record(
                    EventType.MODEL_REQUEST,
                    state,
                    iteration=iterations,
                    messages=len(messages),
                    tools=[spec.name for spec in tool_specs()],
                )

                try:
                    response = self._provider.complete(
                        system=SYSTEM_PROMPT,
                        messages=messages,
                        tools=tool_specs(),
                        max_tokens=self._budget.max_output_tokens,
                    )
                except LLMError as exc:
                    outcome = f"model request failed: {exc}"
                    trace.record(EventType.MODEL_RESPONSE, state, error=str(exc))
                    state = self._transition(trace, state, AgentState.FAILED)
                    break

                usage = usage + response.usage
                cost = _accumulate_cost(cost, response)
                trace.record(
                    EventType.MODEL_RESPONSE,
                    state,
                    iteration=iterations,
                    stop_reason=response.stop_reason.value,
                    tool_calls=[call.name for call in response.tool_calls],
                    text=response.text,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    latency_ms=response.latency_ms,
                    cost_usd=response.cost_usd,
                )
                messages.append(response.to_message())

                if not response.wants_tools:
                    if builder.edited_files:
                        final_message = response.text
                        outcome = "agent proposed a patch"
                        state = self._transition(trace, state, AgentState.REVIEW)
                        state = self._transition(trace, state, AgentState.CANDIDATE)
                        break
                    if nudged:
                        final_message = response.text
                        outcome = "agent finished without proposing any edit"
                        state = self._transition(trace, state, AgentState.FAILED)
                        break
                    nudged = True
                    messages.append(Message.user(build_review_nudge(builder.edited_files)))
                    continue

                if tool_calls + len(response.tool_calls) > self._budget.max_tool_calls:
                    outcome = f"tool-call budget exhausted ({self._budget.max_tool_calls})"
                    trace.record(EventType.BUDGET_EXCEEDED, state, limit="tool_calls")
                    state = self._transition(trace, state, AgentState.FAILED)
                    break

                for call in response.tool_calls:
                    tool_calls += 1
                    trace.record(
                        EventType.TOOL_CALL, state, tool=call.name, arguments=call.arguments
                    )
                    state = self._advance_for_tool(trace, state, call.name)

                    if call.name in _EDIT_TOOLS and len(builder.edited_files) >= (
                        self._budget.max_files
                    ):
                        result_content = (
                            f"File budget reached ({self._budget.max_files} files). "
                            "Stop editing and summarise what you changed."
                        )
                        is_error = True
                    else:
                        result = invoke(call.name, context, call.arguments)
                        result_content, is_error = result.content, result.is_error

                    tool_errors += int(is_error)
                    trace.record(
                        EventType.TOOL_RESULT,
                        state,
                        tool=call.name,
                        is_error=is_error,
                        content_length=len(result_content),
                    )
                    if call.name in _EDIT_TOOLS and not is_error:
                        trace.record(
                            EventType.EDIT_APPLIED,
                            state,
                            file=call.arguments.get("file"),
                            rationale=call.arguments.get("rationale", ""),
                        )

                    messages.append(
                        Message.tool_result(call, wrap_untrusted(result_content), is_error=is_error)
                    )

            patch = builder.build(summary=final_message)
            summary = RunSummary(
                run_id=identifier,
                repository=str(workspace.root),
                provider=self._provider.name,
                model=self._provider.model,
                final_state=state,
                iterations=iterations,
                tool_calls=tool_calls,
                tool_errors=tool_errors,
                files_changed=len(patch.files),
                usage=usage,
                cost_usd=cost,
                duration_seconds=round(time.perf_counter() - started, 3),
                outcome=outcome or "run ended",
            )
            trace.record(
                EventType.RUN_FINISHED,
                state,
                outcome=summary.outcome,
                files_changed=summary.files_changed,
                total_tokens=usage.total_tokens,
                cost_usd=cost,
            )
            trace.write_summary(summary)

        logger.info(
            "migration_run_finished",
            run_id=identifier,
            final_state=state.value,
            iterations=iterations,
            tool_calls=tool_calls,
            files_changed=summary.files_changed,
            total_tokens=usage.total_tokens,
            cost_usd=cost,
        )
        return MigrationResult(
            summary=summary, patch=patch, trace=trace, final_message=final_message
        )

    # ------------------------------------------------------------ helpers ---

    @staticmethod
    def _transition(trace: RunTrace, current: AgentState, target: AgentState) -> AgentState:
        """Move to ``target``, refusing an illegal transition.

        An illegal transition is a bug in this loop, not something a model can
        cause, so it raises rather than being smoothed over.
        """
        if current is target:
            return current
        if is_terminal(current):
            return current
        if not can_transition(current, target):
            raise AgentError(
                "illegal agent state transition", **{"from": current.value, "to": target.value}
            )
        trace.record(EventType.STATE_CHANGED, target, previous=current.value)
        return target

    def _advance_for_tool(self, trace: RunTrace, state: AgentState, tool_name: str) -> AgentState:
        """Advance the state machine based on which tool was called."""
        if tool_name in _EDIT_TOOLS:
            if state is AgentState.ANALYZE:
                state = self._transition(trace, state, AgentState.PLAN)
            return self._transition(trace, state, AgentState.EDIT)
        if tool_name in _REVIEW_TOOLS and state is AgentState.EDIT:
            return self._transition(trace, state, AgentState.REVIEW)
        return state


def _accumulate_cost(total: float | None, response: LLMResponse) -> float | None:
    """Add a response's cost, propagating "unknown" rather than treating it as free."""
    if total is None or response.cost_usd is None:
        return None
    return total + response.cost_usd


__all__ = ["AgentBudget", "MigrationAgent", "MigrationResult"]
