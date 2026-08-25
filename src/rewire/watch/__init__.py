"""Following an upstream specification without being asked to.

Everything before this phase started with a person running a command against two
specifications they had already found. This is the part that notices.

Split by what each piece is allowed to do. :mod:`~rewire.watch.source` reads,
over a network, from something untrusted, and nothing else.
:mod:`~rewire.watch.store` remembers, and owns the rule that a baseline is the
version the repository's code targets rather than the last thing downloaded.
:mod:`~rewire.watch.monitor` decides, and is written so that the expensive
decision — calling a model — is reached only after three cheaper questions have
failed to settle the matter.

There is no daemon. ``rewire watch check`` is one pass with an exit code, meant
for cron, systemd or CI, because a scheduler is the one part of this that every
operating system already ships and does better.
"""

from rewire.watch.models import (
    ActedRecord,
    CheckOutcome,
    CheckStatus,
    Watch,
    WatchAction,
    WatchState,
)
from rewire.watch.monitor import (
    CheckPolicy,
    accept,
    check_all,
    check_watch,
    exit_code_for,
    semantic_digest,
)
from rewire.watch.source import Fetched, fetch, is_url
from rewire.watch.store import WatchStore, validate_name

__all__ = [
    "ActedRecord",
    "CheckOutcome",
    "CheckPolicy",
    "CheckStatus",
    "Fetched",
    "Watch",
    "WatchAction",
    "WatchState",
    "WatchStore",
    "accept",
    "check_all",
    "check_watch",
    "exit_code_for",
    "fetch",
    "is_url",
    "semantic_digest",
    "validate_name",
]
