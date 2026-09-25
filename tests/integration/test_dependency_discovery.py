"""M5 end to end over committed fixture repositories: provider detection,
versions, usage sites with enclosing symbols, OpenAPI contracts, the
dependency graph, false-positive traps, secret-value safety and the CLI.
Offline: no network, no provider calls."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from patchfrog.dependencies import discover_dependencies
from patchfrog.dependencies.adapters import (
    KnownProviderAdapter,
    ProviderSpec,
    default_provider_adapters,
)
from patchfrog.dependencies.domain import (
    ContractSourceType,
    DependencyInventory,
    DetectionConfidence,
    Ecosystem,
    EvidenceType,
    ExternalDependencyKind,
)
from patchfrog.dependencies.graph import build_dependency_graph
from patchfrog.dependencies.report import inventory_to_dict
from patchfrog.intelligence.graph import EdgeKind, NodeKind
from patchfrog.repository.git import run_git

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dependencies"
MIXED = FIXTURES / "mixed_repo"

#: Deliberately fake, non-provider-shaped values: they must never appear
#: in any discovery output.
_FAKE_ENV_VALUE = "PFTEST-NOT-A-SECRET-6f1d2c"
_FAKE_DEFAULT_VALUE = "PFTEST-DEFAULT-NOT-A-SECRET-91ab"


def _sites(inventory: DependencyInventory, key: str) -> set[tuple[str, EvidenceType, str]]:
    return {(s.location, s.evidence_type, s.token) for s in inventory.by_key()[key].usage_sites}


def test_mixed_repository_inventory() -> None:
    inventory = discover_dependencies(MIXED, repository="acme/mixed")
    deps = inventory.by_key()
    assert set(deps) == {"openai:pypi", "stripe:npm", "github:http", "openapi:openapi.yaml", "package:pypi:requests"}

    openai = deps["openai:pypi"]
    assert (openai.package_name, openai.version.declared, openai.confidence) == ("openai", "==1.40.0", DetectionConfidence.HIGH)
    assert ("app/ai/chat.py::generate_reply", EvidenceType.SDK_CALL, "chat.completions.create") in _sites(inventory, "openai:pypi")
    assert ("workers/summarize.py::summarize", EvidenceType.SDK_CALL, "responses.create") in _sites(inventory, "openai:pypi")
    assert openai.contract is not None and openai.contract.source.type is ContractSourceType.SDK_STATIC
    assert openai.contract.normalized["consumed_surface"] == [
        "OpenAI", "chat.completions.create", "moderations.create", "responses.create",
    ]

    stripe = deps["stripe:npm"]
    assert (stripe.version.declared, stripe.version.resolved) == ("^12.0.0", "12.3.0")
    assert ("billing/checkout.js::createSession", EvidenceType.SDK_CALL, "checkout.sessions.create") in _sites(
        inventory, "stripe:npm"
    )
    assert stripe.contract is not None and stripe.contract.source.ref == "npm:stripe@12.3.0"

    github = deps["github:http"]
    assert github.kind is ExternalDependencyKind.HTTP_API
    assert github.confidence is DetectionConfidence.MEDIUM
    assert (
        "integrations/github.py::create_comment",
        EvidenceType.HTTP_ENDPOINT,
        "api.github.com/repos/{}/{}/issues/{}/comments",
    ) in _sites(inventory, "github:http")

    spec = deps["openapi:openapi.yaml"]
    assert spec.provider.display_name == "Inventory Service"
    assert spec.contract is not None and dict(spec.contract.summary)["operations"] == 2
    assert ("services/inventory.py::reserve", EvidenceType.HTTP_ENDPOINT,
            "inventory.internal.example.com/v1/reservations") in _sites(inventory, "openapi:openapi.yaml")
    assert spec.metadata["relationship"] == "consumed_in_repository"

    env_names = {e.token for d in inventory.dependencies for e in d.evidence if e.evidence_type is EvidenceType.ENV_VAR_NAME}
    assert env_names == {"OPENAI_API_KEY", "STRIPE_SECRET_KEY", "GITHUB_TOKEN"}


def test_false_positive_traps_produce_no_provider_dependency() -> None:
    inventory = discover_dependencies(FIXTURES / "false_positive_repo")
    # Local github.py module, prose/comment/string mentions, docs links, a
    # lookalike host and a lone GITHUB_TOKEN name are all non-evidence.
    assert inventory.dependencies == ()


def test_env_var_name_alone_never_establishes_a_dependency(tmp_path: Path) -> None:
    (tmp_path / "ci.py").write_text('import os\nTOKEN = os.environ["OPENAI_API_KEY"]\n')
    assert discover_dependencies(tmp_path).dependencies == ()


def test_secret_values_are_never_read_or_returned(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(MIXED, repo)
    (repo / ".env").write_text(f"OPENAI_API_KEY={_FAKE_ENV_VALUE}\n")
    (repo / "config" / ".env.production").write_text(f"STRIPE_SECRET_KEY={_FAKE_ENV_VALUE}\n")
    settings = repo / "config" / "settings.py"
    settings.write_text(settings.read_text().replace('os.getenv("STRIPE_SECRET_KEY", "")',
                                                     f'os.getenv("STRIPE_SECRET_KEY", "{_FAKE_DEFAULT_VALUE}")'))
    inventory = discover_dependencies(repo)
    serialized = json.dumps(inventory_to_dict(inventory))
    assert _FAKE_ENV_VALUE not in serialized
    assert _FAKE_DEFAULT_VALUE not in serialized
    assert inventory.secret_store_files_skipped == 2
    assert "STRIPE_SECRET_KEY" in serialized  # the NAME is evidence


def test_git_checkout_only_considers_tracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(MIXED, repo)
    (repo / ".gitignore").write_text("scratch/\n")
    run_git(["-C", str(repo), "init", "--quiet"])
    run_git(["-C", str(repo), "config", "user.email", "t@patchfrog.invalid"])
    run_git(["-C", str(repo), "config", "user.name", "t"])
    run_git(["-C", str(repo), "add", "-A"])
    run_git(["-C", str(repo), "commit", "--quiet", "-m", "init"])
    (repo / "scratch").mkdir()
    (repo / "scratch" / "notes.py").write_text("import stripe\nstripe.Charge.create()\n")
    inventory = discover_dependencies(repo)
    assert not any(s.file_path.startswith("scratch/") for d in inventory.dependencies for s in d.usage_sites)


def test_dependency_graph_links_symbols_to_dependencies_and_contracts() -> None:
    inventory = discover_dependencies(MIXED)
    edges = build_dependency_graph(inventory)
    uses = {
        (e.source.qualified_name, e.target.qualified_name)
        for e in edges
        if e.kind is EdgeKind.USES_EXTERNAL_DEPENDENCY and e.source.kind is NodeKind.SYMBOL
    }
    assert ("generate_reply", "openai:pypi") in uses
    assert ("createSession", "stripe:npm") in uses
    contracts = {e.source.qualified_name for e in edges if e.kind is EdgeKind.DEPENDENCY_HAS_CONTRACT}
    assert {"openai:pypi", "stripe:npm", "github:http", "openapi:openapi.yaml"} <= contracts


def test_discovery_is_deterministic() -> None:
    assert inventory_to_dict(discover_dependencies(MIXED)) == inventory_to_dict(discover_dependencies(MIXED))


def test_a_new_provider_is_one_spec_without_core_changes(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("anthropic==0.40.0\n")
    (tmp_path / "bot.py").write_text(
        "from anthropic import Anthropic\n\nclient = Anthropic()\n\n\n"
        "def ask(q):\n    return client.messages.create(model='m', max_tokens=5, messages=[])\n"
    )
    spec = ProviderSpec(
        key="anthropic",
        display_name="Anthropic",
        packages={Ecosystem.PYPI: ("anthropic",)},
        python_modules=("anthropic",),
        api_hosts=("api.anthropic.com",),
        env_var_pattern=r"^ANTHROPIC_[A-Z0-9_]+$",
    )
    adapters = [*default_provider_adapters(), KnownProviderAdapter(spec)]
    inventory = discover_dependencies(tmp_path, adapters=adapters)
    anthropic = inventory.by_key()["anthropic:pypi"]
    assert anthropic.confidence is DetectionConfidence.HIGH
    assert ("bot.py::ask", EvidenceType.SDK_CALL, "messages.create") in _sites(inventory, "anthropic:pypi")
