"""Cross-Repo Intelligence Foundation's own persistence -- the smallest
addition needed to make an explicit, cross-repository contract
dependency provable (see
``validation/cross_repo_intelligence/latest-summary.md`` sections 9,
16). Both tables are populated **only** via the trusted operator CLI
path (:mod:`patchfrog.cli`'s ``cross-repo`` subcommands) -- never from
``.patchfrog.yml`` or any other PR-influenced source (section 12: a PR
under review must never be able to expand PatchFrog's repository
access scope).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.contract_intelligence.domain import ContractKind
from patchfrog.cross_repo_intelligence.domain import (
    RepositoryRelationKind,
    RepositoryRelationProvenance,
)
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base


class RepositoryContractKeyModel(Base):
    """An operator-registered, stable, cross-repository-meaningful name
    for one exact symbol in one repository (e.g. ``"payments.capture:v1"``
    naming ``patchfrog/billing/capture.py::capture_payment``). Never
    derived automatically, never from an LLM, never from freeform prose
    -- see the module docstring."""

    __tablename__ = "repository_contract_keys"
    __table_args__ = (
        UniqueConstraint("repository_id", "stable_key", name="uq_repository_contract_keys_repo_key"),
        Index(
            "ix_repository_contract_keys_repo_symbol", "repository_id", "file_path", "qualified_name"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"), index=True)
    contract_kind: Mapped[ContractKind] = mapped_column(enum_column(ContractKind, length=32))
    stable_key: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(1024))
    qualified_name: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepositoryRelationModel(Base):
    """An operator-registered, directional, explicit dependency from
    ``source_repository_id`` (the producer of ``external_contract_key``)
    to ``target_repository_id`` (a consumer of it). Direction is never
    inferred, never symmetric, and never transitively combined with
    another relation row (see the audit's own "no transitive inference"
    section)."""

    __tablename__ = "repository_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_repository_id", "target_repository_id", "relation_kind", "external_contract_key",
            name="uq_repository_relations_identity",
        ),
        Index("ix_repository_relations_source_active", "source_repository_id", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"), index=True)
    target_repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"), index=True)
    relation_kind: Mapped[RepositoryRelationKind] = mapped_column(
        enum_column(RepositoryRelationKind, length=32)
    )
    external_contract_key: Mapped[str] = mapped_column(String(255))
    provenance: Mapped[RepositoryRelationProvenance] = mapped_column(
        enum_column(RepositoryRelationProvenance, length=32)
    )
    #: Live-checked on every query, never cached -- flipping this
    #: `False` (via the CLI's own remove/deactivate path) takes effect
    #: on the very next review, with no separate retire lifecycle
    #: needed (section 43).
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
