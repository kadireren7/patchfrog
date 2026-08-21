"""Evaluation runner -- orchestrates real production components against
one benchmark case and normalizes their output for
:mod:`patchfrog.evaluation.matcher`.

    case (+ mode, critic on/off, context ablation, provider)
    -> materialize fixture as a real git repo
    -> real RepositoryIndexingService / StaticAnalysisService /
       PullRequestReviewService (never duplicated, never reimplemented)
    -> normalized PredictedFinding list
    -> matcher.match_case
    -> CaseResult

No GitHub publishing, no mutation of any real repository -- every case
runs against its own throwaway temp-directory git repo, cleaned up
immediately after (see the module docstring of
:mod:`patchfrog.evaluation.fixtures`). This module is the one place
:mod:`patchfrog.evaluation` is allowed to depend on production review/
analysis/indexing code; the reverse dependency must never exist (see
``tests/unit/test_evaluation_no_label_leakage.py``, which proves the
reviewer prompt never receives benchmark ground truth).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.analysis.queries import AnalysisQueryService
from patchfrog.analysis.service import StaticAnalysisService
from patchfrog.context.config import ContextConfig
from patchfrog.diff.models import DiffFile
from patchfrog.diff.parser import build_diff_file
from patchfrog.evaluation.domain import (
    EVALUATION_BENCHMARK_VERSION,
    EVALUATION_ENGINE_VERSION,
    AnalyzerExecutionSummary,
    CaseResult,
    CaseStatus,
    EvaluationCase,
    EvaluationIdentity,
    EvaluationMode,
    PredictedFinding,
    PredictionSource,
)
from patchfrog.evaluation.fixtures import (
    BenchmarkValidationError,
    fixture_content_hash,
    fixture_info_for_case,
    materialize_case_repo,
    validate_case,
)
from patchfrog.evaluation.matcher import match_case
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models.analysis import FindingModel
from patchfrog.persistence.models.review import (
    AIFindingModel,
    AIFindingProposalModel,
    ReviewCandidateModel,
)
from patchfrog.persistence.repositories import (
    AIFindingProposalRepository,
    AIFindingRepository,
    RepositoryRepository,
    ReviewCandidateRepository,
)
from patchfrog.persistence.repositories.analysis_run import AnalysisRunRepository
from patchfrog.review.config import (
    REVIEW_ENGINE_VERSION,
    REVIEW_POLICY_VERSION,
    REVIEW_PROMPT_VERSION,
    ReviewConfig,
)
from patchfrog.review.domain import ReviewRunSummary
from patchfrog.review.provider import LLMProvider, ProviderError
from patchfrog.review.provider_factory import MissingProviderCredentialsError
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from patchfrog.review_memory.config import INCREMENTAL_REVIEW_ENGINE_VERSION, REVIEW_MEMORY_VERSION
from patchfrog.review_memory.domain import parse_evidence_json

logger = structlog.get_logger(__name__)

#: Any one of these being on ``PATH`` is enough to call the static
#: toolchain "available" for :class:`~patchfrog.evaluation.domain.EvaluationIdentity`
#: -- a coarse boolean, not a fingerprint (the real per-run toolchain
#: identity already lives in :mod:`patchfrog.analysis.toolchain`, folded
#: into each :class:`~patchfrog.persistence.models.analysis.AnalysisRunModel`
#: separately). See Phase 8 spec section 45: missing analyzers must be
#: reported as a missing capability, never silently treated as "zero
#: findings" -- this flag is what lets a report caption that distinction.
_STATIC_ANALYZER_BINARIES = ("ruff", "semgrep", "cppcheck", "clang-tidy")


def static_toolchain_available() -> bool:
    return any(shutil.which(binary) is not None for binary in _STATIC_ANALYZER_BINARIES)


def build_evaluation_identity(
    *,
    mode: EvaluationMode,
    reviewer_provider: LLMProvider,
    critic_enabled: bool,
    cases: Sequence[EvaluationCase],
    cases_root: Path,
) -> EvaluationIdentity:
    """Everything that must match for two evaluation runs to be
    comparable -- see the module docstring of
    :mod:`patchfrog.evaluation.regression`. ``cases`` determines
    ``case_fixture_hashes`` -- two runs over a different case subset (a
    ``--tag``/``--language``/``--case`` filtered run vs. the full corpus)
    are therefore never silently conflated."""

    return EvaluationIdentity(
        evaluation_benchmark_version=EVALUATION_BENCHMARK_VERSION,
        evaluation_engine_version=EVALUATION_ENGINE_VERSION,
        review_engine_version=REVIEW_ENGINE_VERSION,
        review_prompt_version=REVIEW_PROMPT_VERSION,
        review_policy_version=REVIEW_POLICY_VERSION,
        incremental_review_engine_version=INCREMENTAL_REVIEW_ENGINE_VERSION,
        review_memory_version=REVIEW_MEMORY_VERSION,
        reviewer_provider=reviewer_provider.identity.provider,
        reviewer_model=reviewer_provider.identity.model,
        critic_enabled=critic_enabled,
        static_toolchain_available=static_toolchain_available(),
        mode=mode,
        case_fixture_hashes={c.id: fixture_content_hash(c, cases_root=cases_root) for c in cases},
    )

_ALWAYS_NO_FINDINGS = ScriptedResponse(raw_json=json.dumps({"findings": []}))
_ACCEPT_VERDICT = ScriptedResponse(
    raw_json=json.dumps(
        {"decision": "accept", "reasoning_summary": "eval", "downgraded_severity": None, "downgraded_confidence": None}
    )
)


def _default_reviewer_provider() -> FakeLLMProvider:
    """A safe do-nothing provider for modes/callers that don't supply
    their own -- never used for real quality scoring, only so
    STATIC_ONLY runs (or infrastructure tests) never accidentally need a
    real credential."""

    return FakeLLMProvider(responses=[_ALWAYS_NO_FINDINGS])


def _synthetic_github_repository_id(case_id: str) -> int:
    """A fresh identity every call, never derived deterministically from
    just ``case_id`` -- each :meth:`EvaluationRunner.run_case` gets its
    own throwaway temp-directory git repo (see
    :func:`patchfrog.evaluation.fixtures.materialize_case_repo`), deleted
    at the end of the call. A stable per-case id would let
    :class:`~patchfrog.indexing.service.RepositoryIndexingService` try an
    *incremental* reindex against a prior run's ``commit_sha`` that no
    longer exists anywhere on disk (a real bug found running this corpus
    twice against the same case id) -- a fresh id per call guarantees
    every run starts from a clean, non-incremental index."""

    digest = hashlib.sha256(f"eval:{case_id}:{uuid.uuid4()}".encode()).digest()[:8]
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def build_whole_repo_diff(repo_root: Path, file_paths: Sequence[str]) -> list[DiffFile]:
    """Every fixture file treated as newly added -- every line is
    therefore a valid, changed, candidate-eligible line, so Phase 5's
    own diff-driven candidate generator considers the whole case, not
    just whatever a hand-crafted hunk happened to mark."""

    diff_files = []
    for rel_path in file_paths:
        content = (repo_root / rel_path).read_text(errors="replace")
        lines = content.splitlines()
        if not lines:
            continue
        patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)
        diff_files.append(build_diff_file(rel_path, patch))
    return diff_files


def _static_to_predicted(finding: FindingModel) -> PredictedFinding:
    return PredictedFinding(
        source=PredictionSource.STATIC,
        category=finding.category,
        severity=finding.severity,
        title=finding.title,
        message=finding.message,
        file_path=finding.file_path,
        start_line=finding.start_line,
        end_line=finding.end_line,
        symbol_qualified_name=finding.symbol_name,
        evidence_text=finding.message,
    )


def _ai_to_predicted(
    finding: AIFindingModel | AIFindingProposalModel, candidate: ReviewCandidateModel | None
) -> PredictedFinding:
    evidence = parse_evidence_json(finding.evidence)
    evidence_text = " ".join(e.quoted_text for e in evidence) or finding.message
    return PredictedFinding(
        source=PredictionSource.AI,
        category=finding.category,
        severity=finding.severity,
        title=finding.title,
        message=finding.message,
        file_path=finding.file_path,
        start_line=finding.start_line,
        end_line=finding.end_line,
        symbol_qualified_name=candidate.qualified_name if candidate is not None else None,
        evidence_text=evidence_text,
    )


class EvaluationRunner:
    """Stateful only in the sense of owning repository handles; safe to
    reuse across many :meth:`run_case` calls (each fully cleans up its
    own throwaway repo)."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._indexing = RepositoryIndexingService(session_factory=session_factory)
        self._static = StaticAnalysisService(session_factory=session_factory)
        self._analysis_queries = AnalysisQueryService()
        self._analysis_run_repo = AnalysisRunRepository()
        self._candidate_repo = ReviewCandidateRepository()
        self._proposal_repo = AIFindingProposalRepository()
        self._finding_repo = AIFindingRepository()

    async def run_case(
        self,
        case: EvaluationCase,
        *,
        cases_root: Path,
        mode: EvaluationMode,
        reviewer_provider: LLMProvider | None = None,
        critic_provider: LLMProvider | None = None,
        critic_enabled: bool = True,
        context_config_override: ContextConfig | None = None,
        timeout_seconds: float = 120.0,
    ) -> CaseResult:
        start = time.monotonic()
        try:
            return await asyncio.wait_for(
                self._run_case_inner(
                    case=case, cases_root=cases_root, mode=mode, reviewer_provider=reviewer_provider,
                    critic_provider=critic_provider, critic_enabled=critic_enabled,
                    context_config_override=context_config_override, start=start,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            return CaseResult(
                case_id=case.id, mode=mode, status=CaseStatus.TIMEOUT,
                duration_ms=(time.monotonic() - start) * 1000,
                error=f"case exceeded {timeout_seconds}s timeout", critic_enabled=critic_enabled,
            )
        except MissingProviderCredentialsError as exc:
            return CaseResult(
                case_id=case.id, mode=mode, status=CaseStatus.PROVIDER_ERROR,
                duration_ms=(time.monotonic() - start) * 1000, error=str(exc), critic_enabled=critic_enabled,
            )
        except ProviderError as exc:
            return CaseResult(
                case_id=case.id, mode=mode, status=CaseStatus.PROVIDER_ERROR,
                duration_ms=(time.monotonic() - start) * 1000, error=str(exc), critic_enabled=critic_enabled,
            )
        except BenchmarkValidationError as exc:
            return CaseResult(
                case_id=case.id, mode=mode, status=CaseStatus.FIXTURE_ERROR,
                duration_ms=(time.monotonic() - start) * 1000, error=str(exc), critic_enabled=critic_enabled,
            )
        except Exception as exc:  # infrastructure catch-all, never counted as a false negative
            logger.error("evaluation_case_infrastructure_error", case_id=case.id, error=str(exc))
            return CaseResult(
                case_id=case.id, mode=mode, status=CaseStatus.INFRASTRUCTURE_ERROR,
                duration_ms=(time.monotonic() - start) * 1000, error=str(exc), critic_enabled=critic_enabled,
            )

    async def _run_case_inner(
        self,
        *,
        case: EvaluationCase,
        cases_root: Path,
        mode: EvaluationMode,
        reviewer_provider: LLMProvider | None,
        critic_provider: LLMProvider | None,
        critic_enabled: bool,
        context_config_override: ContextConfig | None,
        start: float,
    ) -> CaseResult:
        errors = validate_case(case, cases_root=cases_root)
        if errors:
            raise BenchmarkValidationError("; ".join(errors))

        repo_root, commit_sha = materialize_case_repo(case, cases_root=cases_root)
        try:
            full_name = f"eval/{case.id}"
            async with self._session_factory() as session:
                repo_row = await RepositoryRepository().upsert(
                    session, github_repository_id=_synthetic_github_repository_id(case.id),
                    owner="eval", name=case.id, full_name=full_name, installation_id=0,
                )
                await session.commit()
                repository_id = repo_row.id

            await self._indexing.index_local_repository(
                repository_id=repository_id, root_path=repo_root, repository_full_name=full_name
            )

            fixture_info = fixture_info_for_case(case, cases_root=cases_root)

            static_predictions: list[PredictedFinding] = []
            analyzer_executions: list[AnalyzerExecutionSummary] = []
            if mode in (EvaluationMode.STATIC_ONLY, EvaluationMode.FULL_PIPELINE):
                await self._static.analyze_local_repository(
                    repository_id=repository_id, root_path=repo_root, repository_full_name=full_name
                )
                async with self._session_factory() as session:
                    analysis_run = await self._analysis_run_repo.get_latest_succeeded_for_commit(
                        session, repository_id=repository_id, commit_sha=commit_sha
                    )
                    if analysis_run is not None:
                        findings = await self._analysis_queries.get_findings_for_run(
                            session, analysis_run_id=analysis_run.id
                        )
                        static_predictions = [_static_to_predicted(f) for f in findings]
                        executions = await self._analysis_queries.get_analyzer_executions(
                            session, analysis_run_id=analysis_run.id
                        )
                        analyzer_executions = [
                            AnalyzerExecutionSummary(
                                analyzer=e.analyzer, status=e.status.value, raw_findings_count=e.raw_findings_count,
                            )
                            for e in executions
                        ]

            ai_predictions: list[PredictedFinding] = []
            proposals_predicted: list[PredictedFinding] = []
            candidates_generated = candidates_reviewed = candidates_skipped = 0
            provider_calls = 0
            reviewer_input_tokens = reviewer_output_tokens = 0

            if mode in (EvaluationMode.AI_ONLY, EvaluationMode.FULL_PIPELINE):
                provider = reviewer_provider or _default_reviewer_provider()
                effective_critic = critic_provider if critic_enabled else None
                diff_files = build_whole_repo_diff(repo_root, sorted(fixture_info.valid_file_paths))
                review_config = ReviewConfig(
                    provider=provider.identity.provider, model=provider.identity.model,
                    critic_enabled=effective_critic is not None,
                    critic_model=(effective_critic.identity.model if effective_critic else ReviewConfig().critic_model),
                    max_concurrent_requests=1,
                )
                service = PullRequestReviewService(
                    session_factory=self._session_factory, reviewer_provider=provider,
                    critic_provider=effective_critic,
                )
                summary: ReviewRunSummary = await service.review_local(
                    repository_id=repository_id, root_path=repo_root, repository_full_name=full_name,
                    commit_sha=commit_sha, diff_files=diff_files, config=review_config,
                    context_config_override=context_config_override,
                )

                async with self._session_factory() as session:
                    candidates = await self._candidate_repo.list_for_run(session, review_run_id=summary.run_id)
                    proposals = await self._proposal_repo.list_for_run(session, review_run_id=summary.run_id)
                    ai_findings = await self._finding_repo.list_for_run(session, review_run_id=summary.run_id)

                candidate_by_id = {c.id: c for c in candidates}
                candidates_generated = len(candidates)
                candidates_reviewed = summary.candidates_reviewed
                candidates_skipped = summary.candidates_skipped_budget
                provider_calls = summary.candidates_reviewed
                reviewer_input_tokens = summary.reviewer_usage.input_tokens
                reviewer_output_tokens = summary.reviewer_usage.output_tokens

                ai_predictions = [_ai_to_predicted(f, candidate_by_id.get(f.candidate_id)) for f in ai_findings]
                proposals_predicted = [
                    _ai_to_predicted(p, candidate_by_id.get(p.candidate_id)) for p in proposals
                ]

            all_predictions = static_predictions + ai_predictions
            prediction_outcomes, expected_outcomes = match_case(
                case=case, mode=mode, predictions=all_predictions,
                valid_file_paths=fixture_info.valid_file_paths, file_line_counts=fixture_info.file_line_counts,
            )

            status = CaseStatus.PASSED if not prediction_outcomes else CaseStatus.COMPLETED_WITH_FINDINGS
            return CaseResult(
                case_id=case.id, mode=mode, status=status, duration_ms=(time.monotonic() - start) * 1000,
                predictions=prediction_outcomes, expected_outcomes=expected_outcomes,
                proposals_before_validation=tuple(proposals_predicted), critic_enabled=critic_enabled,
                candidates_generated=candidates_generated, candidates_reviewed=candidates_reviewed,
                candidates_skipped=candidates_skipped, provider_calls=provider_calls,
                reviewer_input_tokens=reviewer_input_tokens, reviewer_output_tokens=reviewer_output_tokens,
                analyzer_executions=tuple(analyzer_executions),
            )
        finally:
            shutil.rmtree(repo_root, ignore_errors=True)

    async def run_suite(
        self,
        cases: Sequence[EvaluationCase],
        *,
        cases_root: Path,
        mode: EvaluationMode,
        reviewer_provider_factory: Callable[[EvaluationCase], LLMProvider] | None = None,
        critic_provider_factory: Callable[[EvaluationCase], LLMProvider] | None = None,
        critic_enabled: bool = True,
        context_config_override: ContextConfig | None = None,
        timeout_seconds: float = 120.0,
    ) -> list[CaseResult]:
        """Run every case in ``cases`` in turn, never concurrently --
        each case gets its own throwaway repository row/checkout (see
        :meth:`run_case`), so sequencing trades run time for the
        simplicity of never having to reason about two cases' database
        writes interleaving. ``reviewer_provider_factory``/
        ``critic_provider_factory`` are called once per case (not once
        for the whole suite) so each case can get its own oracle-scripted
        provider (see :mod:`patchfrog.evaluation.oracle`) or its own
        per-case live-provider instance."""

        results: list[CaseResult] = []
        for case in cases:
            reviewer = reviewer_provider_factory(case) if reviewer_provider_factory is not None else None
            critic = critic_provider_factory(case) if critic_provider_factory is not None else None
            results.append(
                await self.run_case(
                    case,
                    cases_root=cases_root,
                    mode=mode,
                    reviewer_provider=reviewer,
                    critic_provider=critic,
                    critic_enabled=critic_enabled,
                    context_config_override=context_config_override,
                    timeout_seconds=timeout_seconds,
                )
            )
        return results


def oracle_reviewer_provider_factory(*, cases_root: Path) -> Callable[[EvaluationCase], LLMProvider]:
    """The default reviewer for a corpus dogfood run: a fresh
    :class:`FakeLLMProvider` per case, scripted from that case's own
    ground truth via :func:`patchfrog.evaluation.oracle.build_oracle_response_factory`.
    Proves pipeline correctness, never AI quality (see the module
    docstring of :mod:`patchfrog.evaluation.oracle`)."""

    from patchfrog.evaluation.oracle import build_oracle_response_factory

    def factory(case: EvaluationCase) -> LLMProvider:
        return FakeLLMProvider(
            response_factory=build_oracle_response_factory(case, cases_root=cases_root),
            provider_name="fake-oracle",
            model_id="oracle-v1",
        )

    return factory

