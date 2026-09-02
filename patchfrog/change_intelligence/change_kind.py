"""Deterministic, evidence-based :class:`ChangeKind` classification.

Every rule here reads either repository *structure* (a file path
pattern, `IndexedFileModel.is_test`) or real *graph* evidence (a
cross-file caller) -- never source-code prose, never an LLM. See
``docs/change-intelligence.md`` for why the taxonomy stops at these
seven values and why "contract" specifically means "has a real
cross-file caller," not a naming guess.
"""

from __future__ import annotations

from patchfrog.change_intelligence.domain import ChangeKind

_CONFIG_PATH_MARKERS = ("config", "settings")
_CONFIG_EXTENSIONS = (".yml", ".yaml", ".toml", ".ini", ".env")
_INFRA_PATH_MARKERS = ("docker", ".github/workflows", "/ci/", "ci/", "deploy", "infra")
_PERSISTENCE_PATH_MARKERS = ("model", "models", "migration", "migrations", "schema", "persistence", "orm")


def _path_contains_any(path: str, markers: tuple[str, ...]) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in markers)


def classify_file_path(file_path: str, *, is_test: bool) -> ChangeKind | None:
    """Classification available from the file's own path/index metadata
    alone -- returns ``None`` when no path-level signal applies (the
    caller then falls back to graph-based classification, see
    :func:`classify_candidate`)."""

    if is_test:
        return ChangeKind.TEST
    # Infrastructure markers checked first: a `.yml`/`.yaml` file under
    # `.github/workflows/`, `docker/`, etc. is far more specifically
    # infrastructure than the generic "config file extension" signal
    # below would otherwise classify it as.
    if _path_contains_any(file_path, _INFRA_PATH_MARKERS):
        return ChangeKind.INFRASTRUCTURE
    if file_path.lower().endswith(_CONFIG_EXTENSIONS) or _path_contains_any(file_path, _CONFIG_PATH_MARKERS):
        return ChangeKind.CONFIGURATION
    if _path_contains_any(file_path, _PERSISTENCE_PATH_MARKERS):
        return ChangeKind.PERSISTENCE
    return None


def classify_candidate(*, file_path: str, is_test: bool, has_cross_file_caller: bool) -> ChangeKind:
    """The full per-candidate classification: path-based signals first
    (cheapest, most specific), then the one graph-based signal
    (cross-file caller -> CONTRACT), then the BEHAVIOR default."""

    path_kind = classify_file_path(file_path, is_test=is_test)
    if path_kind is not None:
        return path_kind
    if has_cross_file_caller:
        return ChangeKind.CONTRACT
    return ChangeKind.BEHAVIOR


def combine_kinds(kinds: list[ChangeKind]) -> ChangeKind:
    """A :class:`~patchfrog.change_intelligence.domain.ChangeUnit`'s own
    kind: the single shared kind if every constituent candidate agrees,
    otherwise ``MIXED``. Never averages, never picks a "majority" --
    a unit that touches both persistence and configuration code is
    genuinely mixed, not arbitrarily one or the other."""

    unique = set(kinds)
    if len(unique) == 1:
        return next(iter(unique))
    return ChangeKind.MIXED
