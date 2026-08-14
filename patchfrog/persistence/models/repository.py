from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
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
    installation_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
