"""Milestone Y: durable snapshot of repository-scoped learning records.
See :mod:`patchfrog.learning_records.domain` for the pure domain model
this table stores a persisted view of -- this table stores **derived
conclusions only** (surface identity, maturity, support count, bounded
evidence *references*), never a copy of the underlying
``feedback_events``/``ai_findings`` rows themselves.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.analysis.domain import FindingCategory
from patchfrog.learning_records.domain import LearningMaturity, LearningType
from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base


class RepositoryLearningRecordModel(Base):
    __tablename__ = "repository_learning_records"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "learning_type",
            "surface_file_path",
            "surface_qualified_name",
            "surface_category",
            name="uq_repository_learning_record_surface",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    learning_type: Mapped[LearningType] = mapped_column(enum_column(LearningType, length=32))
    surface_file_path: Mapped[str] = mapped_column(String(1024))
    surface_qualified_name: Mapped[str] = mapped_column(String(512))
    surface_category: Mapped[FindingCategory] = mapped_column(enum_column(FindingCategory, length=32))
    maturity: Mapped[LearningMaturity] = mapped_column(enum_column(LearningMaturity, length=16))
    support_count: Mapped[int] = mapped_column(Integer)
    #: JSON-encoded tuple of {finding_id, review_run_id, observed_at} --
    #: bounded (MAX_EVIDENCE_PER_RECORD), references only, never a copy
    #: of finding/feedback content.
    evidence_json: Mapped[str] = mapped_column(Text)
    first_observed_at: Mapped[str] = mapped_column(String(64))
    last_observed_at: Mapped[str] = mapped_column(String(64))
    retired_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
