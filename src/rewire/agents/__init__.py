"""LLM agent loop, tool definitions and run tracing.

The agent produces a *candidate* patch. It has no way to establish that the
patch works — that requires executing it, which is Phase 5 — so nothing here
reports success, and the terminal state is named accordingly.
"""

from rewire.agents.config import ALL_TOOLS, DEFAULT_AGENT_CONFIG, SEARCH_TOOLS, AgentConfig
from rewire.agents.migration_agent import AgentBudget, MigrationAgent, MigrationResult
from rewire.agents.patch import CandidatePatch, FileChange, FileEdit, PatchBuilder, write_patch
from rewire.agents.prompts import PROMPT_VERSION, SYSTEM_PROMPT, wrap_untrusted
from rewire.agents.state import AgentState, can_transition, is_terminal
from rewire.agents.tools import TOOLS, ToolContext, invoke, tool_specs
from rewire.agents.trace import EventType, RunSummary, RunTrace, TraceEvent, load_trace
from rewire.agents.workspace import Workspace

__all__ = [
    "ALL_TOOLS",
    "DEFAULT_AGENT_CONFIG",
    "PROMPT_VERSION",
    "SEARCH_TOOLS",
    "SYSTEM_PROMPT",
    "TOOLS",
    "AgentBudget",
    "AgentConfig",
    "AgentState",
    "CandidatePatch",
    "EventType",
    "FileChange",
    "FileEdit",
    "MigrationAgent",
    "MigrationResult",
    "PatchBuilder",
    "RunSummary",
    "RunTrace",
    "ToolContext",
    "TraceEvent",
    "Workspace",
    "can_transition",
    "invoke",
    "is_terminal",
    "load_trace",
    "tool_specs",
    "wrap_untrusted",
    "write_patch",
]
