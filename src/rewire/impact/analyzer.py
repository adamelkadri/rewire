"""Joining an API change report to a repository index.

This is where Phase 1 and Phase 2 meet. Phase 1 knows a field named
``max_tokens`` was removed from ``POST /v1/chat/completions``; Phase 2 knows
every place that name occurs and how. This module decides which of those
occurrences the change actually breaks, and how sure it is.

Candidates are proposed by three independent strategies and then scored, rather
than being filtered as they are found. Proposing generously and scoring
strictly keeps recall high while leaving precision to a single, auditable model,
and it means a location found by two strategies is not reported twice.

Analysis only: nothing here writes to a file. The report is what Phase 4's agent
is given, and what Phase 8 measures against ground truth.
"""

from __future__ import annotations

import time
from pathlib import Path

from rewire.analyzers.models import FileInfo, FunctionCall, Reference, RepositoryIndex
from rewire.changes.models import ApiChange, ChangeReport, Severity
from rewire.core.logging import get_logger
from rewire.impact.models import (
    DEFAULT_MIN_CONFIDENCE,
    AffectedLocation,
    ChangeImpact,
    ImpactReport,
    MatchStrategy,
    Signal,
)
from rewire.impact.packages import resolve_packages
from rewire.impact.scoring import (
    confidence_from,
    direction_signal,
    enclosing_sdk_usage_signal,
    endpoint_mentioned_signal,
    imports_sdk_module_signal,
    in_test_code_signal,
    opaque_arguments_signal,
    order_signals,
    package_absent_signal,
    package_declared_signal,
    package_import_signal,
    reference_kind_signal,
    sdk_call_target_signal,
)

logger = get_logger(__name__)

#: Shortest endpoint path treated as a searchable literal. "/" and "/v1" occur
#: in unrelated strings often enough to be worthless as evidence.
MIN_SEARCHABLE_PATH_LENGTH = 4


class _FileContext:
    """Per-file facts reused across every candidate in that file.

    Computed once per file rather than per candidate: a file with fifty
    references would otherwise re-scan its imports and calls fifty times.
    """

    def __init__(
        self, file: FileInfo, packages: tuple[str, ...], sdk_modules: frozenset[str]
    ) -> None:
        self.file = file
        self.imported_packages = tuple(
            package
            for package in packages
            if any(record.is_from(package) for record in file.imports)
        )
        # Local modules this file imports that themselves call the SDK. A test
        # or a service layer sitting one hop from the API imports neither the
        # package nor anything resembling it, and would otherwise be
        # indistinguishable from an unrelated file that reuses the name.
        self.imported_sdk_modules = tuple(
            sorted(
                {
                    record.module
                    for record in file.imports
                    if not record.is_relative and record.module in sdk_modules
                }
            )
        )
        self._sdk_calls: list[FunctionCall] = []
        self._sdk_symbols: set[str] = set()
        for call in file.calls:
            if _call_targets_packages(call, packages):
                self._sdk_calls.append(call)
                if call.enclosing_symbol:
                    self._sdk_symbols.add(call.enclosing_symbol)
        self._mentions: dict[str, bool] = {}

    def sdk_call_at(self, line: int) -> FunctionCall | None:
        """Return the innermost SDK call whose expression contains ``line``.

        Matched by range rather than by start line: a multi-line call puts its
        arguments on lines the call itself does not begin on, which is the shape
        of every realistic SDK invocation. Innermost wins so that a call nested
        inside another is attributed to the one that actually receives the
        argument.
        """
        containing = [call for call in self._sdk_calls if call.spans(line)]
        if not containing:
            return None
        return min(containing, key=lambda call: (call.end_line - call.line, call.line))

    def symbol_uses_sdk(self, symbol: str | None) -> bool:
        """Whether ``symbol`` contains a call into one of the packages."""
        return symbol is not None and symbol in self._sdk_symbols

    def mentions(self, path: str) -> bool:
        """Whether the file contains ``path`` inside any string literal."""
        cached = self._mentions.get(path)
        if cached is None:
            cached = any(path in reference.name for reference in self.file.references)
            self._mentions[path] = cached
        return cached


def _modules_using_sdk(index: RepositoryIndex, packages: tuple[str, ...]) -> frozenset[str]:
    """Dotted module paths of files in this repository that call into ``packages``.

    Used to recognise a file that reaches the API indirectly. Module paths are
    matched against import statements, so a test importing ``app.client`` is
    linked to the file that defines it.
    """
    return frozenset(
        file.module
        for file in index.files
        if file.module and any(_call_targets_packages(call, packages) for call in file.calls)
    )


