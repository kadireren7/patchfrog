from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.persistence.models.base import Base


class PullRequestModel(Base):
    """A pull request PatchFrog has ingested at least once."""

    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repository_id", "github_pr_number", name="uq_pull_requests_repo_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id"), index=True)
    github_pr_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(1024))
    author: Mapped[str] = mapped_column(String(255))
    base_sha: Mapped[str] = mapped_column(String(40))
    head_sha: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
