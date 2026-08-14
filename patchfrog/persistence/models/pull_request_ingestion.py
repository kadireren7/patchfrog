from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.persistence.models.base import Base


class IngestionStatus(StrEnum):
    """Lifecycle status of a single webhook delivery's ingestion attempt."""

    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PullRequestIngestionModel(Base):
    """Record of a single webhook delivery being ingested.

    ``delivery_id`` carries a unique constraint — this is what makes
    re-processing a retried GitHub webhook delivery a safe no-op rather
    than duplicate work.
    """

    __tablename__ = "pull_request_ingestions"
    __table_args__ = (
        UniqueConstraint("delivery_id", name="uq_pull_request_ingestions_delivery_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    pull_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("pull_requests.id"), nullable=True
    )
    delivery_id: Mapped[str] = mapped_column(String(255), index=True)
    event_action: Mapped[str] = mapped_column(String(32))
    head_sha: Mapped[str] = mapped_column(String(40))
    status: Mapped[IngestionStatus] = mapped_column(
        SAEnum(
            IngestionStatus,
            native_enum=False,
            length=32,
            # Without this, SQLAlchemy persists the enum *member name*
            # (e.g. "IN_PROGRESS") instead of its value ("in_progress"),
            # silently diverging from the lowercase values declared in the
            # migration and from what raw SQL against the table expects.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        )
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
