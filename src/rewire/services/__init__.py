"""Orchestration across components that must not depend on each other.

The repair loop needs both the agent and the sandbox. Putting it inside either
would make the package graph circular -- ``rewire.sandbox`` already imports
``rewire.agents.patch`` in order to apply a patch, so ``rewire.agents`` cannot
import ``rewire.sandbox`` back. A composition layer that depends on both, and
which nothing else depends on, keeps every arrow pointing one way.
"""

from rewire.services.migrate import (
    MigrationOutcome,
    MigrationRequest,
    MigrationStatus,
    run_migration,
)
from rewire.services.repair import (
    REPAIRABLE,
    Attempt,
    RepairOutcome,
    RepairPolicy,
    migrate_with_repair,
)

__all__ = [
    "REPAIRABLE",
    "Attempt",
    "MigrationOutcome",
    "MigrationRequest",
    "MigrationStatus",
    "RepairOutcome",
    "RepairPolicy",
    "migrate_with_repair",
    "run_migration",
]
