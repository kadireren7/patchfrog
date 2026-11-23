"""PatchFrog Engine Public API v0 (ADR-017, docs/architecture/PUBLIC_API.md).

The engine names this repository promises to keep importable for any
downstream user (self-hosters, integrators, and the separately maintained
hosted product). Derived from the engine's own documentation and
architecture -- runtime entry points (Makefile/Dockerfile/docs), the
engine services named in docs/product-boundary.md, the agent-handoff MCP
server, and the approved core roots (BOUNDARIES.md R1) -- never from any
private consumer's import list.

Tiers (PUBLIC_API.md section 2):
* ``stable`` -- protected for the whole 0.x line: removing, renaming or an
  incompatible signature change needs the ADR-015 check and a deprecation
  window of at least one minor release.
* ``provisional`` -- importable and side-effect-free today (tested), but
  expected to evolve in Phases 1-2; may change in a minor release with a
  PUBLIC_API.md changelog entry and no deprecation window.
* ``deprecated`` -- still importable; kept for at least one minor release
  after deprecation, then removable via the ADR-015 check.

Changing this file is an API change: update PUBLIC_API.md in the same change.
"""

from __future__ import annotations

from typing import Literal

Tier = Literal["stable", "provisional", "deprecated"]

#: Version of this contract (semantic: additive -> minor; a stable removal or
#: incompatible change -> only after deprecation, recorded as a minor bump in 0.x).
PUBLIC_API_VERSION = "0.6.0"
Area = Literal["runtime", "configuration", "persistence", "services", "agent", "core"]

