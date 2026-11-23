"""Application discovery as a JobRunner job (Milestone 2.3).

The use case the CLI and the MCP server will both call: capture the
revision, run :func:`patchfrog.scalability.project.discover_application`
inside a :class:`~patchfrog.app.jobs.JobRunner` job, and store the result as
the immutable ``discovery.json`` artifact of the run. Repeat requests for
the same revision, limits and engine version return the stored run without
re-analysing (JobRunner idempotency).

Every byte the engine sees comes from :meth:`JobContext.read_file`, so it
is verified against the revision (size and sha256, symlinks never
followed). A file that is too large, over the byte budget, or changed since
capture is skipped with a reason, and the run is then PARTIAL.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from patchfrog.app.jobs import (
    CancellationToken,
    JobBudget,
    JobContext,
    JobResult,
    JobRunner,
    JobSpec,
)
from patchfrog.app.revision import RevisionEntryChanged, RevisionScope
from patchfrog.app.store import LocalRunStore, RunRecord, RunState, StoreError
from patchfrog.scalability.project import (
    DISCOVERY_SCHEMA_VERSION,
    ApplicationModel,
    DiscoveryLimits,
    discover_application,
)

DISCOVERY_KIND = "discovery"
DISCOVERY_ARTIFACT = "discovery.json"
#: Version of the discovery semantics, one component per analysis (FastAPI
#: routes, SQLAlchemy/SQLModel structure). Part of the idempotency key, so a
#: change to what discovery reports never reuses an old run. Bump the
#: component that changes with any change to supported patterns or output.
DISCOVERY_RULESET_VERSION = "fastapi.1+sqlalchemy.1"


class DiscoveryArtifactError(ValueError):
    """A stored discovery artifact is unreadable, corrupt, inconsistent with
    its run, or of an unsupported schema version."""


class UnsupportedDiscoverySchemaError(DiscoveryArtifactError):
    """The artifact was written by a newer (or unknown) schema version."""


@dataclass(frozen=True)
class DiscoveryResult:
    job: JobResult
    #: None when the run produced no discovery artifact (FAILED or CANCELLED).
    model: ApplicationModel | None


class _BudgetedReader:
    """Revision-verified reads with the per-file and total byte budgets."""

    def __init__(self, ctx: JobContext, limits: DiscoveryLimits) -> None:
        self._ctx = ctx
        self._limits = limits
        self._sizes = {entry.path: entry.size for entry in ctx.revision.entries}
        self._counted: set[str] = set()
        self._total = 0
        self._skipped: dict[str, str] = {}

    @property
    def skipped(self) -> Mapping[str, str]:
        return self._skipped

    def read(self, path: str) -> bytes | None:
        self._ctx.checkpoint()
        if path in self._skipped:
            return None
        size = self._sizes[path]
        if size > self._limits.max_file_bytes:
            self._skipped[path] = "max_file_bytes"
            return None
        if path not in self._counted:
            if self._total + size > self._limits.max_total_bytes:
                self._skipped[path] = "max_total_bytes"
                return None
            self._counted.add(path)
            self._total += size
        try:
            return self._ctx.read_file(path, max_bytes=self._limits.max_file_bytes)
        except RevisionEntryChanged:
            self._skipped[path] = "changed_since_capture"
            return None


def discovery_config(limits: DiscoveryLimits) -> dict[str, object]:
    return {"discovery_schema_version": DISCOVERY_SCHEMA_VERSION, "limits": asdict(limits)}


def _limits_from(config: Mapping[str, object]) -> DiscoveryLimits:
    raw = config.get("limits")
    if not isinstance(raw, Mapping):
        raise ValueError("discovery config has no limits")
    return DiscoveryLimits(**{str(k): int(str(v)) for k, v in raw.items()})


def discovery_job(ctx: JobContext) -> None:
    limits = _limits_from(ctx.config)
    reader = _BudgetedReader(ctx, limits)
    model = discover_application(
        revision_id=ctx.revision.revision_id,
        paths=[entry.path for entry in ctx.revision.entries],
        reader=reader,
        limits=limits,
        checkpoint=ctx.checkpoint,
    )
    ctx.put_bytes(DISCOVERY_ARTIFACT, model.to_json_bytes())
    for path, reason in sorted(reader.skipped.items()):
        ctx.mark_partial(f"file skipped ({reason}): {path}")
    if model.coverage.limit_reached and not reader.skipped:
        ctx.mark_partial("a discovery limit was reached (see the model's limitations)")


def discovery_spec(
    root: Path,
    *,
    engine_version: str,
    limits: DiscoveryLimits | None = None,
    scope: RevisionScope | None = None,
    budget: JobBudget | None = None,
) -> JobSpec:
    return JobSpec(
        kind=DISCOVERY_KIND,
        root=root,
        engine_version=engine_version,
        config=discovery_config(limits or DiscoveryLimits()),
        ruleset_version=DISCOVERY_RULESET_VERSION,
        scope=scope or RevisionScope(),
        budget=budget or JobBudget(),
    )


def load_discovery(store: LocalRunStore, record: RunRecord) -> ApplicationModel | None:
    """The run's discovery model, or None when the run has none (FAILED,
    CANCELLED, INTERRUPTED, RUNNING). Raises :class:`DiscoveryArtifactError`
    for anything that is not a valid, supported artifact of *this* run;
    corrupt bytes are never returned as a model."""

    ref = record.artifacts.get(DISCOVERY_ARTIFACT)
    if ref is None or record.state not in (RunState.SUCCEEDED, RunState.PARTIAL):
        return None
    try:
        data = json.loads(store.read_bytes(ref, max_bytes=256 * 1024 * 1024))
    except (StoreError, ValueError) as exc:  # hash mismatch, missing object, oversized, not JSON
        raise DiscoveryArtifactError(f"discovery artifact is unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise DiscoveryArtifactError("discovery artifact is not a JSON object")
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version > DISCOVERY_SCHEMA_VERSION or version < 1:
        raise UnsupportedDiscoverySchemaError(
            f"discovery schema version {version!r} is not supported (supported: 1..{DISCOVERY_SCHEMA_VERSION})"
        )
    try:
        model = ApplicationModel.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise DiscoveryArtifactError(f"discovery artifact does not match schema {version}: {type(exc).__name__}") from exc
    if model.revision_id != record.revision_id:
        raise DiscoveryArtifactError("discovery artifact belongs to another revision")
    return model


def run_discovery(
    store: LocalRunStore,
    root: Path,
    *,
    engine_version: str,
    limits: DiscoveryLimits | None = None,
    scope: RevisionScope | None = None,
    budget: JobBudget | None = None,
    token: CancellationToken | None = None,
    runner: JobRunner | None = None,
) -> DiscoveryResult:
    spec = discovery_spec(root, engine_version=engine_version, limits=limits, scope=scope, budget=budget)
    result = (runner or JobRunner(store)).run(spec, discovery_job, token=token)
    return DiscoveryResult(job=result, model=load_discovery(store, result.record))


__all__ = [
    "DISCOVERY_ARTIFACT",
    "DISCOVERY_KIND",
    "DISCOVERY_RULESET_VERSION",
    "DiscoveryArtifactError",
    "DiscoveryResult",
    "UnsupportedDiscoverySchemaError",
    "discovery_job",
    "discovery_spec",
    "load_discovery",
    "run_discovery",
]