def _call_targets_packages(call: FunctionCall, packages: tuple[str, ...]) -> bool:
    target = call.resolved_callee
    if target is None:
        return False
    return any(target == package or target.startswith(f"{package}.") for package in packages)


def analyse_impact(
    report: ChangeReport,
    index: RepositoryIndex,
    *,
    packages: tuple[str, ...] = (),
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    min_severity: Severity = Severity.NON_BREAKING,
) -> ImpactReport:
    """Determine which parts of a repository an API change report affects.

    Args:
        report: Changes detected by :func:`rewire.changes.diff_specs`.
        index: Repository index from :func:`rewire.analyzers.build_index`.
        packages: Packages the API belongs to. Inferred from the specification
            title when omitted.
        min_confidence: Locations scoring below this are discarded.
        min_severity: Changes less severe than this are not analysed.

    Returns:
        An :class:`ImpactReport`. Changes with no surviving location are still
        included, so "this change affects nothing here" is visible rather than
        indistinguishable from "this change was never considered".
    """
    started = time.perf_counter()
    resolved = resolve_packages(report.new_spec.title, index, explicit=packages)
    declared = index.declared_dependency_names()
    sdk_modules = _modules_using_sdk(index, resolved)
    repository_modules = frozenset(index.imported_modules())
    contexts = {file.path: _FileContext(file, resolved, sdk_modules) for file in index.files}

    impacts = [
        _analyse_change(
            change, index, contexts, resolved, declared, repository_modules, min_confidence
        )
        for change in report.filter(min_severity)
    ]

    duration = time.perf_counter() - started
    result = ImpactReport.build(
        index.root, impacts, min_confidence=min_confidence, duration=duration
    )
    logger.info(
        "impact_analysed",
        repository=index.root,
        packages=list(resolved),
        changes=result.summary.changes_analysed,
        locations=result.summary.locations,
        files=result.summary.files_affected,
        duration_seconds=round(duration, 4),
    )
    return result


def _analyse_change(
    change: ApiChange,
    index: RepositoryIndex,
    contexts: dict[str, _FileContext],
    packages: tuple[str, ...],
    declared: set[str],
    repository_modules: frozenset[str],
    min_confidence: float,
) -> ChangeImpact:
    candidates: dict[tuple[str, int], AffectedLocation] = {}

    for location in _field_candidates(
        change, index, contexts, packages, declared, repository_modules
    ):
        _keep_best(candidates, location)
    for location in _endpoint_candidates(
        change, index, contexts, packages, declared, repository_modules
    ):
        _keep_best(candidates, location)

    surviving = sorted(
        (location for location in candidates.values() if location.confidence >= min_confidence),
        key=lambda location: location.sort_key,
    )
    return ChangeImpact(change=change, locations=tuple(surviving), packages=packages)


def _keep_best(
    candidates: dict[tuple[str, int], AffectedLocation], location: AffectedLocation
) -> None:
    """Record a candidate, keeping the higher-scoring one on a collision.

    Keyed by file and line rather than by exact column. ``max_tokens=max_tokens``
    yields two references on one line -- the keyword and the value it is bound to
    -- and reporting the same line twice at two confidences is noise to a reader
    and a double count in any per-line metric. The strongest interpretation of a
    line is the one that matters.
    """
    key = (location.file, location.line)
    existing = candidates.get(key)
    if existing is None or location.confidence > existing.confidence:
        candidates[key] = location


# ------------------------------------------------------------------- fields ---


def _field_candidates(
    change: ApiChange,
    index: RepositoryIndex,
    contexts: dict[str, _FileContext],
    packages: tuple[str, ...],
    declared: set[str],
    repository_modules: frozenset[str],
) -> list[AffectedLocation]:
    """Propose locations from occurrences of the changed field's name."""
    if not change.field:
        return []
    return [
        _score_reference(
            reference,
            change,
            contexts[reference.file],
            packages,
            declared,
            repository_modules,
        )
        for reference in index.find_references(change.field)
        if reference.file in contexts
    ]


