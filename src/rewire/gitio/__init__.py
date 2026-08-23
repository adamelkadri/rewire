"""Git and GitHub integration.

Named ``gitio`` so it cannot shadow the ``git`` module namespace.

Phase 7 uses only the read-only half: deciding whether a working tree is clean
enough that a written patch can be reviewed and undone. Branching, committing
and pull requests are Phase 11.
"""

from rewire.gitio.repository import WorkingTree, inspect_working_tree

__all__ = ["WorkingTree", "inspect_working_tree"]
