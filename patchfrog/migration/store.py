"""Idempotent persistence of migration plans and patches (M7.6 / M7.7).

Mirrors :mod:`patchfrog.upstream.store`'s pattern exactly:

- :meth:`MigrationStore.record_plan` -- one row per plan fingerprint (the
  originating change + repository + exact repository content state);
  re-planning the same state only refreshes ``last_generated_at``.
- :meth:`record_patch` -- one row per (plan, patch fingerprint); a
  regenerated identical patch is a no-op write.

``base_content_fingerprint`` -- a sha256 over the sorted (path, content)
pairs of every file any step of the plan targets -- is the plan's tie to
an *exact* repository state, independent of whether that state is a git
commit (a local, uncommitted checkout has none). It, together with the
originating change's fingerprint, is what :class:`PatchLinkage` gives M8
verification and M9 PR idempotency to key on.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.migration.domain import GeneratedPatch, MigrationPlan, PatchLinkage
from patchfrog.migration.generator import read_target_file
from patchfrog.migration.report import linkage_to_dict, safety_gate_to_dict, step_to_dict
from patchfrog.persistence.models.upstream import MigrationPatchModel, MigrationPlanModel

MAX_JSON_COLUMN_BYTES = 256_000
MAX_DIFF_COLUMN_BYTES = 512_000


def _bounded(value: object, limit: int = MAX_JSON_COLUMN_BYTES) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    if len(rendered.encode()) <= limit:
        return rendered
    return json.dumps({"truncated": True, "bytes": len(rendered.encode())})


async def _advisory_lock(session: AsyncSession, key: str) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(key.encode()).digest()[:8]
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF}
    )


def compute_base_content_fingerprint(root: Path, plan: MigrationPlan) -> str:
    """sha256 over every distinct file any step of ``plan`` targets, read
    the same secret-store-safe, bounded way the generator does. A file
    that cannot be read contributes a stable sentinel, never a crash."""

    resolved_root = root.resolve()
    pairs = []
    for file_path in sorted({s.target.file_path for s in plan.steps}):
        content = read_target_file(resolved_root, file_path)
        pairs.append((file_path, content if content is not None else "\0unreadable"))
    canonical = json.dumps(pairs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_linkage(
    plan: MigrationPlan, patch: GeneratedPatch, *, base_content_fingerprint: str, plan_fingerprint: str
) -> PatchLinkage:
    diff_items = sorted({key for s in plan.steps for key in s.diff_item_keys})
    usage_sites = sorted({s.usage_site_key for s in plan.steps if s.usage_site_key})
    return PatchLinkage(
        change_fingerprint=plan.change_fingerprint,
        dependency_keys=plan.dependency_keys,
        diff_item_keys=tuple(diff_items),
        usage_site_keys=tuple(usage_sites),
        repository=plan.repository,
        base_commit_sha=plan.commit_sha,
        base_content_fingerprint=base_content_fingerprint,
        plan_fingerprint=plan_fingerprint,
        patch_fingerprint=patch.fingerprint,
        engine_version=plan.engine_version,
    )


class MigrationStore:
    async def record_plan(
        self,
        session: AsyncSession,
        *,
        event_id: uuid.UUID,
        repository_id: uuid.UUID,
        plan: MigrationPlan,
        root: Path,
        generated_at: datetime | None = None,
    ) -> tuple[MigrationPlanModel, str, bool]:
        """Returns (row, base_content_fingerprint, created)."""

        base_fingerprint = compute_base_content_fingerprint(root, plan)
        fingerprint = plan.fingerprint(base_fingerprint)
        now = generated_at or datetime.now(UTC)
        await _advisory_lock(session, f"migration-plan:{fingerprint}")
        existing = await self.get_plan(session, fingerprint)
        if existing is not None:
            existing.last_generated_at = now
            await session.flush()
            return existing, base_fingerprint, False
        row = MigrationPlanModel(
            event_id=event_id,
            repository_id=repository_id,
            plan_fingerprint=fingerprint,
            engine_version=plan.engine_version,
            base_commit_sha=plan.commit_sha or "",
            base_content_fingerprint=base_fingerprint,
            dependency_keys_json=json.dumps(list(plan.dependency_keys)),
            status=plan.status.value,
            residual_risk=plan.residual_risk.value,
            step_count=len(plan.steps),
            auto_step_count=len(plan.automatic_steps),
            human_step_count=len(plan.unresolved_steps),
            steps_json=_bounded([step_to_dict(s) for s in plan.steps]),
            unresolved_json=_bounded([s.step_id for s in plan.unresolved_steps]),
            evidence_json=_bounded({k: list(v) for k, v in plan.evidence.items()}),
            created_at=now,
            last_generated_at=now,
        )
        session.add(row)
        await session.flush()
        return row, base_fingerprint, True

    async def get_plan(self, session: AsyncSession, fingerprint: str) -> MigrationPlanModel | None:
        return (
            await session.execute(select(MigrationPlanModel).where(MigrationPlanModel.plan_fingerprint == fingerprint))
        ).scalar_one_or_none()

    async def plans_for_event(self, session: AsyncSession, event_id: uuid.UUID) -> list[MigrationPlanModel]:
        query = (
            select(MigrationPlanModel)
            .where(MigrationPlanModel.event_id == event_id)
            .order_by(MigrationPlanModel.repository_id, MigrationPlanModel.created_at)
        )
        return list((await session.execute(query)).scalars().all())

    async def record_patch(
        self,
        session: AsyncSession,
        *,
        plan_id: uuid.UUID,
        patch: GeneratedPatch,
        linkage: PatchLinkage,
        created_at: datetime | None = None,
    ) -> tuple[MigrationPatchModel, bool]:
        now = created_at or datetime.now(UTC)
        await _advisory_lock(session, f"migration-patch:{plan_id}:{patch.fingerprint}")
        existing = (
            await session.execute(
                select(MigrationPatchModel).where(
                    MigrationPatchModel.plan_id == plan_id, MigrationPatchModel.patch_fingerprint == patch.fingerprint
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        diff: str | None = patch.unified_diff
        if diff is not None and len(diff.encode()) > MAX_DIFF_COLUMN_BYTES:
            diff = None
        row = MigrationPatchModel(
            plan_id=plan_id,
            patch_fingerprint=patch.fingerprint,
            origin=patch.origin.value,
            status="candidate" if patch.is_candidate else "rejected",
            unified_diff=diff,
            modified_files_json=json.dumps(list(patch.modified_files)),
            linkage_json=_bounded(linkage_to_dict(linkage)),
            safety_json=_bounded([safety_gate_to_dict(g) for g in patch.safety]),
            created_at=now,
        )
        session.add(row)
        await session.flush()
        return row, True

    async def patches_for_plan(self, session: AsyncSession, plan_id: uuid.UUID) -> list[MigrationPatchModel]:
        query = (
            select(MigrationPatchModel)
            .where(MigrationPatchModel.plan_id == plan_id)
            .order_by(MigrationPatchModel.created_at)
        )
        return list((await session.execute(query)).scalars().all())


__all__ = ["MAX_DIFF_COLUMN_BYTES", "MAX_JSON_COLUMN_BYTES", "MigrationStore", "build_linkage",
           "compute_base_content_fingerprint"]
