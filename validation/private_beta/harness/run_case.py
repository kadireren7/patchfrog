"""Private Beta Validation Sprint harness.

Drives the REAL production pipeline (RepositoryIndexingService ->
StaticAnalysisService -> PullRequestReviewService, exactly the classes
apps/worker/tasks/*.py and patchfrog/cli.py call in production/CLI use)
against a REAL git repository and a REAL base..head diff -- never a
synthetic single-file "fixture repo" the way the Phase 8 benchmark corpus
uses.

The one deliberate substitution: no live ANTHROPIC_API_KEY exists in this
environment (see docs/deployment.md's "Live model support" section, true
for every phase of this project). Every PR scenario driven by this script
therefore uses a scripted FakeLLMProvider in place of the real Claude API
call -- never invented, never claimed as real AI quality:

- controlled cases (--case-id) script the fake reviewer from a
  human-authored case.yaml (same schema as
  patchfrog.evaluation.fixtures/oracle, reused verbatim), producing real
  precision/recall/severity-calibration numbers over the real pipeline
  machinery. Labeled "pipeline_correctness" everywhere it's reported.
- natural cases (no --case-id) use a no-op reviewer that always returns
  zero AI findings -- there is no way to script "what a real model would
  have found" on code whose bugs (if any) are not already known, so this
  script never fabricates one. Only static-analyzer findings (Ruff /
  Semgrep / cppcheck / clang-tidy, all real, all live tools) are evaluated
  for natural PRs in this sprint.

Every other stage (git diff, indexing, static analysis, context building,
candidate generation, critic, persistence) is 100% the real production
code path, run against a real checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from patchfrog.cli import _analyze_local, _index_local, _upsert_cli_repository  # noqa: E402
from patchfrog.config.logging import configure_logging  # noqa: E402
from patchfrog.config.settings import get_settings  # noqa: E402
from patchfrog.evaluation.fixtures import load_case  # noqa: E402
from patchfrog.evaluation.oracle import build_oracle_response_factory  # noqa: E402
from patchfrog.persistence.database import create_engine, create_session_factory  # noqa: E402
from patchfrog.repository.git import run_git  # noqa: E402
from patchfrog.review.config_resolution import resolve_repository_review_config  # noqa: E402
from patchfrog.review.local_diff import diff_against_base  # noqa: E402
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse  # noqa: E402
from patchfrog.review.service import PullRequestReviewService  # noqa: E402

_NO_FINDINGS = ScriptedResponse(raw_json=json.dumps({"findings": []}))


def _noop_response_factory(request: object) -> ScriptedResponse:
    return _NO_FINDINGS


async def run(
    *,
    repository_path: Path,
    full_name: str,
    base_ref: str,
    case_yaml: Path | None,
    critic_enabled: bool,
    output_path: Path,
) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    t_total_start = time.monotonic()

    diff_files = diff_against_base(repository_path, base_ref)
    changed_files = len(diff_files)
    diff_bytes = sum(
        len(line.content) + 1 for f in diff_files for hunk in f.hunks for line in hunk.lines
    )

    t0 = time.monotonic()
    index_summary = await _index_local(repository_path=repository_path, full_name=full_name)
    index_ms = (time.monotonic() - t0) * 1000

    t0 = time.monotonic()
    analyze_summary = await _analyze_local(repository_path=repository_path, full_name=full_name)
    analyze_ms = (time.monotonic() - t0) * 1000

    head_sha = run_git(["rev-parse", "HEAD"], cwd=repository_path).strip()

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        repository_id = await _upsert_cli_repository(session_factory, full_name=full_name)
        config = await resolve_repository_review_config(
            local=True, commit_sha=head_sha, repository_full_name=full_name, root_path=repository_path
        )

        if case_yaml is not None:
            case = load_case(case_yaml.parent)
            reviewer_factory = build_oracle_response_factory(case, cases_root=case_yaml.parent.parent)
            mode = "pipeline_correctness"
        else:
            reviewer_factory = _noop_response_factory
            mode = "natural_static_only"

        reviewer_provider = FakeLLMProvider(
            response_factory=reviewer_factory, provider_name="fake-oracle", model_id="oracle-v1"
        )
        critic_provider = (
            FakeLLMProvider(response_factory=reviewer_factory, provider_name="fake-oracle", model_id="oracle-v1")
            if critic_enabled
            else None
        )

        service = PullRequestReviewService(
            session_factory=session_factory, reviewer_provider=reviewer_provider, critic_provider=critic_provider
        )

        t0 = time.monotonic()
        review_summary = await service.review_local(
            repository_id=repository_id,
            root_path=repository_path,
            repository_full_name=full_name,
            commit_sha=head_sha,
            diff_files=diff_files,
            config=config,
        )
        review_ms = (time.monotonic() - t0) * 1000
    finally:
        await engine.dispose()

    total_ms = (time.monotonic() - t_total_start) * 1000

    result = {
        "full_name": full_name,
        "base_ref": base_ref,
        "head_sha": head_sha,
        "mode": mode,
        "changed_files": changed_files,
        "diff_bytes": diff_bytes,
        "timings_ms": {
            "index": index_ms,
            "analyze": analyze_ms,
            "review": review_ms,
            "total": total_ms,
        },
        "index_summary": {
            "files_total": index_summary.files_total,
            "files_parsed": index_summary.files_parsed,
            "symbols_extracted": index_summary.symbols_extracted,
        },
        "analyze_summary": {
            "analyzers_succeeded": analyze_summary.analyzers_succeeded,
            "analyzers_failed": analyze_summary.analyzers_failed,
            "raw_findings_count": analyze_summary.raw_findings_count,
            "findings_count": analyze_summary.findings_count,
        },
        "review_summary": {
            "run_id": str(review_summary.run_id),
            "status": review_summary.status.value,
            "candidate_count": review_summary.candidate_count,
            "candidates_reviewed": review_summary.candidates_reviewed,
            "proposals_count": review_summary.proposals_count,
            "accepted_count": review_summary.accepted_count,
            "rejected_count": review_summary.rejected_count,
            "suppressed_duplicate_count": review_summary.suppressed_duplicate_count,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--case-yaml", type=Path, default=None)
    parser.add_argument("--critic", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(
        run(
            repository_path=args.repository,
            full_name=args.full_name,
            base_ref=args.base,
            case_yaml=args.case_yaml,
            critic_enabled=args.critic,
            output_path=args.output,
        )
    )


if __name__ == "__main__":
    main()
