from __future__ import annotations

from patchfrog.persistence.repositories.ai_finding import AIFindingRepository
from patchfrog.persistence.repositories.ai_finding_proposal import AIFindingProposalRepository
from patchfrog.persistence.repositories.analysis_run import AnalysisRunRepository
from patchfrog.persistence.repositories.analyzer_execution import AnalyzerExecutionRepository
from patchfrog.persistence.repositories.call_reference import CallReferenceRepository
from patchfrog.persistence.repositories.context_bundle import ContextBundleRepository
from patchfrog.persistence.repositories.context_item import ContextItemRepository
from patchfrog.persistence.repositories.critic_verdict import CriticVerdictRepository
from patchfrog.persistence.repositories.feedback import (
    FeedbackAssessmentRepository,
    FeedbackEventRepository,
)
from patchfrog.persistence.repositories.finding import FindingRepository
from patchfrog.persistence.repositories.finding_source import FindingSourceRepository
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
from patchfrog.persistence.repositories.review_candidate import ReviewCandidateRepository
from patchfrog.persistence.repositories.review_generation import ReviewGenerationRepository
from patchfrog.persistence.repositories.review_memory_finding import ReviewMemoryFindingRepository
from patchfrog.persistence.repositories.review_memory_transition import (
    ReviewMemoryTransitionRepository,
)
from patchfrog.persistence.repositories.review_publication import ReviewPublicationRepository
from patchfrog.persistence.repositories.review_publication_comment import (
    ReviewPublicationCommentRepository,
)
from patchfrog.persistence.repositories.review_run import ReviewRunRepository
from patchfrog.persistence.repositories.symbol import SymbolRepository

__all__ = [
    "AIFindingProposalRepository",
    "AIFindingRepository",
    "AnalysisRunRepository",
    "AnalyzerExecutionRepository",
    "CallReferenceRepository",
    "ContextBundleRepository",
    "ContextItemRepository",
    "CriticVerdictRepository",
    "FeedbackAssessmentRepository",
    "FeedbackEventRepository",
    "FindingRepository",
    "FindingSourceRepository",
    "ImportReferenceRepository",
    "IndexedFileRepository",
    "ParsedFileCacheRepository",
    "PullRequestIngestionRepository",
    "PullRequestRepository",
    "RepositoryEdgeRepository",
    "RepositoryIndexRepository",
    "RepositoryRepository",
    "ReviewCandidateRepository",
    "ReviewGenerationRepository",
    "ReviewMemoryFindingRepository",
    "ReviewMemoryTransitionRepository",
    "ReviewPublicationCommentRepository",
    "ReviewPublicationRepository",
    "ReviewRunRepository",
    "SymbolRepository",
]
