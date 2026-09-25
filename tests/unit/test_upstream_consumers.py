"""M6.5-M6.7 unit rules: call location, consumer matching, dependency
identity matching, blast-radius bounds and registry-only impact."""

from __future__ import annotations

from typing import Any

from patchfrog.dependencies.domain import (
    DependencyUsageSite,
    DetectionConfidence,
    Ecosystem,
    EvidenceType,
)
from patchfrog.intelligence.graph import EdgeKind, GraphNode, NodeKind, RepositoryEdge
from patchfrog.upstream.blast_radius import compute_blast_radius
from patchfrog.upstream.calls import locate_js_calls, locate_python_calls
from patchfrog.upstream.code_graph import LocalCodeGraph
from patchfrog.upstream.consumers import (
    ConsumerMatchType,
    DependencyIdentity,
    ImpactKind,
    map_consumers,
    match_dependency,
    path_matches,
)
from patchfrog.upstream.domain import DependencyTarget
from patchfrog.upstream.events import build_contract_change, parse_contract_document
from patchfrog.upstream.hints import parse_hints
from patchfrog.upstream.workspace import RegistryDependency, registry_impact


def _site(path: str, line: int, token: str, evidence: EvidenceType = EvidenceType.SDK_CALL,
          symbol: str | None = "fn") -> DependencyUsageSite:
    return DependencyUsageSite(path, line, symbol, evidence, token, DetectionConfidence.HIGH)


def _surface(symbols: dict[str, Any], version: str = "1.0.0") -> dict[str, Any]:
    return {"patchfrog_sdk_surface": 1, "package": "sdk", "ecosystem": "pypi", "version": version,
            "modules": ["sdk"], "symbols": symbols}


def _event(old: dict[str, Any], new: dict[str, Any], hints: dict[str, Any] | None = None) -> Any:
    return build_contract_change(parse_contract_document(old, ref="old"), parse_contract_document(new, ref="new"),
                                 hints=parse_hints(hints))


# -- call location -------------------------------------------------------------


def test_python_call_location_spans_and_arguments() -> None:
    source = 'import sdk\nc = sdk.Client()\n\ndef f(x, **kw):\n    return c.items.list(limit=10, cursor=x, **kw)\n'
    (call,) = locate_python_calls(source, 5, "items.list")
    assert source[call.chain_span.start:call.chain_span.end] == ".items.list"
    assert call.has_receiver and call.indirect_keywords
    limit = call.keyword("limit")
    assert limit is not None and limit.is_literal and limit.literal == 10
    assert source[limit.name_span.start:limit.name_span.end] == "limit"
    assert source[call.close_paren] == ")"
    assert locate_python_calls(source, 5, "items.create") == []
    assert locate_python_calls("def broken(:\n", 1, "x") == []


def test_python_offsets_survive_non_ascii_text() -> None:
    source = 'import sdk\nc = sdk.Client()\nc.say.it(text="héllo wörld", mode="x")\n'
    (call,) = locate_python_calls(source, 3, "say.it")
    mode = call.keyword("mode")
    assert mode is not None and source[mode.name_span.start:mode.name_span.end] == "mode"


def test_js_location_ignores_comments_and_strings() -> None:
    source = (
        'const c = new Sdk();\n'
        '// c.items.list({ limit: 1 })\n'
        'const s = "c.items.list({ limit: 1 })";\n'
        'c.items.list({ limit: 1, "cursor": next, extra, ...rest });\n'
    )
    assert locate_js_calls(source, 2, "items.list") == []
    assert locate_js_calls(source, 3, "items.list") == []
    (call,) = locate_js_calls(source, 4, "items.list")
    names = {k.name: k for k in call.keywords}
    assert set(names) == {"limit", "cursor", "extra"}
    assert names["extra"].shorthand and call.indirect_keywords
    cursor = names["cursor"]
    assert source[cursor.name_span.start:cursor.name_span.end] == "cursor"  # quotes preserved around it


# -- consumer matching -----------------------------------------------------------


def test_argument_level_items_rule_out_calls_that_do_not_use_the_argument(tmp_path: Any) -> None:
    from patchfrog.upstream.consumers import source_call_locator

    (tmp_path / "a.py").write_text(
        "import sdk\nc = sdk.Client()\n"
        "def uses():\n    return c.items.list(cursor='x')\n"
        "def skips():\n    return c.items.list(limit=1)\n"
        "def hidden(**kw):\n    return c.items.list(**kw)\n"
    )
    event = _event(_surface({"items.list": {"params": {"cursor": {}, "limit": {}}}}),
                   _surface({"items.list": {"params": {"limit": {}}}}))
    sites = [_site("a.py", 4, "items.list", symbol="uses"), _site("a.py", 6, "items.list", symbol="skips"),
             _site("a.py", 8, "items.list", symbol="hidden")]
    impact = map_consumers(event, repository="r", dependency_key="sdk:pypi", match_reason="package",
                           usage_sites=sites, locate=source_call_locator(tmp_path))
    assert [c.location for c in impact.direct] == ["a.py::uses"]
    assert [c.location for c in impact.potential] == ["a.py::hidden"]  # **kw: possibility, not proof
    assert [s.location for s in impact.ruled_out_sites] == ["a.py::skips"]


