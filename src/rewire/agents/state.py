"""The migration agent's state machine.

The terminal success state is named ``CANDIDATE``, not ``DONE``. That is the
whole point of Phase 4: the agent produces a *proposal*, and has no means of
establishing that it works. Calling the state ``DONE`` would invite every later
reader — and every later feature — to treat reaching it as evidence of success.
Verification arrives in Phase 5, and only a sandbox run can move a migration
past this point.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class AgentState(StrEnum):
    """Where a migration run has got to."""

    #: Reading the impact report and the code it points at.
    ANALYZE = "analyze"
    #: Deciding what to change and why, before changing anything.
    PLAN = "plan"
    #: Proposing edits.
    EDIT = "edit"
    #: Checking its own edits against the change report.
    REVIEW = "review"
    #: Finished with a patch that is proposed, not proven.
    CANDIDATE = "candidate"
    #: Finished with no usable patch.
    FAILED = "failed"


#: States from which no transition is possible.
TERMINAL_STATES: Final[frozenset[AgentState]] = frozenset({AgentState.CANDIDATE, AgentState.FAILED})

#: Permitted transitions. The agent may loop within the working states as it
#: reads more code, but may never leave a terminal state, and may never reach
#: CANDIDATE without passing through EDIT -- a patch has to be built before it
#: can be proposed.
ALLOWED_TRANSITIONS: Final[dict[AgentState, frozenset[AgentState]]] = {
    AgentState.ANALYZE: frozenset({AgentState.ANALYZE, AgentState.PLAN, AgentState.FAILED}),
    AgentState.PLAN: frozenset(
        {AgentState.ANALYZE, AgentState.PLAN, AgentState.EDIT, AgentState.FAILED}
    ),
    AgentState.EDIT: frozenset(
        {AgentState.ANALYZE, AgentState.EDIT, AgentState.REVIEW, AgentState.FAILED}
    ),
    AgentState.REVIEW: frozenset(
        {AgentState.EDIT, AgentState.REVIEW, AgentState.CANDIDATE, AgentState.FAILED}
    ),
    AgentState.CANDIDATE: frozenset(),
    AgentState.FAILED: frozenset(),
}


def can_transition(current: AgentState, target: AgentState) -> bool:
    """Whether moving from ``current`` to ``target`` is permitted."""
    return target in ALLOWED_TRANSITIONS[current]


def is_terminal(state: AgentState) -> bool:
    """Whether a run in ``state`` has finished."""
    return state in TERMINAL_STATES


__all__ = [
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    "AgentState",
    "can_transition",
    "is_terminal",
]
