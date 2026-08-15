from __future__ import annotations

from patchfrog.persistence.repositories.call_reference import CallReferenceRepository
from patchfrog.persistence.repositories.import_reference import ImportReferenceRepository
from patchfrog.persistence.repositories.indexed_file import IndexedFileRepository
from patchfrog.persistence.repositories.parsed_file_cache import ParsedFileCacheRepository
from patchfrog.persistence.repositories.pull_request import PullRequestRepository
from patchfrog.persistence.repositories.pull_request_ingestion import (
    PullRequestIngestionRepository,
)
from patchfrog.persistence.repositories.repository import RepositoryRepository
from patchfrog.persistence.repositories.repository_edge import RepositoryEdgeRepository
from patchfrog.persistence.repositories.repository_index import RepositoryIndexRepository
from patchfrog.persistence.repositories.symbol import SymbolRepository

__all__ = [
    "CallReferenceRepository",
    "ImportReferenceRepository",
    "IndexedFileRepository",
    "ParsedFileCacheRepository",
    "PullRequestIngestionRepository",
    "PullRequestRepository",
    "RepositoryEdgeRepository",
    "RepositoryIndexRepository",
    "RepositoryRepository",
    "SymbolRepository",
]