#: (module, name, area, tier)
PUBLIC_API_V0: tuple[tuple[str, str, Area, Tier], ...] = (
    # Runtime entry points (documented commands: Makefile, docker/Dockerfile, docs/deployment.md)
    ("apps.api.main", "app", "runtime", "stable"),
    ("apps.verifier.celery_app", "verifier_celery_app", "runtime", "stable"),
    ("apps.worker.celery_app", "celery_app", "runtime", "stable"),
    ("patchfrog.cli_scale", "main", "runtime", "provisional"),  # Milestone 2.5: python -m patchfrog scale
    ("patchfrog.cli", "main", "runtime", "stable"),
    # Configuration (ADR-011)
    ("patchfrog.config.core", "CoreSettings", "configuration", "provisional"),
    ("patchfrog.config.core", "get_core_settings", "configuration", "provisional"),
    ("patchfrog.config.logging", "configure_logging", "configuration", "stable"),
    ("patchfrog.config.settings", "ServerSettings", "configuration", "stable"),
    ("patchfrog.config.settings", "Settings", "configuration", "stable"),
    ("patchfrog.config.settings", "get_settings", "configuration", "stable"),
    # Persistence bootstrap (server mode, ADR-003)
    ("patchfrog.persistence.database", "create_engine", "persistence", "stable"),
    ("patchfrog.persistence.database", "create_session_factory", "persistence", "stable"),
    ("patchfrog.persistence.models", "Base", "persistence", "stable"),
    # Engine services named in docs/product-boundary.md ("Repository split")
    ("patchfrog.github.signatures", "verify_signature", "services", "stable"),
    ("patchfrog.merge_readiness.domain", "MergeReadinessResult", "services", "stable"),
    ("patchfrog.merge_readiness.service", "MergeReadinessService", "services", "stable"),
    ("patchfrog.ops.orchestrator", "schedule_pipeline_if_eligible", "services", "stable"),
    ("patchfrog.services.installation_sync", "InstallationSyncService", "services", "stable"),
    ("patchfrog.services.pull_request_ingestion", "IngestionOutcome", "services", "stable"),
    ("patchfrog.services.pull_request_ingestion", "PullRequestIngestionService", "services", "stable"),
    # Agent handoff (docs/agent-handoff.md)
    ("patchfrog.mcp.server", "PatchFrogMCPServer", "agent", "provisional"),
    # Core primitives (BOUNDARIES.md R1 core roots)
    ("patchfrog.analysis.subprocess_sandbox", "run_sandboxed", "core", "provisional"),
    # Local application layer (Milestones 2.1-2.4, docs/architecture/LOCAL_STORE.md, DISCOVERY.md)
    ("patchfrog.app.discovery", "DiscoveryResult", "core", "provisional"),
    ("patchfrog.app.discovery", "run_discovery", "core", "provisional"),
    ("patchfrog.app.jobs", "CancellationToken", "core", "provisional"),
    ("patchfrog.app.jobs", "FailureClass", "core", "provisional"),
    ("patchfrog.app.jobs", "JobBudget", "core", "provisional"),
    ("patchfrog.app.jobs", "JobContext", "core", "provisional"),
    ("patchfrog.app.jobs", "JobError", "core", "provisional"),
    ("patchfrog.app.jobs", "JobReport", "core", "provisional"),
    ("patchfrog.app.jobs", "JobResult", "core", "provisional"),
    ("patchfrog.app.jobs", "JobRunner", "core", "provisional"),
    ("patchfrog.app.jobs", "JobSpec", "core", "provisional"),
    ("patchfrog.app.jobs", "derive_idempotency_key", "core", "provisional"),
    ("patchfrog.app.revision", "RepositoryRevision", "core", "provisional"),
    ("patchfrog.app.revision", "RevisionCaptureError", "core", "provisional"),
    ("patchfrog.app.revision", "RevisionScope", "core", "provisional"),
    ("patchfrog.app.revision", "capture_revision", "core", "provisional"),
    ("patchfrog.app.store", "ArtifactRef", "core", "provisional"),
    ("patchfrog.app.store", "LocalRunStore", "core", "provisional"),
    ("patchfrog.app.store", "RunRecord", "core", "provisional"),
    ("patchfrog.app.store", "RunState", "core", "provisional"),
    ("patchfrog.app.store", "StoreError", "core", "provisional"),
    ("patchfrog.scalability.project", "ApplicationModel", "core", "provisional"),
    ("patchfrog.scalability.project", "DiscoveryLimits", "core", "provisional"),
    ("patchfrog.scalability.project", "DatabaseModel", "core", "provisional"),
    ("patchfrog.scalability.project.schema", "application_model_json_schema", "core", "provisional"),
    ("patchfrog.change_risk", "classify_change", "core", "provisional"),
    ("patchfrog.codegraph", "EMPTY_GRAPH", "core", "provisional"),
    ("patchfrog.codegraph", "LocalCodeGraph", "core", "provisional"),
    ("patchfrog.codegraph", "build_local_code_graph", "core", "provisional"),
    ("patchfrog.dependencies.files", "iter_repository_files", "core", "provisional"),
    ("patchfrog.dependencies.manifests", "parse_lockfile", "core", "provisional"),
    ("patchfrog.dependencies.manifests", "parse_manifest", "core", "provisional"),
    ("patchfrog.intelligence.graph", "build_graph", "core", "provisional"),
    ("patchfrog.intelligence.resolution", "RepositoryResolver", "core", "provisional"),
    ("patchfrog.parsing.registry", "default_registry", "core", "provisional"),
    ("patchfrog.repository.git", "run_git", "core", "provisional"),
    # Deprecated aliases (Milestone 1.3: moved to patchfrog.codegraph)
    ("patchfrog.upstream.code_graph", "EMPTY_GRAPH", "core", "deprecated"),
    ("patchfrog.upstream.code_graph", "LocalCodeGraph", "core", "deprecated"),
    ("patchfrog.upstream.code_graph", "build_local_code_graph", "core", "deprecated"),
)

#: Deprecated alias -> canonical location; the objects must be identical.
DEPRECATED_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("patchfrog.upstream.code_graph", "EMPTY_GRAPH"): ("patchfrog.codegraph", "EMPTY_GRAPH"),
    ("patchfrog.upstream.code_graph", "LocalCodeGraph"): ("patchfrog.codegraph", "LocalCodeGraph"),
    ("patchfrog.upstream.code_graph", "build_local_code_graph"): ("patchfrog.codegraph", "build_local_code_graph"),
}
