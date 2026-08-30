"""Incremental review memory configuration and versioning.

Mirrors :mod:`patchfrog.review.config`/:mod:`patchfrog.publishing.config`'s
split exactly: :class:`IncrementalConfig` captures the optional
``review.incremental``/``review.suppress_already_reported`` fields of a
repository's ``.patchfrog.yml`` (loaded the same untrusted-content-safe
way), while the module-level version constants capture PatchFrog's own
*effective* incremental-review toolchain -- a logic change to symbol
continuity, the memory resolver, or the incremental planner must
invalidate unsafe reuse the same way a prompt/model change does for
Phase 5 (see :class:`patchfrog.review.config.ReviewModelIdentity`) and a
comment-format change does for Phase 6 (see
:meth:`patchfrog.publishing.config.PublicationConfig.fingerprint`).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, ConfigDict

from patchfrog.review_memory.domain import IncrementalModePolicy

logger = structlog.get_logger(__name__)

_CONFIG_FILENAMES = (".patchfrog.yml", ".patchfrog.yaml")

DEFAULT_INCREMENTAL: IncrementalModePolicy = IncrementalModePolicy.AUTO
DEFAULT_SUPPRESS_ALREADY_REPORTED = True
DEFAULT_MEMORY_ENABLED = True

#: Bumped whenever patchfrog.review_memory.planner's selection logic
#: (which candidates get skipped under incremental mode) changes
#: materially.
INCREMENTAL_REVIEW_ENGINE_VERSION = 1

#: Bumped whenever patchfrog.review_memory.resolver/symbol_continuity's
#: carry-forward/resolved/changed decision logic changes materially.
REVIEW_MEMORY_VERSION = 1


class IncrementalConfig(BaseModel):
    """Effective incremental-review policy for one repository.

    ``memory_enabled=False`` disables Phase 7 entirely for this
    repository -- every review is full, exactly as if Phase 7 did not
    exist. This is the safe, always-available escape hatch.
    """

    model_config = ConfigDict(extra="ignore")

    incremental: IncrementalModePolicy = DEFAULT_INCREMENTAL
    suppress_already_reported: bool = DEFAULT_SUPPRESS_ALREADY_REPORTED
    memory_enabled: bool = DEFAULT_MEMORY_ENABLED


def load_incremental_config(repository_root: Path) -> IncrementalConfig:
    """Load ``review.incremental``/``review.suppress_already_reported`` and
    ``memory.enabled`` from ``.patchfrog.yml``/``.patchfrog.yaml``. Absent
    or malformed always falls back to safe defaults (memory enabled,
    ``auto`` mode) -- a bad config file degrades to "decide per-run
    whether memory is safely usable", never to a hard failure."""

    for filename in _CONFIG_FILENAMES:
        path = repository_root / filename
        if not path.is_file():
            continue

        try:
            raw = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("incremental_config_unreadable", path=str(path), error=str(exc))
            return IncrementalConfig()

        if raw is None or not isinstance(raw, dict):
            return IncrementalConfig()

        review_section = raw.get("review", {})
        memory_section = raw.get("memory", {})
        payload: dict[str, object] = {}
        if isinstance(review_section, dict):
            if "incremental" in review_section:
                payload["incremental"] = review_section["incremental"]
            if "suppress_already_reported" in review_section:
                payload["suppress_already_reported"] = review_section["suppress_already_reported"]
        if isinstance(memory_section, dict) and "enabled" in memory_section:
            payload["memory_enabled"] = memory_section["enabled"]

        try:
            return IncrementalConfig.model_validate(payload)
        except Exception as exc:
            logger.warning("incremental_config_invalid", path=str(path), error=str(exc))
            return IncrementalConfig()

    return IncrementalConfig()


def compute_memory_compatibility_fingerprint(
    *,
    review_engine_version: int,
    review_prompt_version: int,
    review_policy_version: int,
    reviewer_provider: str,
    reviewer_model: str,
    toolchain_fingerprint: str | None,
    context_engine_version: int,
) -> str:
    """Identity of the *effective* toolchain a review generation's memory
    is only safely comparable across -- see
    :class:`patchfrog.review_memory.domain.PreviousReviewSelection`'s
    ``compatibility_ok``. A change to any of these means "review
    unchanged candidates again" (candidate-skipping disabled), even
    though finding-lifecycle memory can still be retained for historical
    comparison.

    ``context_engine_version`` (see
    :data:`patchfrog.context.config.CONTEXT_ENGINE_VERSION`) closes a
    real gap found auditing Milestone E (adaptive multi-hop context):
    before this field existed, a context-engine change (candidate
    generation, scoring, budgeting, dedup, or -- as of this milestone --
    adaptive expansion) never invalidated incremental-review candidate
    skipping, even though the *context* a skipped candidate's memory was
    built from could be entirely different under the new engine. A
    change here forces "review unchanged candidates again," the same
    safe fallback any other compatibility-affecting change already
    triggers.
    """

    payload = {
        "incremental_review_engine_version": INCREMENTAL_REVIEW_ENGINE_VERSION,
        "review_memory_version": REVIEW_MEMORY_VERSION,
        "review_engine_version": review_engine_version,
        "review_prompt_version": review_prompt_version,
        "review_policy_version": review_policy_version,
        "reviewer_provider": reviewer_provider,
        "reviewer_model": reviewer_model,
        "toolchain_fingerprint": toolchain_fingerprint,
        "context_engine_version": context_engine_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


#: The stable identity used for review_runs.incremental_context_fingerprint
#: when a run is FULL with no previous generation involved at all -- the
#: common case for every review before Phase 7 existed, and for Phase 5's
#: own direct callers (CLI/tests) that never touch patchfrog.review_memory.
#: Computed once, not from any runtime state, so it is 100% stable across
#: processes/versions unless deliberately bumped here.
NO_MEMORY_CONTEXT_FINGERPRINT = hashlib.sha256(b"incremental:full:no-previous-generation:v1").hexdigest()


def compute_incremental_context_fingerprint(
    *, mode: str, previous_generation_id: str | None
) -> str:
    """Folds into a review run's own canonical identity (alongside
    ``config_fingerprint``/``model_fingerprint`` -- see
    :mod:`patchfrog.review.service`) so a full-mode result and an
    incremental-mode result for the identical commit are never
    accidentally treated as the same canonical run, and an incremental
    run tied to one previous generation is never reused for a *different*
    previous generation."""

    if mode == "full" and previous_generation_id is None:
        return NO_MEMORY_CONTEXT_FINGERPRINT
    payload = {
        "mode": mode,
        "previous_generation_id": previous_generation_id,
        "incremental_review_engine_version": INCREMENTAL_REVIEW_ENGINE_VERSION,
        "review_memory_version": REVIEW_MEMORY_VERSION,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
