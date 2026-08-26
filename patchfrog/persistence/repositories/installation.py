from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.persistence.models.installation import (
    BetaState,
    InstallationModel,
    InstallationStatus,
)


class InstallationRepository:
    async def get_by_github_id(
        self, session: AsyncSession, *, github_installation_id: int
    ) -> InstallationModel | None:
        result = await session.execute(
            select(InstallationModel).where(InstallationModel.github_installation_id == github_installation_id)
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        session: AsyncSession,
        *,
        github_installation_id: int,
        account_login: str,
        account_type: str,
        default_beta_state: BetaState = BetaState.ACTIVE,
    ) -> InstallationModel:
        """Create the installation row if missing (self-healing: an
        ``installation`` webhook event is the normal creation path, but a
        ``pull_request`` event can legitimately arrive first if delivery
        order isn't guaranteed -- see :mod:`patchfrog.ops.eligibility`'s
        module docstring), or reactivate a previously deleted one.
        ``default_beta_state`` lets the caller apply allowlist-mode
        policy (``BetaState.PENDING``) only on first creation -- never
        overwrites an operator's own later decision on an existing row.
        """

        existing = await self.get_by_github_id(session, github_installation_id=github_installation_id)
        if existing is not None:
            existing.account_login = account_login
            existing.account_type = account_type
            if existing.status is InstallationStatus.DELETED:
                existing.status = InstallationStatus.ACTIVE
            await session.flush()
            return existing

        model = InstallationModel(
            github_installation_id=github_installation_id,
            account_login=account_login,
            account_type=account_type,
            status=InstallationStatus.ACTIVE,
            beta_state=default_beta_state,
        )
        session.add(model)
        await session.flush()
        return model

    async def mark_status(
        self, session: AsyncSession, *, github_installation_id: int, status: InstallationStatus
    ) -> InstallationModel | None:
        model = await self.get_by_github_id(session, github_installation_id=github_installation_id)
        if model is None:
            return None
        model.status = status
        await session.flush()
        return model

    async def set_beta_state(
        self, session: AsyncSession, *, github_installation_id: int, beta_state: BetaState
    ) -> InstallationModel | None:
        model = await self.get_by_github_id(session, github_installation_id=github_installation_id)
        if model is None:
            return None
        model.beta_state = beta_state
        await session.flush()
        return model

    async def set_publication_allowed(
        self, session: AsyncSession, *, github_installation_id: int, allowed: bool
    ) -> InstallationModel | None:
        model = await self.get_by_github_id(session, github_installation_id=github_installation_id)
        if model is None:
            return None
        model.publication_allowed = allowed
        await session.flush()
        return model

    async def list_all(self, session: AsyncSession) -> list[InstallationModel]:
        result = await session.execute(select(InstallationModel).order_by(InstallationModel.created_at))
        return list(result.scalars().all())
