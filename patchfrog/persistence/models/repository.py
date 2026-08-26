from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.persistence.models.base import Base


class RepositoryModel(Base):
    """A GitHub repository known to PatchFrog via an App installation."""

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    github_repository_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    owner: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(511))
    #: The raw GitHub installation id -- a soft (value, not FK) join to
    #: ``InstallationModel.github_installation_id``, deliberately not a
    #: real foreign key: this column predates ``installations`` (Phase 1)
    #: and every existing row already carries a value, so adding a real
    #: FK would require a backfill; the value join is indexed on both
    #: sides and just as reliable for the read-only eligibility check
    #: that needs it (see :mod:`patchfrog.ops.eligibility`).
    installation_id: Mapped[int] = mapped_column(BigInteger)
    #: Whether this repository is currently selected for PatchFrog under
    #: its installation -- flipped by ``installation_repositories``
    #: webhook events (added/removed). Defaults ``True`` so every
    #: pre-existing row (and every row created outside the webhook path,
    #: e.g. local CLI use) keeps working unchanged; only a real
    #: "removed" event ever sets this ``False``.
    is_selected: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
