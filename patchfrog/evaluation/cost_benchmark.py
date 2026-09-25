"""M4.10 deterministic review-cost benchmark.

Runs every scenario in ``tests/fixtures/cost_benchmark/scenarios.yaml``
through the *real* :class:`~patchfrog.review.service.PullRequestReviewService`
(real git diff, real indexing, real context engine, real persistence on a
private in-memory SQLite database) twice:

- ``before``: ``ReviewStrategy.SPECIALIST_FANOUT`` -- the exact pre-M4
  per-candidate Correctness+Security fan-out (no cost policy at all);
- ``after``: ``ReviewStrategy.COST_AWARE`` -- the M4 production default.

The reviewer is a deterministic fake that reports a scenario's scripted
finding whenever a reviewer prompt targets the finding's file and shows
the quoted text; the critic accepts. Token counts are deterministic
estimates of the *exact* prompts sent and responses returned
(:func:`patchfrog.context.tokens.estimate_tokens`), priced with a
synthetic, clearly-labelled rate card. **This measures PatchFrog's call
shape and budget behaviour, never model quality** -- the fake reviewer
already knows the answer. No network, no credentials, no paid calls.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from patchfrog.context.tokens import estimate_tokens
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.models import Base
from patchfrog.persistence.repositories import RepositoryRepository
from patchfrog.repository.git import run_git
from patchfrog.review.budget import ModelPricing, PricingCatalog
from patchfrog.review.config import ReviewConfig
from patchfrog.review.cost_policy import ReviewCostPolicy, ReviewStrategy
from patchfrog.review.domain import ReviewRunSummary
from patchfrog.review.local_diff import diff_against_base
from patchfrog.review.provider import ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse

DEFAULT_COST_BENCHMARK_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "cost_benchmark"
COST_BENCHMARK_VERSION = 1

#: Synthetic rate card -- a round, obviously-not-a-vendor number so the
#: estimated-cost column is comparable between strategies and never
#: mistaken for a real provider price.
BENCHMARK_PROVIDER = "benchmark"
BENCHMARK_MODEL = "benchmark-model"
BENCHMARK_PRICING = ModelPricing(input_usd_per_million_tokens=1.0, output_usd_per_million_tokens=4.0)

_ACCEPT = json.dumps(
    {"decision": "accept", "reasoning_summary": "benchmark: verified", "downgraded_severity": None,
     "downgraded_confidence": None}
)


@dataclass(frozen=True, slots=True)
class ScriptedFinding:
    path: str
    quote: str
    category: str
    severity: str
    confidence: str


@dataclass(frozen=True, slots=True)
class CostScenario:
    id: str
    description: str
    max_provider_calls: int
    edits: tuple[dict[str, str], ...] = ()
    finding: ScriptedFinding | None = None
    repeat_of: str | None = None


@dataclass(slots=True)
class StrategyResult:
    strategy: str
    risk_tier: str | None
    provider_calls: int
    reviewer_calls: int
    critic_calls: int
    retries: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float
    cache_hit: bool
    escalation_reasons: list[str] = field(default_factory=list)
    accepted_findings: int = 0
    status: str = ""
    budget_status: str = "within_budget"


@dataclass(slots=True)
class ScenarioResult:
    id: str
    description: str
    target_max_provider_calls: int
    before: StrategyResult
    after: StrategyResult

    @property
    def target_met(self) -> bool:
        return self.after.provider_calls <= self.target_max_provider_calls

    @property
    def finding_preserved(self) -> bool:
        return self.after.accepted_findings >= self.before.accepted_findings


def load_scenarios(root: Path = DEFAULT_COST_BENCHMARK_ROOT) -> list[CostScenario]:
    raw = yaml.safe_load((root / "scenarios.yaml").read_text())
    scenarios: list[CostScenario] = []
    for row in raw["scenarios"]:
        finding = row.get("finding")
        scenarios.append(
            CostScenario(
                id=str(row["id"]),
                description=str(row["description"]),
                max_provider_calls=int(row["max_provider_calls"]),
                edits=tuple(dict(edit) for edit in row.get("edits", ())),
                finding=ScriptedFinding(**finding) if finding else None,
                repeat_of=row.get("repeat_of"),
            )
        )
    return scenarios


def _apply_edits(root: Path, edits: tuple[dict[str, str], ...]) -> None:
    for edit in edits:
        target = root / edit["path"]
        if "write" in edit:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit["write"])
            continue
        text = target.read_text()
        if edit["find"] not in text:
            raise ValueError(f"benchmark edit anchor not found in {edit['path']}: {edit['find']!r}")
        target.write_text(text.replace(edit["find"], edit["replace"], 1))


def _git_init_commit(root: Path, message: str, *, init: bool) -> str:
    if init:
        run_git(["-C", str(root), "init", "--quiet"])
        run_git(["-C", str(root), "config", "user.email", "benchmark@patchfrog.invalid"])
        run_git(["-C", str(root), "config", "user.name", "PatchFrog Benchmark"])
    run_git(["-C", str(root), "add", "-A"])
    run_git(["-C", str(root), "commit", "--quiet", "--allow-empty", "-m", message])
    return run_git(["-C", str(root), "rev-parse", "HEAD"]).strip()


def _finding_json(finding: ScriptedFinding, head_root: Path) -> dict[str, Any]:
    lines = (head_root / finding.path).read_text().splitlines()
    line = next(i for i, text in enumerate(lines, start=1) if finding.quote in text)
    return {
        "title": f"benchmark {finding.category} finding",
        "message": f"`{finding.quote}` is wrong in {finding.path}",
        "category": finding.category,
        "severity": finding.severity,
        "confidence": finding.confidence,
        "file_path": finding.path,
        "start_line": line,
        "end_line": line,
        "evidence": [{"file_path": finding.path, "start_line": line, "end_line": line, "quoted_text": finding.quote}],
        "reasoning_summary": "Scripted benchmark defect.",
        "suggested_fix": None,
        "impact": None,
    }


def _provider(finding: ScriptedFinding | None, head_root: Path) -> FakeLLMProvider:
    finding_payload = _finding_json(finding, head_root) if finding is not None else None

    def respond(request: ProviderRequest) -> ScriptedResponse:
        if request.schema_name == "critic_verdict":
            raw = _ACCEPT
        else:
            shows_finding = (
                finding is not None
                and f"in `{finding.path}`" in request.user_prompt
                and finding.quote in request.user_prompt
            )
            raw = json.dumps({"findings": [finding_payload] if shows_finding else []})
        return ScriptedResponse(
            raw_json=raw,
            input_tokens=estimate_tokens(request.system_prompt) + estimate_tokens(request.user_prompt),
            output_tokens=estimate_tokens(raw),
        )

    return FakeLLMProvider(response_factory=respond, provider_name=BENCHMARK_PROVIDER, model_id=BENCHMARK_MODEL)


def _strategy_result(strategy: ReviewStrategy, summary: ReviewRunSummary) -> StrategyResult:
    report = summary.cost_report()
    return StrategyResult(
        strategy=strategy.value,
        risk_tier=summary.risk_tier,
        provider_calls=int(str(report["provider_calls"])),
        reviewer_calls=int(str(report["reviewer_calls"])),
        critic_calls=summary.critic_calls,
        retries=int(str(report["retries"])),
        estimated_input_tokens=int(str(report["estimated_input_tokens"])),
        estimated_output_tokens=int(str(report["estimated_output_tokens"])),
        estimated_cost_usd=round(float(str(report["estimated_cost_usd"])), 8),
        cache_hit=summary.reused_existing_run,
        escalation_reasons=list(summary.escalation_reasons),
        accepted_findings=summary.accepted_count,
        status=summary.status.value,
        budget_status=str(report["budget_status"]),
    )


class _ScenarioWorkspace:
    """One materialized base->head repository and one private database,
    shared by both strategies of a scenario (and by a repeat scenario)."""

    def __init__(self, fixture_root: Path, scenario: CostScenario) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix=f"pf-costbench-{scenario.id}-"))
        self.root = self.dir / "repo"
        shutil.copytree(fixture_root / "base_repo", self.root)
        _git_init_commit(self.root, "base", init=True)
        _apply_edits(self.root, scenario.edits)
        self.commit_sha = _git_init_commit(self.root, f"scenario {scenario.id}", init=False)
        self.diff_files = diff_against_base(self.root, "HEAD~1")
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )
        self.repository_id: uuid.UUID | None = None
        self.full_name = f"benchmark/{scenario.id}"

    async def prepare(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.session_factory() as session:
            row = await RepositoryRepository().upsert(
                session, github_repository_id=uuid.uuid4().int % (2**62), owner="benchmark",
                name=self.full_name.split("/")[1], full_name=self.full_name, installation_id=0,
            )
            await session.commit()
            self.repository_id = row.id
        await RepositoryIndexingService(session_factory=self.session_factory).index_local_repository(
            repository_id=row.id, root_path=self.root, repository_full_name=self.full_name
        )

    async def review(self, strategy: ReviewStrategy, provider: FakeLLMProvider) -> ReviewRunSummary:
        from patchfrog.review.service import PullRequestReviewService

        assert self.repository_id is not None
        service = PullRequestReviewService(
            session_factory=self.session_factory,
            reviewer_provider=provider,
            critic_provider=provider,
            pricing_catalog=PricingCatalog({f"{BENCHMARK_PROVIDER}/{BENCHMARK_MODEL}": BENCHMARK_PRICING}),
            cost_policy=ReviewCostPolicy() if strategy is ReviewStrategy.COST_AWARE else None,
        )
        return await service.review_local(
            repository_id=self.repository_id,
            root_path=self.root,
            repository_full_name=self.full_name,
            commit_sha=self.commit_sha,
            diff_files=self.diff_files,
            config=ReviewConfig(max_concurrent_requests=1),
        )

    async def close(self) -> None:
        await self.engine.dispose()
        shutil.rmtree(self.dir, ignore_errors=True)


async def run_cost_benchmark(
    root: Path = DEFAULT_COST_BENCHMARK_ROOT,
    *,
    progress: Callable[[str], None] | None = None,
) -> list[ScenarioResult]:
    scenarios = load_scenarios(root)
    by_id = {s.id: s for s in scenarios}
    workspaces: dict[str, _ScenarioWorkspace] = {}
    results: list[ScenarioResult] = []
    try:
        for scenario in scenarios:
            if progress is not None:
                progress(scenario.id)
            if scenario.repeat_of is not None:
                base_scenario = by_id[scenario.repeat_of]
                workspace = workspaces[base_scenario.id]
                finding = base_scenario.finding
            else:
                workspace = _ScenarioWorkspace(root, scenario)
                await workspace.prepare()
                workspaces[scenario.id] = workspace
                finding = scenario.finding
            outcomes: dict[ReviewStrategy, StrategyResult] = {}
            for strategy in (ReviewStrategy.SPECIALIST_FANOUT, ReviewStrategy.COST_AWARE):
                provider = _provider(finding, workspace.root)
                summary = await workspace.review(strategy, provider)
                outcome = _strategy_result(strategy, summary)
                if summary.reused_existing_run:
                    # An exact-head reuse made no new call in *this* run --
                    # never report the stored run's historical cost as new.
                    outcome.provider_calls = len(provider.calls)
                    outcome.reviewer_calls = sum(1 for c in provider.calls if c.schema_name != "critic_verdict")
                    outcome.critic_calls = sum(1 for c in provider.calls if c.schema_name == "critic_verdict")
                    outcome.retries = 0
                    outcome.estimated_input_tokens = 0
                    outcome.estimated_output_tokens = 0
                    outcome.estimated_cost_usd = 0.0
                    outcome.escalation_reasons = []
                outcomes[strategy] = outcome
            results.append(
                ScenarioResult(
                    id=scenario.id,
                    description=scenario.description,
                    target_max_provider_calls=scenario.max_provider_calls,
                    before=outcomes[ReviewStrategy.SPECIALIST_FANOUT],
                    after=outcomes[ReviewStrategy.COST_AWARE],
                )
            )
    finally:
        for workspace in workspaces.values():
            await workspace.close()
    return results


def build_cost_benchmark_report(results: list[ScenarioResult]) -> dict[str, Any]:
    def totals(side: str) -> dict[str, float]:
        rows = [getattr(r, side) for r in results]
        return {
            "provider_calls": sum(r.provider_calls for r in rows),
            "critic_calls": sum(r.critic_calls for r in rows),
            "estimated_input_tokens": sum(r.estimated_input_tokens for r in rows),
            "estimated_output_tokens": sum(r.estimated_output_tokens for r in rows),
            "estimated_cost_usd": round(sum(r.estimated_cost_usd for r in rows), 8),
            "accepted_findings": sum(r.accepted_findings for r in rows),
        }

    return {
        "benchmark": "m4_cost_benchmark",
        "benchmark_version": COST_BENCHMARK_VERSION,
        "label": (
            "deterministic call-shape/cost benchmark with a scripted fake reviewer and synthetic prices "
            "-- measures PatchFrog's execution cost, never model review quality"
        ),
        "pricing": {
            "provider_model": f"{BENCHMARK_PROVIDER}/{BENCHMARK_MODEL}",
            "input_usd_per_million_tokens": BENCHMARK_PRICING.input_usd_per_million_tokens,
            "output_usd_per_million_tokens": BENCHMARK_PRICING.output_usd_per_million_tokens,
            "synthetic": True,
        },
        "all_targets_met": all(r.target_met for r in results),
        "all_findings_preserved": all(r.finding_preserved for r in results),
        "totals": {"before": totals("before"), "after": totals("after")},
        "scenarios": [
            {
                "id": r.id,
                "description": r.description,
                "target_max_provider_calls": r.target_max_provider_calls,
                "target_met": r.target_met,
                "finding_preserved": r.finding_preserved,
                "before": asdict(r.before),
                "after": asdict(r.after),
            }
            for r in results
        ],
    }


def render_cost_benchmark_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# M4 cost benchmark (deterministic, fake provider, synthetic prices)",
        "",
        f"_{report['label']}_",
        "",
        "| scenario | tier (after) | calls before -> after | critic before -> after | "
        "input tokens before -> after | est. USD before -> after | findings before/after | target | met |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["scenarios"]:
        b, a = row["before"], row["after"]
        lines.append(
            f"| {row['id']} | {a['risk_tier']} | {b['provider_calls']} -> {a['provider_calls']} | "
            f"{b['critic_calls']} -> {a['critic_calls']} | {b['estimated_input_tokens']} -> "
            f"{a['estimated_input_tokens']} | {b['estimated_cost_usd']:.6f} -> {a['estimated_cost_usd']:.6f} | "
            f"{b['accepted_findings']}/{a['accepted_findings']} | <= {row['target_max_provider_calls']} | "
            f"{'yes' if row['target_met'] else 'NO'} |"
        )
    t = report["totals"]
    lines += [
        "",
        f"Totals: provider calls {t['before']['provider_calls']} -> {t['after']['provider_calls']}; "
        f"input tokens {t['before']['estimated_input_tokens']} -> {t['after']['estimated_input_tokens']}; "
        f"est. USD {t['before']['estimated_cost_usd']:.6f} -> {t['after']['estimated_cost_usd']:.6f}; "
        f"accepted findings {t['before']['accepted_findings']} -> {t['after']['accepted_findings']}.",
        f"All targets met: {report['all_targets_met']}. All findings preserved: {report['all_findings_preserved']}.",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "COST_BENCHMARK_VERSION",
    "DEFAULT_COST_BENCHMARK_ROOT",
    "CostScenario",
    "ScenarioResult",
    "StrategyResult",
    "build_cost_benchmark_report",
    "load_scenarios",
    "render_cost_benchmark_markdown",
    "run_cost_benchmark",
]
