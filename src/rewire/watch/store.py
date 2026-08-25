"""Where watches, their baselines and their state are kept.

Three things live on disk, and the middle one is the reason this module exists.

``watches.json`` is the list of declarations. ``<name>/baseline.yaml`` is the
**specification bytes** the repository is believed to target — not a digest of
them, because a digest answers "did it change" and the next question is always
"changed how", which needs the old document. ``<name>/state.json`` is what was
remembered between checks: digests, cache validators, and what has already been
acted on.

Two rules shape the writes:

**A name is a directory name, so it is validated rather than escaped.** The
alternative is hashing or quoting user input into a path, which works until
someone reads the directory listing and cannot tell which watch is which.

**A check takes a lock.** Two overlapping cron runs of the same watch would race
on the baseline and could open two pull requests for one change. The lock
records the process that holds it, so one left behind by a killed run is
identified as stale and taken over instead of blocking the watch forever.
"""

from __future__ import annotations

import errno
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Final

from rewire.core.errors import WatchError
from rewire.core.logging import get_logger
from rewire.watch.models import Watch, WatchState

logger = get_logger(__name__)

#: Names permitted for a watch. Narrow because the name becomes a directory.
_VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

#: File holding the list of declared watches.
WATCHLIST_FILE: Final[str] = "watches.json"

#: How many acted-upon versions to remember per watch. Old entries are dropped
#: oldest-first: the list exists to prevent a repeat, and a version nobody has
#: seen in a hundred checks is not about to reappear.
MAX_ACTED_RECORDS: Final[int] = 50


def validate_name(name: str) -> str:
    """Return ``name`` if it is usable as a watch name and a directory name.

    Raises:
        WatchError: The name is empty, too long, or contains anything that would
            have to be escaped to be a path segment.
    """
    if not _VALID_NAME.match(name):
        raise WatchError(
            "a watch name must start with a letter or digit and contain only "
            "letters, digits, dots, dashes and underscores",
            name=name,
        )
    return name