def _score_reference(
    reference: Reference,
    change: ApiChange,
    context: _FileContext,
    packages: tuple[str, ...],
    declared: set[str],
    repository_modules: frozenset[str],
) -> AffectedLocation:
    signals: list[Signal] = [reference_kind_signal(reference.kind)]

    direction = direction_signal(change.location, reference.kind)
    if direction is not None:
        signals.append(direction)

    call = context.sdk_call_at(reference.line)
    if call is not None:
        signals.append(sdk_call_target_signal(call.resolved_callee or call.callee))
        if call.has_star_args:
            signals.append(opaque_arguments_signal(call.callee))

    signals.extend(_package_signals(context, packages, declared, repository_modules))

    path = change.path
    if path and len(path) >= MIN_SEARCHABLE_PATH_LENGTH and context.mentions(path):
        signals.append(endpoint_mentioned_signal(path))

    if call is None and context.symbol_uses_sdk(reference.enclosing_symbol):
        signals.append(enclosing_sdk_usage_signal(reference.enclosing_symbol or ""))

    if context.file.is_test:
        signals.append(in_test_code_signal(context.file.path))

    return AffectedLocation(
        file=reference.file,
        line=reference.line,
        column=reference.column,
        symbol=reference.enclosing_symbol,
        confidence=confidence_from(signals),
        strategy=MatchStrategy.FIELD_REFERENCE,
        signals=order_signals(signals),
        is_test=context.file.is_test,
    )


def _package_signals(
    context: _FileContext,
    packages: tuple[str, ...],
    declared: set[str],
    repository_modules: frozenset[str],
) -> list[Signal]:
    """Signals about the relationship between the file and the API's package.

    Emits nothing when no package could be attributed: with no package to look
    for, "this file does not import it" is not evidence of anything.
    """
    if not packages:
        return []

    signals: list[Signal] = []
    if context.imported_packages:
        signals.append(package_import_signal(context.imported_packages[0], imported=True))
    else:
        signals.append(package_import_signal(packages[0], imported=False))
        if context.imported_sdk_modules:
            signals.append(imports_sdk_module_signal(context.imported_sdk_modules[0]))

    for package in packages:
        if package.lower().replace("_", "-") in declared:
            signals.append(package_declared_signal(package))
            break
    else:
        # Only when the package leaves no trace anywhere: an explicitly supplied
        # package that the repository genuinely uses via a vendored copy should
        # not be argued against, but a package it has never heard of should be.
        if not any(package in repository_modules for package in packages):
            signals.append(package_absent_signal(packages[0]))
    return signals


# ---------------------------------------------------------------- endpoints ---


def _endpoint_candidates(
    change: ApiChange,
    index: RepositoryIndex,
    contexts: dict[str, _FileContext],
    packages: tuple[str, ...],
    declared: set[str],
    repository_modules: frozenset[str],
) -> list[AffectedLocation]:
    """Propose locations from literal uses of the changed endpoint's path.

    Only for changes to the operation as a whole -- an endpoint removed, a method
    gone. For a change to a single field the path is evidence *about the file*,
    not a location in its own right: the URL string does not need editing when
    a request key is renamed, and proposing it produced a false positive on every
    such change in the raw-HTTP benchmark case.
    """
    path = change.path
    if change.field or not path or len(path) < MIN_SEARCHABLE_PATH_LENGTH:
        return []

    found: list[AffectedLocation] = []
    for context in contexts.values():
        for reference in context.file.references:
            if path not in reference.name:
                continue
            signals: list[Signal] = [endpoint_mentioned_signal(path)]
            signals.extend(_package_signals(context, packages, declared, repository_modules))
            if context.symbol_uses_sdk(reference.enclosing_symbol):
                signals.append(enclosing_sdk_usage_signal(reference.enclosing_symbol or ""))
            if context.file.is_test:
                signals.append(in_test_code_signal(context.file.path))
            found.append(
                AffectedLocation(
                    file=reference.file,
                    line=reference.line,
                    column=reference.column,
                    symbol=reference.enclosing_symbol,
                    confidence=confidence_from(signals),
                    strategy=MatchStrategy.ENDPOINT_PATH,
                    signals=order_signals(signals),
                    is_test=context.file.is_test,
                )
            )
    return found


def attach_snippets(report: ImpactReport, root: Path | str) -> ImpactReport:
    """Return a copy of ``report`` with each location's source line attached.

    Kept separate from analysis so that scoring never depends on file contents
    read after indexing, and so a report can be produced without touching the
    filesystem again.
    """
    from rewire.analyzers.index import source_line

    impacts = tuple(
        impact.model_copy(
            update={
                "locations": tuple(
                    location.model_copy(
                        update={
                            "snippet": (
                                text.strip()
                                if (text := source_line(Path(root), location.file, location.line))
                                else None
                            )
                        }
                    )
                    for location in impact.locations
                )
            }
        )
        for impact in report.impacts
    )
    return report.model_copy(update={"impacts": impacts})


__all__ = ["MIN_SEARCHABLE_PATH_LENGTH", "analyse_impact", "attach_snippets"]
