"""Shared builders for the M10/M11 campaign tests and demo: the fictional Acme
workspace (``tests/fixtures/campaigns/acme``) and the internal-contract
workspace (``.../internal``). Everything is built by the real engine from
fixture files -- no hand-built domain objects stand in for analysis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from patchfrog.campaigns.domain import EnrolledRepository, RepositoryAccess
from patchfrog.campaigns.evaluate import RepositoryInput
from patchfrog.upstream.domain import ExternalChangeEvent
from patchfrog.upstream.events import build_contract_change, load_contract_file
from patchfrog.upstream.hints import ChangeHints, load_hints

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "campaigns"
ACME = FIXTURES / "acme"
INTERNAL = FIXTURES / "internal"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
FRESH_AT = NOW - timedelta(hours=2)
STALE_AT = NOW - timedelta(days=30)
SHA = "a" * 40


def acme_event() -> tuple[ExternalChangeEvent, ChangeHints]:
    hints = load_hints((ACME / "contract" / "hints.yaml").read_text())
    event = build_contract_change(
        load_contract_file(ACME / "contract" / "old.yaml"), load_contract_file(ACME / "contract" / "new.yaml"),
        hints=hints,
    )
    return event, hints


def acme_input(
    name: str, *, discovered_at: datetime | None = FRESH_AT, root: Path | str | None = "repo",
    access: RepositoryAccess = RepositoryAccess.AVAILABLE, sha: str = SHA, repos_root: Path | None = None,
) -> RepositoryInput:
    base = repos_root or (ACME / "repos")
    resolved = (base / name) if root == "repo" else root
    assert resolved is None or isinstance(resolved, Path)
    return RepositoryInput(
        repository=EnrolledRepository(
            full_name=f"acme/{name}", access=access, last_discovery_at=discovered_at, last_discovery_commit_sha=sha,
        ),
        root=resolved, commit_sha=sha, extra_workspace_paths=(ACME / "sdk_stub",),
    )


def acme_entries() -> list[RepositoryInput]:
    """The spec's four-repository scenario."""

    return [
        acme_input("acme-web"),
        acme_input("acme-worker"),
        acme_input("acme-search"),
        acme_input("acme-legacy", discovered_at=STALE_AT, root=None),
    ]