class WatchStore:
    """Reads and writes everything a watch keeps on disk."""

    def __init__(self, root: Path) -> None:
        """Store watches under ``root``, typically ``<data_dir>/watch``."""
        self.root = Path(root)

    # ------------------------------------------------------------- watches ---

    @property
    def watchlist_path(self) -> Path:
        """Path of the file holding the declarations."""
        return self.root / WATCHLIST_FILE

    def load_all(self) -> tuple[Watch, ...]:
        """Return every declared watch, in the order they were added.

        Raises:
            WatchError: The watchlist exists but cannot be read or parsed.
        """
        path = self.watchlist_path
        if not path.exists():
            return ()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WatchError(f"the watchlist could not be read: {exc}", path=str(path)) from exc
        if not isinstance(payload, list):
            raise WatchError("the watchlist must be a list of watches", path=str(path))
        return tuple(Watch.from_dict(item) for item in payload if isinstance(item, dict))

    def get(self, name: str) -> Watch:
        """Return one watch by name.

        Raises:
            WatchError: No watch of that name is declared.
        """
        for watch in self.load_all():
            if watch.name == name:
                return watch
        raise WatchError("no watch by that name", name=name)

    def save(self, watch: Watch) -> None:
        """Add ``watch``, or replace the existing one with the same name."""
        validate_name(watch.name)
        existing = [item for item in self.load_all() if item.name != watch.name]
        self._write_watchlist([*existing, watch])
        logger.info("watch_saved", watch=watch.name, action=watch.action.value)

    def remove(self, name: str, *, forget_state: bool = False) -> None:
        """Remove a watch, optionally discarding its baseline and history.

        The state is kept by default. Re-adding a watch whose baseline survived
        picks up where it left off; discarding it means the next check adopts
        whatever is upstream *now* as the truth, which silently swallows any
        change that happened in between.

        Raises:
            WatchError: No watch of that name is declared.
        """
        declared = self.load_all()
        remaining = [item for item in declared if item.name != name]
        if len(remaining) == len(declared):
            raise WatchError("no watch by that name", name=name)
        self._write_watchlist(remaining)
        if forget_state:
            for child in sorted(self.directory(name).glob("*")):
                child.unlink(missing_ok=True)
            self.directory(name).rmdir()
        logger.info("watch_removed", watch=name, forget_state=forget_state)

    def _write_watchlist(self, watches: list[Watch]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _write_atomic(
            self.watchlist_path,
            json.dumps([watch.to_dict() for watch in watches], indent=2) + "\n",
        )

    # --------------------------------------------------------------- state ---

    def directory(self, name: str) -> Path:
        """Directory holding one watch's baseline, state and lock."""
        return self.root / validate_name(name)

    def read_state(self, name: str) -> WatchState:
        """Return what was remembered about ``name``, empty if nothing was.

        A state file that cannot be parsed is a bug or a corrupted disk, and
        treating it as "no baseline" would silently adopt whatever is upstream
        now. It is raised instead.

        Raises:
            WatchError: The state file exists but cannot be read or parsed.
        """
        path = self.directory(name) / "state.json"
        if not path.exists():
            return WatchState()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WatchError(f"the watch state could not be read: {exc}", watch=name) from exc
        if not isinstance(payload, dict):
            raise WatchError("the watch state must be an object", watch=name)
        return WatchState.from_dict(payload)

    def write_state(self, name: str, state: WatchState) -> None:
        """Persist ``state``, trimming the acted-upon history to its ceiling."""
        directory = self.directory(name)
        directory.mkdir(parents=True, exist_ok=True)
        trimmed = state
        if len(state.acted) > MAX_ACTED_RECORDS:
            ordered = sorted(state.acted.items(), key=lambda item: item[1].at)
            trimmed = replace(state, acted=dict(ordered[-MAX_ACTED_RECORDS:]))
        _write_atomic(directory / "state.json", json.dumps(trimmed.to_dict(), indent=2) + "\n")

    # ------------------------------------------------------------ baseline ---

    def baseline_path(self, name: str) -> Path:
        """Path of the stored baseline specification, whatever its extension."""
        directory = self.directory(name)
        for candidate in sorted(directory.glob("baseline.*")):
            return candidate
        return directory / "baseline.yaml"

    def write_baseline(self, name: str, text: str, *, suffix: str = ".yaml") -> Path:
        """Replace the baseline specification and return where it was written.

        Any previously stored baseline is removed first, so a watch whose source
        changed from JSON to YAML does not leave two files behind for
        :meth:`baseline_path` to choose between.
        """
        directory = self.directory(name)
        directory.mkdir(parents=True, exist_ok=True)
        for stale in sorted(directory.glob("baseline.*")):
            stale.unlink(missing_ok=True)
        path = directory / f"baseline{suffix}"
        _write_atomic(path, text)
        return path

    def write_candidate(self, name: str, text: str, *, suffix: str = ".yaml") -> Path:
        """Write the specification currently upstream, for diffing and migrating.

        Kept after the check rather than deleted: when a watch reports a breaking
        change, the two documents that produced that conclusion are the first
        thing anyone will want to look at.
        """
        directory = self.directory(name)
        directory.mkdir(parents=True, exist_ok=True)
        for stale in sorted(directory.glob("candidate.*")):
            stale.unlink(missing_ok=True)
        path = directory / f"candidate{suffix}"
        _write_atomic(path, text)
        return path

    # ---------------------------------------------------------------- lock ---

    @contextmanager
    def lock(self, name: str) -> Iterator[bool]:
        """Hold the check lock for ``name``, yielding whether it was acquired.

        Yields ``False`` rather than raising when another process holds it: an
        overlapping cron run is an ordinary event, not an error, and the right
        response is to skip this pass and say so.
        """
        path = self.directory(name) / "check.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        held = _try_lock(path)
        if not held:
            logger.info("watch_check_skipped_locked", watch=name)
            yield False
            return
        try:
            yield True
        finally:
            path.unlink(missing_ok=True)


def _try_lock(path: Path) -> bool:
    """Create ``path`` exclusively, taking it over if its owner is gone."""
    for attempt in (1, 2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if attempt == 2 or not _is_stale(path):
                return False
            path.unlink(missing_ok=True)
            continue
        except OSError as exc:  # pragma: no cover - the directory was just created
            raise WatchError(f"could not take the check lock: {exc}", path=str(path)) from exc
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(f"{os.getpid()}\n")
        return True
    return False  # pragma: no cover - the loop returns on both paths


def _is_stale(path: Path) -> bool:
    """Whether the process recorded in a lock file is no longer running.

    Only meaningful on the machine that wrote it. A lock file with no readable
    pid is treated as stale, because the alternative is a watch that never runs
    again and gives no reason.
    """
    try:
        recorded = path.read_text(encoding="utf-8").strip()
    except OSError:  # pragma: no cover - it existed a moment ago
        return True
    if not recorded.isdigit():
        return True
    try:
        os.kill(int(recorded), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Alive, owned by another user. Not ours to take.
        return False
    except OSError as exc:  # pragma: no cover - defensive
        return exc.errno == errno.ESRCH
    return False


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temporary file in the same directory.

    A check that is killed part-way through writing its state would otherwise
    leave a truncated file, and the next check would read it as "no baseline"
    and adopt whatever is upstream — losing the change it was there to find.
    """
    temporary = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise WatchError(f"could not write {path.name}: {exc}", path=str(path)) from exc


__all__ = [
    "MAX_ACTED_RECORDS",
    "WATCHLIST_FILE",
    "WatchStore",
    "validate_name",
]