def test_without_source_argument_level_matches_are_potential_medium() -> None:
    event = _event(_surface({"items.list": {"params": {"cursor": {}}}}), _surface({"items.list": {"params": {}}}))
    impact = map_consumers(event, repository="r", dependency_key="k", match_reason="package",
                           usage_sites=[_site("a.py", 4, "items.list")])
    (consumer,) = impact.affected
    assert consumer.impact is ImpactKind.POTENTIAL and consumer.confidence is DetectionConfidence.MEDIUM


def test_symbol_level_items_match_exact_tokens_and_namespaces_only() -> None:
    event = _event(_surface({"items.list": {}, "items.get": {}, "users.list": {}}),
                   _surface({"items.get": {}, "users.list": {}}))
    sites = [_site("a.py", 1, "items.list"), _site("a.py", 2, "items.get"), _site("a.py", 3, "users.list"),
             _site("a.py", 4, "sdk", EvidenceType.IMPORT, None)]
    impact = map_consumers(event, repository="r", dependency_key="k", match_reason="package", usage_sites=sites)
    assert [c.site.token for c in impact.affected] == ["items.list"]
    assert len(impact.unaffected_sites) == 3


def test_package_level_evidence_is_potential_and_superseded_by_structure() -> None:
    from patchfrog.upstream.events import build_version_change

    sites = [_site("a.py", 1, "items.list"), _site("a.py", 2, "SDK_TOKEN", EvidenceType.ENV_VAR_NAME, None)]
    version_only = build_version_change(DependencyTarget(package_name="sdk"), "1.0.0", "2.0.0")
    impact = map_consumers(version_only, repository="r", dependency_key="k", match_reason="package", usage_sites=sites)
    (consumer,) = impact.affected  # env-var names are never package-level consumers
    assert consumer.impact is ImpactKind.POTENTIAL and consumer.match_types == (ConsumerMatchType.PACKAGE_LEVEL,)
    structural = _event(_surface({"items.list": {}}, "1.0.0"), _surface({"items.list": {}}, "2.0.0"))
    assert map_consumers(structural, repository="r", dependency_key="k", match_reason="package",
                         usage_sites=sites).affected == ()


def test_operation_changes_reach_sdk_calls_only_through_an_explicit_bridge() -> None:
    spec: dict[str, Any] = {"openapi": "3.0.0", "info": {"title": "t", "version": "1"}, "paths": {
        "/v1/sessions": {"post": {"parameters": [{"name": "expand", "in": "query", "schema": {"type": "string"}}],
                                  "responses": {"200": {"description": "ok"}}}}}}
    new: dict[str, Any] = {**spec, "paths": {"/v1/sessions": {"post": {"responses": {"200": {"description": "ok"}}}}}}
    sites = [_site("a.py", 3, "sessions.create")]
    plain = _event(spec, new)
    impact = map_consumers(plain, repository="r", dependency_key="k", match_reason="package", usage_sites=sites)
    assert impact.affected == () and impact.unmapped_item_keys
    assert impact.notes  # says why it could not be mapped
    bridged_hints = {"patchfrog_change_hints": 1, "operation_symbols": [
        {"operation": "POST /v1/sessions", "symbol": "sessions.create"}]}
    bridged = _event(spec, new, bridged_hints)
    impact = map_consumers(bridged, repository="r", dependency_key="k", match_reason="package", usage_sites=sites,
                           hints=parse_hints(bridged_hints))
    (consumer,) = impact.affected
    assert consumer.match_types == (ConsumerMatchType.SDK_VIA_OPERATION,)


def test_path_matching() -> None:
    assert path_matches("/repos/{}/{}/issues", "/repos/{owner}/{repo}/issues")
    assert path_matches("/v1/repos/{}/{}/issues", "/repos/{owner}/{repo}/issues")  # base path prefix
    assert path_matches("/repos/acme/app/issues?state=open", "/repos/{owner}/{repo}/issues")
    assert not path_matches("/repos/{}/{}/issues/{}", "/repos/{owner}/{repo}/issues")
    assert not path_matches("/v1/products", "/v1/products/{id}")
    assert not path_matches("/api/users", "/users")  # single-segment spec paths never get a prefix


def test_http_method_ambiguity_lowers_confidence() -> None:
    op = {"responses": {"200": {"description": "ok"}}}
    old: dict[str, Any] = {"openapi": "3.0.0", "info": {"title": "t", "version": "1"},
                           "paths": {"/v1/items": {"get": op, "post": op}}}
    new: dict[str, Any] = {**old, "paths": {"/v1/items": {"get": op}}}
    impact = map_consumers(_event(old, new), repository="r", dependency_key="k", match_reason="contract_fingerprint",
                           usage_sites=[_site("a.py", 1, "/v1/items", EvidenceType.OPENAPI_PATH_REFERENCE)])
    (consumer,) = impact.affected
    assert consumer.confidence is DetectionConfidence.MEDIUM
    assert "not determinable" in consumer.reasons[0]


