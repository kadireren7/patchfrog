"""Private Beta Validation Sprint -- Phase 7 incremental-review 3-commit
arc (case12-incremental-arc).

Drives the REAL production incremental-review path
(patchfrog.review_memory.service.IncrementalReviewMemoryService +
PullRequestReviewService.review_local(candidate_filter=...), exactly what
`patchfrog.cli review --incremental` calls) across three real commits on
one real repository:

  commit1: bug introduced (is_loyalty_tier's inverted comparison)
  commit2: unrelated change (a scratch README in the same directory)
  commit3: bug fixed

The scripted reviewer is stage-aware (not the generic case.yaml oracle,
which only matches on symbol name and can't tell "the bug is still
there" from "the bug was just fixed" for the same symbol) -- it returns
the case's one expected finding at stage 1 and 2, and zero findings at
stage 3, mirroring exactly what a real reviewer seeing the fixed code
would be expected to (not) find. Still never a live model call -- see
run_case.py's module docstring for why.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import asyncio  # noqa: E402

from patchfrog.analysis.service import StaticAnalysisService  # noqa: E402
from patchfrog.cli import _upsert_cli_pull_request, _upsert_cli_repository  # noqa: E402
from patchfrog.config.logging import configure_logging  # noqa: E402
from patchfrog.config.settings import get_settings  # noqa: E402
from patchfrog.indexing.service import RepositoryIndexingService  # noqa: E402
from patchfrog.persistence.database import create_engine, create_session_factory  # noqa: E402
from patchfrog.persistence.repositories import RepositoryIndexRepository  # noqa: E402
from patchfrog.repository.git import run_git  # noqa: E402
from patchfrog.review.config_resolution import resolve_repository_review_config  # noqa: E402
from patchfrog.review.local_diff import diff_against_base  # noqa: E402
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse  # noqa: E402
from patchfrog.review.service import PullRequestReviewService  # noqa: E402
from patchfrog.review_memory import config_resolution as _rm_config  # noqa: E402
from patchfrog.review_memory.service import IncrementalReviewMemoryService  # noqa: E402

resolve_repository_incremental_config = _rm_config.resolve_repository_incremental_config

_SYMBOL = "is_loyalty_tier"
_FILE = "patchfrog/ops/loyalty.py"
_ACCEPT_VERDICT = ScriptedResponse(
    raw_json=json.dumps(
        {"decision": "accept", "reasoning_summary": "oracle: matches committed ground truth"}
    )
)
_NO_FINDINGS = ScriptedResponse(raw_json=json.dumps({"findings": []}))


def _finding_response() -> ScriptedResponse:
    return ScriptedResponse(
        raw_json=json.dumps(
            {
                "findings": [
                    {
                        "title": "[oracle] inverted comparison",
                        "message": "is_loyalty_tier uses <= where the docstring requires >=",
                        "category": "correctness",
                        "severity": "medium",
                        "confidence": "high",
                        "file_path": _FILE,
                        "start_line": 11,
                        "end_line": 11,
                        "evidence": [
                            {
                                "file_path": _FILE,
                                "start_line": 11,
                                "end_line": 11,
                                "quoted_text": "return account_age_days <= minimum_age_days",
                            }
                        ],
                        "reasoning_summary": "docstring says 'at least minimum_age_days' (>=) but code checks <=",
                        "impact": None,
                        "suggested_fix": "use >= instead of <=",
                    }
                ]
            }
        )
    )


def _stage_aware_factory(stage_has_bug: bool) -> Callable[[object], ScriptedResponse]:
    def factory(request: object) -> ScriptedResponse:
        schema_name = getattr(request, "schema_name", None)
        if schema_name == "critic_verdict":
            return _ACCEPT_VERDICT
        prompt = getattr(request, "user_prompt", "")
        target = None
        for line in prompt.splitlines():
            if line.startswith("Review target: `"):
                target = line.split("`")[1]
                break
        if target is None:
            return _NO_FINDINGS
        matches = target == _SYMBOL or target.endswith(f".{_SYMBOL}") or target.endswith(f"::{_SYMBOL}")
        if matches and stage_has_bug:
            return _finding_response()
        return _NO_FINDINGS

    return factory


async def review_stage(
    *, repository_path: Path, full_name: str, base_ref: str, stage_has_bug: bool, stage_label: str
) -> dict[str, Any]:
    settings = get_settings()
    configure_logging(settings.log_level)
    t0 = time.monotonic()

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)

        index_service = RepositoryIndexingService(session_factory=session_factory)
        await index_service.index_local_repository(
            repository_id=repository_id, root_path=repository_path, repository_full_name=full_name
        )
        analysis_service = StaticAnalysisService(session_factory=session_factory)
        await analysis_service.analyze_local_repository(
            repository_id=repository_id, root_path=repository_path, repository_full_name=full_name
        )

        head_sha = run_git(["rev-parse", "HEAD"], cwd=repository_path).strip()
        base_sha = run_git(["rev-parse", base_ref], cwd=repository_path).strip()
        diff_files = diff_against_base(repository_path, base_ref)
        config = await resolve_repository_review_config(
            local=True, commit_sha=head_sha, repository_full_name=full_name, root_path=repository_path
        )

        async with session_factory() as session:
            active_index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert active_index is not None

        pull_request_id = await _upsert_cli_pull_request(
            session_factory, repository_id=repository_id, base_sha=base_sha, head_sha=head_sha
        )
        incremental_config = await resolve_repository_incremental_config(
            local=True, commit_sha=head_sha, repository_full_name=full_name, root_path=repository_path
        )
        memory_service = IncrementalReviewMemoryService(session_factory=session_factory)
        full_candidates = await memory_service.build_candidates(
            repository_id=repository_id,
            repository_index_id=active_index.id,
            commit_sha=head_sha,
            diff_files=diff_files,
            max_candidates=config.max_candidates,
        )
        prepared = await memory_service.prepare(
            pull_request_id=pull_request_id,
            repository_index_id=active_index.id,
            commit_sha=head_sha,
            clone_url=str(repository_path),
            token=None,
            current_candidates=full_candidates,
            reviewer_provider="fake-oracle",
            reviewer_model="oracle-v1",
            incremental_config=incremental_config,
        )

        reviewer = FakeLLMProvider(
            response_factory=_stage_aware_factory(stage_has_bug), provider_name="fake-oracle", model_id="oracle-v1"
        )
        service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer)
        summary = await service.review_local(
            repository_id=repository_id,
            root_path=repository_path,
            repository_full_name=full_name,
            commit_sha=head_sha,
            diff_files=diff_files,
            pull_request_id=pull_request_id,
            config=config,
            candidate_filter=prepared.candidate_filter,
            incremental_context_fingerprint=prepared.incremental_context_fingerprint,
        )
        if prepared.memory_tracking_active:
            await memory_service.finalize(
                review_run_id=summary.run_id,
                repository_id=repository_id,
                pull_request_id=pull_request_id,
                commit_sha=head_sha,
                prepared=prepared,
            )
    finally:
        await engine.dispose()

    duration_ms = (time.monotonic() - t0) * 1000
    return {
        "stage": stage_label,
        "head_sha": head_sha,
        "candidate_count": summary.candidate_count,
        "candidates_reviewed": summary.candidates_reviewed,
        "accepted_count": summary.accepted_count,
        "carried_forward_count": full_candidates and len(full_candidates) - summary.candidates_reviewed,
        "duration_ms": duration_ms,
        "provider_calls": summary.candidates_reviewed,
    }


async def main_async(repository_path: Path, full_name: str, output_path: Path) -> None:
    results = []

    run_git(["checkout", "--quiet", "case12-incremental-arc~2"], cwd=repository_path)
    results.append(
        await review_stage(
            repository_path=repository_path,
            full_name=full_name,
            base_ref="f7e4735e464e4c09752894a85a19c66456f2a8dc",
            stage_has_bug=True,
            stage_label="commit1_bug_introduced",
        )
    )

    run_git(["checkout", "--quiet", "case12-incremental-arc~1"], cwd=repository_path)
    results.append(
        await review_stage(
            repository_path=repository_path,
            full_name=full_name,
            base_ref="case12-incremental-arc~2",
            stage_has_bug=True,
            stage_label="commit2_unrelated_change",
        )
    )

    run_git(["checkout", "--quiet", "case12-incremental-arc"], cwd=repository_path)
    results.append(
        await review_stage(
            repository_path=repository_path,
            full_name=full_name,
            base_ref="case12-incremental-arc~1",
            stage_has_bug=False,
            stage_label="commit3_bug_fixed",
        )
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(main_async(args.repository, args.full_name, args.output))


if __name__ == "__main__":
    main()
