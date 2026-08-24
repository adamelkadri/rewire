"""Git and GitHub integration.

Named ``gitio`` so it cannot shadow the ``git`` module namespace.

Split by what it can do to a repository. :mod:`~rewire.gitio.repository` only
reads, and decides whether a working tree is clean enough that a written patch
could be reviewed and undone. :mod:`~rewire.gitio.branch` writes, and every
operation in it is narrowed so that Rewire cannot destroy work it did not
create. :mod:`~rewire.gitio.github` opens pull requests and deliberately has no
way to merge one.
"""

from rewire.gitio.branch import (
    Commit,
    branch_exists,
    branch_name,
    checkout,
    commit,
    create_branch,
    current_branch,
    default_remote,
    on_branch,
    push,
    slugify,
)
from rewire.gitio.github import (
    PullRequest,
    Repository,
    describe_repository,
    is_authenticated,
    open_pull_request,
)
from rewire.gitio.repository import WorkingTree, inspect_working_tree

__all__ = [
    "Commit",
    "PullRequest",
    "Repository",
    "WorkingTree",
    "branch_exists",
    "branch_name",
    "checkout",
    "commit",
    "create_branch",
    "current_branch",
    "default_remote",
    "describe_repository",
    "inspect_working_tree",
    "is_authenticated",
    "on_branch",
    "open_pull_request",
    "push",
    "slugify",
]
