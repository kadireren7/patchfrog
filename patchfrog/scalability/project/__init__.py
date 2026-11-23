"""Application discovery: framework evidence from manifests and lockfiles,
static FastAPI route discovery (Milestone 2.3) and SQLAlchemy/SQLModel
structure discovery (Milestone 2.4). See docs/architecture/DISCOVERY.md for
the supported and unsupported patterns."""

from __future__ import annotations

from patchfrog.scalability.project.db_domain import DATABASE_SCHEMA_VERSION, DatabaseModel
from patchfrog.scalability.project.discovery import SourceReader, discover_application
from patchfrog.scalability.project.domain import DISCOVERY_SCHEMA_VERSION, ApplicationModel
from patchfrog.scalability.project.fastapi_routes import DiscoveryLimits

__all__ = [
    "DATABASE_SCHEMA_VERSION",
    "DISCOVERY_SCHEMA_VERSION",
    "ApplicationModel",
    "DatabaseModel",
    "DiscoveryLimits",
    "SourceReader",
    "discover_application",
]
