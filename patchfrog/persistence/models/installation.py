"""Persisted GitHub App installation lifecycle (public-beta readiness).

One row per GitHub App installation -- the account-level counterpart to
``repositories`` (one installation covers one or more repositories, via
the existing soft ``installation_id`` join, never a hard FK -- see
:class:`~patchfrog.persistence.models.repository.RepositoryModel`'s
docstring for why).

Two independent lifecycle axes, deliberately kept separate:

- ``status``: what GitHub itself reports (installed / suspended by the
  account owner / uninstalled). PatchFrog never invents this state; it
  only ever reflects what an ``installation`` webhook event said.
- ``beta_state``: PatchFrog's own beta-program gate (Phase "public beta
  readiness" spec section 33) -- independent of whether GitHub considers
  the installation active. Defaults to ``ACTIVE`` (self-serve beta) or
  ``PENDING`` when allowlist mode is configured (see
  :mod:`patchfrog.ops.eligibility`), never something an installation can
  set for itself.

``publication_allowed`` is the beta-specific publication gate (spec
section 11/35): a repository's own ``.patchfrog.yml`` ``publish.enabled``
is necessary but never sufficient on its own in production -- both must
be true for a real GitHub write to ever happen. Defaults ``False``,
mirroring the existing safe-by-default philosophy from Phase 6.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from patchfrog.persistence.models._enum import enum_column
from patchfrog.persistence.models.base import Base


class InstallationStatus(StrEnum):
    """What GitHub itself reports for this installation -- reflects
    ``installation`` webhook events verbatim, never inferred."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class BetaState(StrEnum):
    """PatchFrog's own beta-program gate -- independent of
    :class:`InstallationStatus`."""

    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class InstallationModel(Base):
    """One GitHub App installation."""

    __tablename__ = "installations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    github_installation_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    account_login: Mapped[str] = mapped_column(String(255))
    account_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[InstallationStatus] = mapped_column(
        enum_column(InstallationStatus, length=16), default=InstallationStatus.ACTIVE
    )
    beta_state: Mapped[BetaState] = mapped_column(enum_column(BetaState, length=16), default=BetaState.ACTIVE)
    #: Beta-specific publication gate -- see the module docstring. Must
    #: be ``True`` *in addition to* the repository's own
    #: ``.patchfrog.yml`` ``publish.enabled`` for a real GitHub write to
    #: happen; neither one alone is sufficient.
    publication_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    #: ``None`` means "use the process-wide default" (see
    #: :mod:`patchfrog.ops.eligibility`) -- never a magic sentinel int.
    daily_review_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
