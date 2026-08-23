"""Impact analysis: joining detected API changes to the code they affect.

Deterministic and LLM-free, like the phases it builds on. Given a change report
and a repository index, it proposes candidate locations by several independent
strategies and scores each one against an auditable evidence model.

    >>> report = diff_specs(load_spec("old.yaml"), load_spec("new.yaml"))
    >>> impact = analyse_impact(report, build_index("./repo"))
    >>> impact.impacts[0].locations[0].location
    'src/chatapp/client.py:26'
"""

from rewire.impact.analyzer import analyse_impact, attach_snippets
from rewire.impact.models import (
    DEFAULT_MIN_CONFIDENCE,
    AffectedLocation,
    ChangeImpact,
    ImpactReport,
    ImpactSummary,
    MatchStrategy,
    Signal,
    SignalKind,
)
from rewire.impact.packages import infer_packages, resolve_packages
from rewire.impact.scoring import confidence_from, sigmoid

__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "AffectedLocation",
    "ChangeImpact",
    "ImpactReport",
    "ImpactSummary",
    "MatchStrategy",
    "Signal",
    "SignalKind",
    "analyse_impact",
    "attach_snippets",
    "confidence_from",
    "infer_packages",
    "resolve_packages",
    "sigmoid",
]
