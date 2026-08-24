"""Turning a verified patch into a pull request a person can review.

This is the first thing in Rewire that reaches outside the machine, and the last
step before a change could reach someone's default branch. Two rules follow.

**Only a verified patch is published, and there is no override.** Phases 8 to 10
measured how often Rewire's own verification is wrong, which is an argument for
a human reviewer — not an argument for publishing weaker evidence and hoping the
reviewer catches it. A patch the sandbox refused can still be printed, saved with
``--write-diff`` and applied by hand.

**Rewire cannot merge.** :mod:`rewire.gitio.github` has no merge function, no
approve, and no auto-merge flag, so no bug or future flag here can reach one.
The pull request exists precisely to put a person between Rewire's evidence and
the default branch.

The description is written to be *argued with*. It says what the specification
changed, what Rewire changed, what evidence exists, and — in its own section —
what that evidence does not establish. A pull request that only lists the green
checks invites the reviewer to skim, which is the opposite of the point.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from rewire.agents.patch import assert_patch_applies_to, write_patch
from rewire.changes.models import ChangeReport, Severity
from rewire.core.errors import PatchError
from rewire.core.logging import get_logger
from rewire.gitio import branch as git
from rewire.gitio import github
from rewire.gitio.repository import inspect_working_tree
from rewire.sandbox.models import CheckStatus, VerificationReport
from rewire.services.migrate import MigrationOutcome, MigrationStatus

logger = get_logger(__name__)

#: How many changed files to name in the description before summarising.
MAX_LISTED_FILES: int = 25


class PublishStatus(StrEnum):
    """What happened to a publishing attempt."""

    #: A branch was pushed and a pull request opened.
    PUBLISHED = "published"
    #: Everything ran except the push and the pull request.
    DRY_RUN = "dry_run"
    #: A precondition was not met. Nothing was written or pushed.
    REFUSED = "refused"
    #: The migration produced nothing to publish, which is not a failure.
    NOTHING_TO_PUBLISH = "nothing_to_publish"

    @property
    def is_success(self) -> bool:
        """Whether the run did what was asked of it."""
        return self in {PublishStatus.PUBLISHED, PublishStatus.DRY_RUN}


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """How to publish a verified migration."""

    repository: Path
    #: Leading path segment of the created branch.
    prefix: str = "rewire"
    #: Open the pull request as a draft.
    draft: bool = False
    #: Branch to propose into. Defaults to the repository's default branch.
    base: str = ""
    #: Do everything except push and open the pull request.
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """What publishing did, and what it refused to do."""

    status: PublishStatus
    branch: str = ""
    commit: git.Commit | None = None
    pull_request: github.PullRequest | None = None
    title: str = ""
    body: str = ""
    #: Why publishing was refused. Empty otherwise.
    refusal: str = ""

    @property
    def published(self) -> bool:
        """Whether a pull request now exists."""
        return self.pull_request is not None


def check_publishable(root: Path) -> str:
    """Return why ``root`` cannot be published to, or an empty string if it can.

    Called *before* the migration runs. Every one of these answers is available
    in milliseconds, and discovering them after an agent run and two container
    runs would cost real money to learn something free.
    """
    tree = inspect_working_tree(root)
    if not tree.is_repository:
        return "not a Git repository, so there is no branch to push"
    if not tree.is_clean:
        return (
            f"{tree.describe()}; commit or stash them first, so the pull request "
            "contains Rewire's change and nothing else"
        )
    if not git.default_remote(root):
        return "the repository has no remote, so there is nowhere to push"
    if not github.is_authenticated(root):
        return "the GitHub CLI is not authenticated; run 'gh auth login'"
    return ""


def _summarise_changes(changes: ChangeReport | None) -> list[str]:
    if changes is None:  # pragma: no cover - a published run always has a diff
        return []
    breaking = [c for c in changes.changes if c.severity is Severity.BREAKING]
    potential = [c for c in changes.changes if c.severity is Severity.POTENTIALLY_BREAKING]
    lines = [
        "## What changed in the API",
        "",
        f"`{changes.old_spec.version or '?'}` → `{changes.new_spec.version or '?'}`, "
        f"{len(breaking)} breaking and {len(potential)} potentially breaking change(s).",
        "",
    ]
    for change in (*breaking, *potential)[:MAX_LISTED_FILES]:
        detail = change.detail or change.type.value
        replacement = f" → `{change.replacement}`" if change.replacement else ""
        lines.append(f"- **{change.severity.value}** {detail}{replacement}")
    return [*lines, ""]


def _summarise_evidence(report: VerificationReport | None) -> list[str]:
    if report is None:  # pragma: no cover - a published run is always verified
        return []
    lines = [
        "## Evidence",
        "",
        f"Verdict **{report.verdict.value}** — {report.reason}",
        "",
        "The repository's own checks were run twice inside a container with no network: "
        "once before the patch and once after it. A check is only evidence if it passed "
        "before *and* after, which is what separates a patch that broke something from a "
        "repository that was already failing.",
        "",
        "| Check | Tool | Before | After |",
        "|---|---|---|---|",
    ]
    before = {result.kind: result for result in report.baseline}
    after = {result.kind: result for result in report.patched}
    for kind in sorted(before | after, key=lambda k: k.strength):
        shown = after.get(kind) or before[kind]
        lines.append(
            f"| {kind.value} | `{shown.name}` | "
            f"{before[kind].status.value if kind in before else '-'} | "
            f"{after[kind].status.value if kind in after else '-'} |"
        )
    return [*lines, ""]


def _summarise_limits(outcome: MigrationOutcome, report: VerificationReport | None) -> list[str]:
    """The section that says what the green checks do not establish."""
    lines = [
        "## What this does not establish",
        "",
        "- The checks above are the repository's own. They cover what they cover, and "
        "a migration can be wrong in a way no existing test exercises.",
        "- Rewire compared assertion counts and public signatures to refuse a patch that "
        "passes by weakening its tests. That catches deletions and interface changes; it "
        "cannot catch a test whose *expected values* were rewritten to match a wrong "
        "implementation.",
        "- Nothing here was reviewed by a person. That is what this pull request is for.",
    ]
    if report is not None and report.weakenings:
        lines += [
            "",
            "Rewire also noticed the following, which did not block the verdict but is "
            "worth a look:",
            "",
        ]
        lines += [f"- {finding.describe()}" for finding in report.weakenings]
    unavailable = [
        result.kind.value
        for result in (report.patched if report else ())
        if result.status in {CheckStatus.UNAVAILABLE, CheckStatus.SKIPPED}
    ]
    if unavailable:
        lines += ["", f"Checks that could not run at all: {', '.join(sorted(unavailable))}."]
    return [*lines, ""]


def build_title(outcome: MigrationOutcome) -> str:
    """A one-line summary naming the specification and the version it moved to."""
    changes = outcome.changes
    if changes is None:  # pragma: no cover - a published run always has a diff
        return "Migrate to the new API version"
    name = changes.new_spec.title or "the API"
    version = changes.new_spec.version
    return f"Migrate {name} to {version}" if version else f"Migrate {name} to the new version"


def build_body(outcome: MigrationOutcome) -> str:
    """Write the pull request description.

    Structured so a reviewer meets the evidence and its limits together. The
    closing line is not decoration: a reader needs to know that nothing will
    merge this on its own.
    """
    repair = outcome.repair
    attempt = repair.verified if repair else None
    report = attempt.report if attempt else None
    patch = outcome.patch
    files, added, removed = patch.stats()

    lines = [
        "Rewire proposed this migration automatically. **It cannot merge it, and it has "
        "not been reviewed by a person.**",
        "",
    ]
    lines += _summarise_changes(outcome.changes)

    lines += ["## What changed here", "", f"{files} file(s), +{added} -{removed}.", ""]
    for change in patch.changes:
        if change.changed:
            lines.append(f"- `{change.file}` +{change.added_lines} -{change.removed_lines}")
    lines.append("")

    if summary := (attempt.result.final_message if attempt else ""):
        lines += ["The agent's own summary of what it did:", "", f"> {summary.strip()}", ""]

    lines += _summarise_evidence(report)
    lines += _summarise_limits(outcome, report)

    if repair is not None:
        cost = f"${repair.total_cost_usd:.4f}" if repair.total_cost_usd is not None else "unknown"
        attempts = len(repair.attempts)
        repaired = (
            " (an earlier attempt failed and was retried with the failure)"
            if repair.repaired
            else ""
        )
        lines += [
            "## Cost",
            "",
            f"{attempts} attempt(s){repaired}, {repair.total_tokens} tokens, {cost}.",
            "",
        ]

    lines += [
        "---",
        "",
        f"Run `{outcome.run_id}`. Rewire has no merge, approve or auto-merge capability: "
        "this pull request stays open until a person acts on it.",
    ]
    return "\n".join(lines)


def publish(outcome: MigrationOutcome, request: PublishRequest) -> PublishOutcome:
    """Put a verified patch on a branch and open a pull request for it.

    The original branch is restored whatever happens, including on failure, so a
    half-finished publish leaves the user where they started.

    Raises:
        GitError: Git or ``gh`` failed in a way that leaves nothing sensible to
            report. Refusals are returned, not raised.
    """
    root = Path(request.repository)

    if outcome.patch.is_empty:
        return PublishOutcome(
            status=PublishStatus.NOTHING_TO_PUBLISH,
            refusal=f"the migration ended as {outcome.status.value} with no patch",
        )
    if outcome.status is not MigrationStatus.VERIFIED:
        return PublishOutcome(
            status=PublishStatus.REFUSED,
            refusal=(
                f"the patch is {outcome.status.value}, and Rewire only publishes a patch "
                "the sandbox verified. There is no override: run with --diff and apply it "
                "by hand if you disagree."
            ),
        )
    if reason := check_publishable(root):
        return PublishOutcome(status=PublishStatus.REFUSED, refusal=reason)

    # Only asked when it is actually needed. An explicit --base makes the lookup
    # unnecessary, and a dry run against a repository GitHub cannot describe is
    # still a useful rehearsal of everything local.
    base = request.base or github.describe_repository(root).default_branch
    name = git.branch_name(
        prefix=request.prefix,
        subject=(outcome.changes.new_spec.title if outcome.changes else "") or "migration",
        run_id=outcome.run_id,
    )
    title = build_title(outcome)
    body = build_body(outcome)

    with git.on_branch(root, name):
        try:
            assert_patch_applies_to(outcome.patch, root)
            written = write_patch(outcome.patch, root)
        except PatchError as exc:
            return PublishOutcome(
                status=PublishStatus.REFUSED,
                branch=name,
                refusal=f"the repository changed since the patch was verified: {exc}",
            )

        commit = git.commit(root, written, f"{title}\n\nOpened by Rewire, run {outcome.run_id}.")
        if request.dry_run:
            logger.info("publish_dry_run", branch=name, files=len(written))
            return PublishOutcome(
                status=PublishStatus.DRY_RUN, branch=name, commit=commit, title=title, body=body
            )

        remote = git.default_remote(root)
        git.push(root, remote, name)
        pull_request = github.open_pull_request(
            root, title=title, body=body, head=name, base=base, draft=request.draft
        )

    logger.info("publish_finished", branch=name, url=pull_request.url)
    return PublishOutcome(
        status=PublishStatus.PUBLISHED,
        branch=name,
        commit=commit,
        pull_request=pull_request,
        title=title,
        body=body,
    )


__all__ = [
    "MAX_LISTED_FILES",
    "PublishOutcome",
    "PublishRequest",
    "PublishStatus",
    "build_body",
    "build_title",
    "check_publishable",
    "publish",
]