# -- dependency identity matching ---------------------------------------------------


def _identity(**overrides: Any) -> DependencyIdentity:
    base: dict[str, Any] = {"key": "stripe:pypi", "provider_key": "stripe", "kind": "sdk", "ecosystem": "pypi",
                            "package_name": "stripe", "contract_fingerprint": None, "hosts": ("api.stripe.com",)}
    return DependencyIdentity(**{**base, **overrides})


def test_dependency_matching_hierarchy() -> None:
    assert match_dependency(DependencyTarget(dependency_key="stripe:pypi"), _identity()) == "dependency_key"
    assert match_dependency(DependencyTarget(dependency_key="stripe:npm", package_name="stripe"), _identity()) is None
    assert match_dependency(DependencyTarget(package_name="Stripe", ecosystem=Ecosystem.PYPI), _identity()) == "package"
    assert match_dependency(DependencyTarget(package_name="stripe", ecosystem=Ecosystem.NPM), _identity()) is None
    assert match_dependency(DependencyTarget(api_hosts=("api.stripe.com",)), _identity()) == "api_host"
    assert match_dependency(DependencyTarget(provider_key="stripe"), _identity()) == "provider"
    assert match_dependency(DependencyTarget(provider_key="stripe-like"), _identity()) is None
    openapi = _identity(key="openapi:spec.yaml", provider_key="openapi", kind="openapi_contract", ecosystem="openapi",
                        package_name=None, contract_fingerprint="abc", hosts=())
    assert match_dependency(DependencyTarget(provider_key="openapi"), openapi, old_contract_fingerprint="abc") == (
        "contract_fingerprint")
    # "openapi" is a format, never a provider identity.
    assert match_dependency(DependencyTarget(provider_key="openapi"), openapi) is None


# -- blast radius ------------------------------------------------------------------


def _calls(*pairs: tuple[str, str]) -> LocalCodeGraph:
    graph = LocalCodeGraph(graph_files=frozenset({"a.py", "b.py", "c.py"}), languages=frozenset({"python"}))
    edges = []
    for caller, callee in pairs:
        cf, cs = caller.split("::")
        tf, ts = callee.split("::")
        graph.callers[(tf, ts)].add((cf, cs))
        graph.callees[(cf, cs)].add((tf, ts))
        edges.append(RepositoryEdge(EdgeKind.SYMBOL_CALLS_SYMBOL, GraphNode(NodeKind.SYMBOL, cf, cs),
                                    GraphNode(NodeKind.SYMBOL, tf, ts)))
    graph.edges = tuple(edges)
    return graph


def test_blast_radius_depth_bound_potential_isolation_and_callees() -> None:
    event = _event(_surface({"items.list": {}}), _surface({}))
    sites = [_site("a.py", 3, "items.list", symbol="direct")]
    impact = map_consumers(event, repository="r", dependency_key="k", match_reason="package", usage_sites=sites)
    graph = _calls(("b.py::one", "a.py::direct"), ("c.py::two", "b.py::one"), ("c.py::three", "c.py::two"),
                   ("a.py::direct", "a.py::helper"))
    radius = compute_blast_radius(impact, graph, max_depth=2)
    assert [n.key for n in radius.transitive] == ["b.py::one", "c.py::two"]  # c.py::three is depth 3
    assert radius.direct_callees == ("a.py::helper",)  # listed, never counted
    assert radius.summary()["transitive"] == 2
    limited = compute_blast_radius(impact, graph, max_depth=2, max_nodes=2)
    assert limited.truncated and len(limited.nodes) == 2


def test_potential_consumers_are_never_expanded() -> None:
    from patchfrog.upstream.events import build_version_change

    event = build_version_change(DependencyTarget(package_name="sdk"), "1.0.0", "2.0.0")
    impact = map_consumers(event, repository="r", dependency_key="k", match_reason="package",
                           usage_sites=[_site("a.py", 3, "items.list", symbol="direct")])
    radius = compute_blast_radius(impact, _calls(("b.py::one", "a.py::direct")))
    assert [n.impact for n in radius.nodes] == [ImpactKind.POTENTIAL]
    assert radius.transitive == ()


# -- registry-only cross-repo impact -----------------------------------------------


def test_registry_impact_reports_every_repository() -> None:
    event = _event(_surface({"items.list": {}}), _surface({}))
    identity = DependencyIdentity("sdk:pypi", "sdk", "sdk", "pypi", "sdk", None, ())
    rows = [
        RegistryDependency("org/a", "aaa", identity, (_site("x.py", 1, "items.list"),)),
        RegistryDependency("org/b", "bbb", identity, (_site("y.py", 1, "items.get"),)),
    ]
    workspace = registry_impact(rows, event, repositories=["org/a", "org/b", "org/c"])
    status = {r.repository: r.status.value for r in workspace.repositories}
    assert status == {"org/a": "affected", "org/b": "unaffected", "org/c": "unaffected"}
    (affected,) = workspace.affected
    assert affected.notes and "registry evidence only" in affected.notes[0]
