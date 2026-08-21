"""Multi-commit incremental-review-memory benchmark scenarios (Phase 8
spec sections 20-21).

Drives real production components exactly the way
``tests/integration/test_review_memory_end_to_end.py`` already proves
correct for one lifecycle -- real throwaway git repositories, real
:class:`~patchfrog.indexing.service.RepositoryIndexingService`, real
:class:`~patchfrog.review_memory.service.IncrementalReviewMemoryService`,
real :class:`~patchfrog.review.service.PullRequestReviewService` --  with
:class:`~patchfrog.review.providers.fake.FakeLLMProvider` standing in for
the reviewer/critic, scripted per step from that step's own declared
ground truth (never a second, hidden answer key).

Each scenario asserts the one invariant the Phase 8 spec cares about most:
a *wrong* carry-forward (memory says CARRIED_FORWARD/RESOLVED but that
doesn't match what's actually true of the code at that commit) is worse
than an extra LLM call -- ``unsafe_carry_forward`` must always be
``False``. Provider-call savings are recorded but never allowed to excuse
an unsafe transition.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from patchfrog.evaluation.domain import IncrementalScenarioResult
from patchfrog.indexing.service import RepositoryIndexingService
from patchfrog.persistence.repositories import (
    PullRequestRepository,
    RepositoryIndexRepository,
    RepositoryRepository,
)
from patchfrog.persistence.repositories.review_memory_finding import ReviewMemoryFindingRepository
from patchfrog.repository.git import run_git
from patchfrog.review.config import ReviewConfig
from patchfrog.review.local_diff import diff_against_base
from patchfrog.review.provider import ProviderRequest
from patchfrog.review.providers.fake import FakeLLMProvider, ScriptedResponse
from patchfrog.review.service import PullRequestReviewService
from patchfrog.review_memory.config import IncrementalConfig
from patchfrog.review_memory.domain import FindingMemoryStatus, IncrementalRunMode
from patchfrog.review_memory.service import IncrementalReviewMemoryService

_NO_FINDINGS = ScriptedResponse(raw_json=json.dumps({"findings": []}))
_ACCEPT_VERDICT = ScriptedResponse(
    raw_json=json.dumps(
        {"decision": "accept", "reasoning_summary": "scenario oracle", "downgraded_severity": None, "downgraded_confidence": None}
    )
)


def _init_git_repo(root: Path) -> None:
    run_git(["-C", str(root), "init", "--quiet"])
    run_git(["-C", str(root), "config", "user.email", "eval@patchfrog.dev"])
    run_git(["-C", str(root), "config", "user.name", "PatchFrog Eval"])


def _commit_all(root: Path, message: str) -> str:
    run_git(["-C", str(root), "add", "-A"])
    run_git(["-C", str(root), "commit", "--quiet", "-m", message])
    return run_git(["-C", str(root), "rev-parse", "HEAD"]).strip()


def _rev_parse(root: Path, ref: str) -> str:
    return run_git(["-C", str(root), "rev-parse", ref]).strip()


def oracle_finding(
    *, title: str, file_path: str, start_line: int, end_line: int, quoted_text: str,
    category: str = "correctness", severity: str = "high",
) -> dict[str, Any]:
    return {
        "title": title,
        "message": f"{title} (scenario ground truth)",
        "category": category,
        "severity": severity,
        "confidence": "high",
        "file_path": file_path,
        "start_line": start_line,
        "end_line": end_line,
        "evidence": [
            {"file_path": file_path, "start_line": start_line, "end_line": end_line, "quoted_text": quoted_text}
        ],
        "reasoning_summary": "scenario oracle",
        "suggested_fix": None,
    }


def _review_target(user_prompt: str) -> str | None:
    for line in user_prompt.splitlines():
        if line.startswith("Review target: `"):
            return line.split("`")[1]
    return None


def _make_provider_pair(findings_by_symbol: dict[str, dict[str, Any]]) -> tuple[FakeLLMProvider, FakeLLMProvider]:
    def factory(request: ProviderRequest) -> ScriptedResponse:
        if request.schema_name == "critic_verdict":
            return _ACCEPT_VERDICT
        target = _review_target(request.user_prompt)
        if target is not None:
            for symbol, finding in findings_by_symbol.items():
                if target == symbol or target.endswith(f".{symbol}") or target.endswith(f"::{symbol}"):
                    return ScriptedResponse(raw_json=json.dumps({"findings": [finding]}))
        return _NO_FINDINGS

    return FakeLLMProvider(response_factory=factory), FakeLLMProvider(response_factory=factory)


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    """One commit in a scenario. ``mutate`` edits the repo root in place
    (whole-file writes/deletes/renames -- whatever this step needs);
    ``findings_by_symbol`` is this step's ground truth: a bare symbol
    name -> the oracle finding JSON the reviewer should report *if* that
    symbol is actually sent to the reviewer this step. A symbol absent
    from this mapping is scripted as "no finding" if reviewed."""

    label: str
    mutate: Callable[[Path], None] = lambda root: None
    findings_by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: If set, the PR row's ``base_sha`` is updated to this value before
    #: this step's review runs (the "base change" scenario's hook) --
    #: ``None`` (the default) leaves the PR's base untouched.
    rebase_base_to: Callable[[Path], str] | None = None
    #: If set, called instead of a normal commit -- e.g. a force-push
    #: (``git reset --hard`` + new commit). Returns the new HEAD sha.
    #: When set, ``mutate``/normal commit are skipped for this step.
    custom_commit: Callable[[Path], str] | None = None


@dataclass(frozen=True, slots=True)
class StepObservation:
    label: str
    mode: IncrementalRunMode
    ancestry_verified: bool
    provider_calls_avoided: int
    prompted_targets: frozenset[str]
    statuses_after: dict[str, FindingMemoryStatus]
    new_titles: frozenset[str]
    resolved_titles: frozenset[str]


async def drive_scenario(
    session_factory: async_sessionmaker[AsyncSession], *, tmp_path: Path, full_name: str, steps: Sequence[ScenarioStep]
) -> list[StepObservation]:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("scenario scratch repo\n")
    _init_git_repo(root)
    base_sha = _commit_all(root, "base")

    async with session_factory() as session:
        repo_row = await RepositoryRepository().upsert(
            session, github_repository_id=abs(hash(full_name)) % (2**62),
            owner=full_name.split("/")[0], name=full_name.split("/")[-1],
            full_name=full_name, installation_id=0,
        )
        await session.commit()
        repository_id = repo_row.id

        pr_row = await PullRequestRepository().upsert(
            session, repository_id=repository_id, github_pr_number=1, title="scenario",
            author="eval", base_sha=base_sha, head_sha=base_sha, state="open",
        )
        await session.commit()
        pull_request_id = pr_row.id

    indexing = RepositoryIndexingService(session_factory=session_factory)
    memory = IncrementalReviewMemoryService(session_factory=session_factory)
    incremental_config = IncrementalConfig()

    async with session_factory() as session:
        prior_open = {f.id: f for f in await ReviewMemoryFindingRepository().get_open_for_pr(session, pull_request_id=pull_request_id)}

    observations: list[StepObservation] = []
    for step in steps:
        if step.custom_commit is not None:
            commit_sha = step.custom_commit(root)
        else:
            step.mutate(root)
            commit_sha = _commit_all(root, step.label)

        if step.rebase_base_to is not None:
            new_base = step.rebase_base_to(root)
            async with session_factory() as session:
                await PullRequestRepository().upsert(
                    session, repository_id=repository_id, github_pr_number=1, title="scenario",
                    author="eval", base_sha=new_base, head_sha=commit_sha, state="open",
                )
                await session.commit()

        await indexing.index_local_repository(repository_id=repository_id, root_path=root, repository_full_name=full_name)
        diff_files = diff_against_base(root, base_sha)

        reviewer, critic = _make_provider_pair(step.findings_by_symbol)
        async with session_factory() as session:
            index = await RepositoryIndexRepository().get_active(session, repository_id=repository_id)
        assert index is not None
        full_candidates = await memory.build_candidates(
            repository_id=repository_id, repository_index_id=index.id, commit_sha=commit_sha,
            diff_files=diff_files, max_candidates=40,
        )
        prepared = await memory.prepare(
            pull_request_id=pull_request_id, repository_index_id=index.id, commit_sha=commit_sha,
            clone_url=str(root), token=None, current_candidates=full_candidates,
            reviewer_provider="fake", reviewer_model="scenario-oracle", incremental_config=incremental_config,
        )
        service = PullRequestReviewService(session_factory=session_factory, reviewer_provider=reviewer, critic_provider=critic)
        summary = await service.review_local(
            repository_id=repository_id, root_path=root, repository_full_name=full_name, commit_sha=commit_sha,
            diff_files=diff_files, pull_request_id=pull_request_id,
            candidate_filter=prepared.candidate_filter,
            incremental_context_fingerprint=prepared.incremental_context_fingerprint,
            config=ReviewConfig(max_concurrent_requests=1),
        )
        reconciliation = await memory.finalize(
            review_run_id=summary.run_id, repository_id=repository_id, pull_request_id=pull_request_id,
            commit_sha=commit_sha, prepared=prepared,
        )

        async with session_factory() as session:
            open_after = await ReviewMemoryFindingRepository().get_open_for_pr(session, pull_request_id=pull_request_id)

        new_titles = frozenset(f.title for f in open_after if f.source_finding_id in reconciliation.new_finding_ids)
        resolved_titles = frozenset(
            prior_open[fid].title for fid in reconciliation.resolved_memory_finding_ids if fid in prior_open
        )
        prior_open = {f.id: f for f in open_after}

        observations.append(
            StepObservation(
                label=step.label,
                mode=prepared.plan.mode,
                ancestry_verified=prepared.plan.selection.ancestry_verified,
                provider_calls_avoided=prepared.plan.metrics.provider_calls_avoided,
                prompted_targets=frozenset(_review_target(c.user_prompt) or "" for c in reviewer.calls),
                statuses_after={f.title: f.status for f in open_after},
                new_titles=new_titles,
                resolved_titles=resolved_titles,
            )
        )

    return observations


def _bug_a(file_path: str = "math_ops.py") -> dict[str, Any]:
    return oracle_finding(title="divide subtracts instead of dividing", file_path=file_path, start_line=2, end_line=2, quoted_text="return a - b")


async def scenario_unrelated_change(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> IncrementalScenarioResult:
    scenario_id = "unrelated_change"

    def step1(root: Path) -> None:
        (root / "math_ops.py").write_text("def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n    return a - b\n")

    def step2(root: Path) -> None:
        (root / "greetings.py").write_text("def greet(name):\n    return f'hi {name}'\n")

    steps = [
        ScenarioStep(label="introduce divide bug", mutate=step1, findings_by_symbol={"divide": _bug_a()}),
        ScenarioStep(label="add unrelated file", mutate=step2, findings_by_symbol={}),
    ]
    obs = await drive_scenario(session_factory, tmp_path=tmp_path, full_name=f"eval/incr-{scenario_id}-{uuid.uuid4().hex[:8]}", steps=steps)
    s1, s2 = obs
    bug_title = "divide subtracts instead of dividing"
    unsafe = s2.statuses_after.get(bug_title) not in (FindingMemoryStatus.CARRIED_FORWARD, FindingMemoryStatus.OPEN)
    passed = (
        s1.new_titles == frozenset({bug_title})
        and s2.mode is IncrementalRunMode.INCREMENTAL
        and s2.ancestry_verified
        and "add" not in s2.prompted_targets
        and "divide" not in s2.prompted_targets
        and not unsafe
    )
    return IncrementalScenarioResult(
        scenario_id=scenario_id, description="an unrelated file is added; the untouched bug and clean symbol must never be re-reviewed",
        passed=passed, provider_calls_full=len(s1.prompted_targets), provider_calls_incremental=len(s2.prompted_targets),
        provider_calls_avoided=s2.provider_calls_avoided, unsafe_carry_forward=unsafe,
        detail=f"step2 status={s2.statuses_after.get(bug_title)!r} prompted={sorted(s2.prompted_targets)} avoided={s2.provider_calls_avoided}",
    )


async def scenario_bug_remains_unchanged(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> IncrementalScenarioResult:
    scenario_id = "bug_remains_unchanged"

    def step1(root: Path) -> None:
        (root / "math_ops.py").write_text("def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n    return a - b\n")

    def step2(root: Path) -> None:
        # add's body changes (still correct); divide is untouched.
        (root / "math_ops.py").write_text("def add(a, b):\n    total = a + b\n    return total\n\n\ndef divide(a, b):\n    return a - b\n")

    steps = [
        ScenarioStep(label="introduce divide bug", mutate=step1, findings_by_symbol={"divide": _bug_a()}),
        ScenarioStep(label="modify add, leave divide untouched", mutate=step2, findings_by_symbol={}),
    ]
    obs = await drive_scenario(session_factory, tmp_path=tmp_path, full_name=f"eval/incr-{scenario_id}-{uuid.uuid4().hex[:8]}", steps=steps)
    _, s2 = obs
    bug_title = "divide subtracts instead of dividing"
    unsafe = s2.statuses_after.get(bug_title) not in (FindingMemoryStatus.CARRIED_FORWARD, FindingMemoryStatus.OPEN)
    passed = "divide" not in s2.prompted_targets and "add" in s2.prompted_targets and not unsafe
    return IncrementalScenarioResult(
        scenario_id=scenario_id, description="a sibling symbol in the same file changes; the untouched bug must still be carried forward",
        passed=passed, provider_calls_full=0, provider_calls_incremental=len(s2.prompted_targets),
        provider_calls_avoided=s2.provider_calls_avoided, unsafe_carry_forward=unsafe,
        detail=f"step2 status={s2.statuses_after.get(bug_title)!r} prompted={sorted(s2.prompted_targets)}",
    )


async def scenario_bug_fixed(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> IncrementalScenarioResult:
    scenario_id = "bug_fixed"

    def step1(root: Path) -> None:
        (root / "math_ops.py").write_text("def divide(a, b):\n    return a - b\n")

    def step2(root: Path) -> None:
        (root / "math_ops.py").write_text("def divide(a, b):\n    return a / b\n")

    steps = [
        ScenarioStep(label="introduce divide bug", mutate=step1, findings_by_symbol={"divide": _bug_a()}),
        ScenarioStep(label="fix divide", mutate=step2, findings_by_symbol={}),
    ]
    obs = await drive_scenario(session_factory, tmp_path=tmp_path, full_name=f"eval/incr-{scenario_id}-{uuid.uuid4().hex[:8]}", steps=steps)
    _, s2 = obs
    bug_title = "divide subtracts instead of dividing"
    resolved = bug_title in s2.resolved_titles and bug_title not in s2.statuses_after
    unsafe = not resolved  # the bug is genuinely fixed -- anything but RESOLVED is unsafe
    passed = "divide" in s2.prompted_targets and resolved
    return IncrementalScenarioResult(
        scenario_id=scenario_id, description="the body of the buggy symbol actually changes (fixed) -- must always get a real recheck, never a blind carry-forward",
        passed=passed, provider_calls_full=0, provider_calls_incremental=len(s2.prompted_targets),
        provider_calls_avoided=s2.provider_calls_avoided, unsafe_carry_forward=unsafe,
        detail=f"resolved={resolved} prompted={sorted(s2.prompted_targets)} statuses_after={s2.statuses_after}",
    )


async def scenario_evidence_region_changed(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> IncrementalScenarioResult:
    scenario_id = "evidence_region_changed"
    bug_title = "compute returns wrong boundary"

    def step1(root: Path) -> None:
        (root / "calc.py").write_text("def compute(a, b):\n    return a >= b\n")

    def step2(root: Path) -> None:
        # A docstring is inserted above the buggy line -- the symbol's
        # own body text changes (MODIFIED continuity) even though the
        # bug's line content is unchanged.
        (root / "calc.py").write_text('def compute(a, b):\n    """Return whether a can cover b."""\n    return a >= b\n')

    finding1 = oracle_finding(title=bug_title, file_path="calc.py", start_line=2, end_line=2, quoted_text="return a >= b")
    finding2 = oracle_finding(title=bug_title, file_path="calc.py", start_line=3, end_line=3, quoted_text="return a >= b")
    steps = [
        ScenarioStep(label="introduce compute bug", mutate=step1, findings_by_symbol={"compute": finding1}),
        ScenarioStep(label="insert docstring above the bug line", mutate=step2, findings_by_symbol={"compute": finding2}),
    ]
    obs = await drive_scenario(session_factory, tmp_path=tmp_path, full_name=f"eval/incr-{scenario_id}-{uuid.uuid4().hex[:8]}", steps=steps)
    _, s2 = obs
    open_titles_after = {t for t, status in s2.statuses_after.items() if status is not FindingMemoryStatus.RESOLVED}
    still_open = bug_title in open_titles_after
    no_duplicate = len(s2.statuses_after) == 1  # exactly one memory finding for this title, never two
    unsafe = not still_open or not no_duplicate
    passed = "compute" in s2.prompted_targets and still_open and no_duplicate
    return IncrementalScenarioResult(
        scenario_id=scenario_id, description="a change above the bug shifts its line number without changing the bug itself -- must be rechecked fresh, not lost or duplicated",
        passed=passed, provider_calls_full=0, provider_calls_incremental=len(s2.prompted_targets),
        provider_calls_avoided=s2.provider_calls_avoided, unsafe_carry_forward=unsafe,
        detail=f"statuses_after={s2.statuses_after} prompted={sorted(s2.prompted_targets)}",
    )


async def scenario_symbol_moved(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> IncrementalScenarioResult:
    scenario_id = "symbol_moved"
    bug_title = "calc_total subtracts instead of adding"
    body = "def calc_total(a, b):\n    return a - b\n"

    def step1(root: Path) -> None:
        (root / "module_a.py").write_text(body)

    def step2(root: Path) -> None:
        (root / "module_a.py").unlink()
        (root / "module_b.py").write_text(body)

    finding = oracle_finding(title=bug_title, file_path="module_a.py", start_line=2, end_line=2, quoted_text="return a - b")
    finding_moved = oracle_finding(title=bug_title, file_path="module_b.py", start_line=2, end_line=2, quoted_text="return a - b")
    steps = [
        ScenarioStep(label="introduce calc_total bug", mutate=step1, findings_by_symbol={"calc_total": finding}),
        ScenarioStep(label="move calc_total to module_b.py", mutate=step2, findings_by_symbol={"calc_total": finding_moved}),
    ]
    obs = await drive_scenario(session_factory, tmp_path=tmp_path, full_name=f"eval/incr-{scenario_id}-{uuid.uuid4().hex[:8]}", steps=steps)
    _, s2 = obs
    still_open = s2.statuses_after.get(bug_title) is not None and s2.statuses_after.get(bug_title) is not FindingMemoryStatus.RESOLVED
    unsafe = not still_open
    return IncrementalScenarioResult(
        scenario_id=scenario_id, description="the buggy symbol moves to a different file, body unchanged -- the finding must survive the move (carried forward or rechecked), never dropped",
        passed=still_open, provider_calls_full=0, provider_calls_incremental=len(s2.prompted_targets),
        provider_calls_avoided=s2.provider_calls_avoided, unsafe_carry_forward=unsafe,
        detail=f"statuses_after={s2.statuses_after} prompted={sorted(s2.prompted_targets)}",
    )


async def scenario_file_renamed(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> IncrementalScenarioResult:
    scenario_id = "file_renamed"
    bug_title = "legacy_check inverted"
    body = "def legacy_check(a, b):\n    return a >= b\n"

    def step1(root: Path) -> None:
        (root / "legacy.py").write_text(body)

    def step2(root: Path) -> None:
        run_git(["-C", str(root), "mv", "legacy.py", "modern.py"])

    finding = oracle_finding(title=bug_title, file_path="legacy.py", start_line=2, end_line=2, quoted_text="return a >= b")
    finding_renamed = oracle_finding(title=bug_title, file_path="modern.py", start_line=2, end_line=2, quoted_text="return a >= b")
    steps = [
        ScenarioStep(label="introduce legacy_check bug", mutate=step1, findings_by_symbol={"legacy_check": finding}),
        ScenarioStep(label="git mv legacy.py -> modern.py", mutate=step2, findings_by_symbol={"legacy_check": finding_renamed}),
    ]
    obs = await drive_scenario(session_factory, tmp_path=tmp_path, full_name=f"eval/incr-{scenario_id}-{uuid.uuid4().hex[:8]}", steps=steps)
    _, s2 = obs
    still_open = s2.statuses_after.get(bug_title) is not None and s2.statuses_after.get(bug_title) is not FindingMemoryStatus.RESOLVED
    unsafe = not still_open
    return IncrementalScenarioResult(
        scenario_id=scenario_id, description="the file containing the bug is renamed (git mv), content identical -- the finding must survive",
        passed=still_open, provider_calls_full=0, provider_calls_incremental=len(s2.prompted_targets),
        provider_calls_avoided=s2.provider_calls_avoided, unsafe_carry_forward=unsafe,
        detail=f"statuses_after={s2.statuses_after} prompted={sorted(s2.prompted_targets)}",
    )


async def scenario_function_renamed_ambiguously(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> IncrementalScenarioResult:
    scenario_id = "function_renamed_ambiguously"
    bug_title = "handler off by one"

    def step1(root: Path) -> None:
        (root / "handlers.py").write_text("def handler(items):\n    return items[len(items)]\n")

    def step2(root: Path) -> None:
        # `handler` is renamed to `process`, AND a second, differently-
        # bodied `process` is introduced elsewhere -- genuine ambiguity
        # for symbol continuity: which one (if either) is "the same" as
        # the old `handler`?
        (root / "handlers.py").write_text("def process(items):\n    return items[len(items)]\n")
        (root / "other.py").write_text("def process(items):\n    return items[0] if items else None\n")

    finding = oracle_finding(title=bug_title, file_path="handlers.py", start_line=2, end_line=2, quoted_text="return items[len(items)]")
    steps = [
        ScenarioStep(label="introduce handler bug", mutate=step1, findings_by_symbol={"handler": finding}),
        ScenarioStep(
            label="rename handler->process, introduce an unrelated second process",
            mutate=step2,
            findings_by_symbol={"process": finding},  # only handlers.py's process still has the bug
        ),
    ]
    obs = await drive_scenario(session_factory, tmp_path=tmp_path, full_name=f"eval/incr-{scenario_id}-{uuid.uuid4().hex[:8]}", steps=steps)
    _, s2 = obs
    # Safety bar: the system must never silently RESOLVE (drop) a real
    # bug just because its symbol became ambiguous, and must never
    # silently CARRY_FORWARD without ever having looked at the new code
    # at all (zero prompted targets) -- either outcome is unsafe here.
    resolved_without_recheck = bug_title in s2.resolved_titles and "process" not in s2.prompted_targets
    carried_without_recheck = (
        s2.statuses_after.get(bug_title) is FindingMemoryStatus.CARRIED_FORWARD and "process" not in s2.prompted_targets
    )
    unsafe = resolved_without_recheck or carried_without_recheck
    passed = not unsafe
    return IncrementalScenarioResult(
        scenario_id=scenario_id, description="a rename introduces a second equally-plausible symbol of the same name -- must never silently resolve or carry forward without a fresh look",
        passed=passed, provider_calls_full=0, provider_calls_incremental=len(s2.prompted_targets),
        provider_calls_avoided=s2.provider_calls_avoided, unsafe_carry_forward=unsafe,
        detail=f"statuses_after={s2.statuses_after} prompted={sorted(s2.prompted_targets)} resolved_titles={s2.resolved_titles}",
    )


async def scenario_force_push(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> IncrementalScenarioResult:
    scenario_id = "force_push"

    def step1(root: Path) -> None:
        (root / "math_ops.py").write_text("def divide(a, b):\n    return a - b\n")

    def force_push(root: Path) -> str:
        # Rewrite history: reset to the base commit, then commit
        # different content -- the old HEAD (that the memory generation
        # is tied to) becomes unreachable from the new HEAD.
        base_sha = run_git(["-C", str(root), "rev-list", "--max-parents=0", "HEAD"]).strip().splitlines()[0]
        run_git(["-C", str(root), "reset", "--hard", base_sha])
        (root / "math_ops.py").write_text("def divide(a, b):\n    return a - b\n\n\ndef add(a, b):\n    return a + b\n")
        return _commit_all(root, "force-pushed rewrite")

    steps = [
        ScenarioStep(label="introduce divide bug", mutate=step1, findings_by_symbol={"divide": _bug_a()}),
        ScenarioStep(label="force-push rewritten history", custom_commit=force_push, findings_by_symbol={"divide": _bug_a(), "add": {}}),
    ]
    obs = await drive_scenario(session_factory, tmp_path=tmp_path, full_name=f"eval/incr-{scenario_id}-{uuid.uuid4().hex[:8]}", steps=steps)
    _, s2 = obs
    # After a force-push, incremental reuse must never be trusted: the
    # plan must fall back to FULL and never claim ancestry was verified.
    unsafe = s2.mode is not IncrementalRunMode.FULL or s2.ancestry_verified
    passed = not unsafe
    return IncrementalScenarioResult(
        scenario_id=scenario_id, description="history is force-pushed/rewritten -- incremental reuse must never be trusted across a non-ancestor rewrite",
        passed=passed, provider_calls_full=len(s2.prompted_targets), provider_calls_incremental=0,
        provider_calls_avoided=s2.provider_calls_avoided, unsafe_carry_forward=unsafe,
        detail=f"mode={s2.mode!r} ancestry_verified={s2.ancestry_verified} avoided={s2.provider_calls_avoided}",
    )


async def scenario_base_change(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> IncrementalScenarioResult:
    scenario_id = "base_change"

    def step1(root: Path) -> None:
        (root / "math_ops.py").write_text("def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n    return a - b\n")

    def step2(root: Path) -> None:
        (root / "greetings.py").write_text("def greet(name):\n    return f'hi {name}'\n")

    steps = [
        ScenarioStep(label="introduce divide bug", mutate=step1, findings_by_symbol={"divide": _bug_a()}),
        # The PR's declared base_sha moves forward (e.g. the target
        # branch advanced) while HEAD's own commit history stays a
        # normal linear descendant of the previous review's commit --
        # incremental safety is about the review-generation commit
        # chain, not the mutable base pointer, so this must NOT spuriously
        # break carry-forward.
        ScenarioStep(label="base moves forward; unrelated file added", mutate=step2, findings_by_symbol={}, rebase_base_to=lambda root: _rev_parse(root, "HEAD")),
    ]
    obs = await drive_scenario(session_factory, tmp_path=tmp_path, full_name=f"eval/incr-{scenario_id}-{uuid.uuid4().hex[:8]}", steps=steps)
    _, s2 = obs
    bug_title = "divide subtracts instead of dividing"
    still_safe = s2.statuses_after.get(bug_title) in (FindingMemoryStatus.CARRIED_FORWARD, FindingMemoryStatus.OPEN)
    unsafe = not still_safe
    passed = s2.mode is IncrementalRunMode.INCREMENTAL and s2.ancestry_verified and not unsafe
    return IncrementalScenarioResult(
        scenario_id=scenario_id, description="the PR's declared base moves forward independently of HEAD's own commit chain -- must not spuriously break incremental reuse",
        passed=passed, provider_calls_full=0, provider_calls_incremental=len(s2.prompted_targets),
        provider_calls_avoided=s2.provider_calls_avoided, unsafe_carry_forward=unsafe,
        detail=f"mode={s2.mode!r} ancestry_verified={s2.ancestry_verified} statuses_after={s2.statuses_after}",
    )


ALL_SCENARIOS: tuple[Callable[[async_sessionmaker[AsyncSession], Path], Any], ...] = (
    scenario_unrelated_change,
    scenario_bug_remains_unchanged,
    scenario_bug_fixed,
    scenario_evidence_region_changed,
    scenario_symbol_moved,
    scenario_file_renamed,
    scenario_function_renamed_ambiguously,
    scenario_force_push,
    scenario_base_change,
)


async def run_all_incremental_scenarios(
    session_factory: async_sessionmaker[AsyncSession], *, tmp_path_root: Path
) -> list[IncrementalScenarioResult]:
    results: list[IncrementalScenarioResult] = []
    for i, scenario in enumerate(ALL_SCENARIOS):
        scenario_tmp = tmp_path_root / f"scenario-{i}-{uuid.uuid4().hex[:8]}"
        scenario_tmp.mkdir(parents=True, exist_ok=True)
        results.append(await scenario(session_factory, scenario_tmp))
    return results
