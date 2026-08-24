"""What the agent is given — the harness knobs an ablation varies.

Rewire's central claim is that deterministic analysis before the model makes the
model better. Phase 9 established that swapping the model barely moves the
result, which makes that claim the next thing worth testing, and it cannot be
tested without a way to take the analysis away.

This is that way. Every field defaults to the shipped configuration, so an agent
constructed without one behaves exactly as it did before this module existed. An
ablation is a named departure from these defaults, recorded in the trace so a
result can never be attributed to the wrong configuration.

Two knobs, because there are two channels the deterministic findings reach the
agent through, and withholding one while leaving the other open would measure
nothing:

* ``include_impact_locations`` governs both the opening task prompt *and* the
  ``inspect_api_change`` tool. The agent still learns which API fields changed;
  it is not told where in the repository they are used.
* ``tools`` governs which of the eight tools are offered — and, separately,
  which may be invoked. A model that hallucinates a withheld tool name must be
  refused, or the ablation leaks through a lucky guess.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from rewire.agents.tools import TOOLS_BY_NAME

#: Every tool the agent may be given. The shipped configuration offers all of
#: them.
ALL_TOOLS: frozenset[str] = frozenset(TOOLS_BY_NAME)

#: The tools that let the agent look for code the analysis did not point at.
#: Withholding exactly these is the mirror image of withholding the impact
#: findings: one arm gives the agent the answers, the other gives it the means
#: to find them.
SEARCH_TOOLS: frozenset[str] = frozenset({"search_code", "find_calls", "find_symbol"})


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """The agent's information diet for one run.

    Attributes:
        include_impact_locations: Whether the ranked affected locations reach the
            agent, through the task prompt and through ``inspect_api_change``.
            The detected API changes are given either way.
        tools: Tool names the agent may see and call.
    """

    include_impact_locations: bool = True
    tools: frozenset[str] = field(default=ALL_TOOLS)

    def __post_init__(self) -> None:
        """Reject a tool name that does not exist.

        An ablation that silently withholds nothing because a name was misspelt
        would report the full configuration's score under the ablation's label,
        which is the worst possible failure for an experiment.
        """
        unknown = self.tools - ALL_TOOLS
        if unknown:
            raise ValueError(f"unknown tool(s): {', '.join(sorted(unknown))}")

    @property
    def is_default(self) -> bool:
        """Whether this is the shipped configuration."""
        return self.include_impact_locations and self.tools == ALL_TOOLS

    @property
    def withheld_tools(self) -> frozenset[str]:
        """Tools this configuration takes away."""
        return ALL_TOOLS - self.tools

    def describe(self) -> str:
        """One line naming what was taken away, for a trace and a report."""
        if self.is_default:
            return "full configuration"
        parts = []
        if not self.include_impact_locations:
            parts.append("impact locations withheld")
        if withheld := self.withheld_tools:
            parts.append(f"tools withheld: {', '.join(sorted(withheld))}")
        return "; ".join(parts)

    @classmethod
    def without(cls, *, impact_locations: bool = True, tools: Collection[str] = ()) -> AgentConfig:
        """Build a configuration with named things removed.

        Reads as the experiment reads — ``AgentConfig.without(impact_locations=False)``
        — rather than as its double negative.

        Raises:
            ValueError: A named tool does not exist. Subtracting an unknown name
                would silently withhold nothing, and the arm would report the
                full configuration's score under the ablation's label.
        """
        requested = frozenset(tools)
        unknown = requested - ALL_TOOLS
        if unknown:
            raise ValueError(f"unknown tool(s): {', '.join(sorted(unknown))}")
        return cls(include_impact_locations=impact_locations, tools=ALL_TOOLS - requested)


#: The shipped configuration, as a named constant so a comparison's control arm
#: is the real product rather than a restatement of it.
DEFAULT_AGENT_CONFIG: AgentConfig = AgentConfig()


__all__ = [
    "ALL_TOOLS",
    "DEFAULT_AGENT_CONFIG",
    "SEARCH_TOOLS",
    "AgentConfig",
]
