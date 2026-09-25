"""Human and machine-readable dependency inventory reports (M5.8).

Both forms contain only the controlled tokens the domain model stores
(names, versions, paths, symbols, hostnames, env-var *names*,
fingerprints) -- never a secret value, never a source line.
"""

from __future__ import annotations

from typing import Any

from patchfrog.dependencies.domain import (
    DependencyInventory,
    EvidenceType,
    ExternalDependency,
    ExternalDependencyKind,
)
from patchfrog.dependencies.graph import build_dependency_graph, inventory_tree


def dependency_to_dict(dependency: ExternalDependency) -> dict[str, Any]:
    contract = dependency.contract
    return {
        "key": dependency.key,
        "provider": dependency.provider.key,
        "display_name": dependency.provider.display_name,
        "kind": dependency.kind.value,
        "ecosystem": dependency.ecosystem.value,
        "package": dependency.package_name,
        "version": {
            "declared": dependency.version.declared,
            "resolved": dependency.version.resolved,
            "declared_in": dependency.version.declared_in,
            "resolved_in": dependency.version.resolved_in,
        },
        "confidence": dependency.confidence.value,
        "usage_sites": [
            {
                "file": s.file_path,
                "line": s.line,
                "symbol": s.symbol,
                "evidence": s.evidence_type.value,
                "token": s.token,
                "confidence": s.confidence.value,
            }
            for s in dependency.usage_sites
        ],
        "evidence_counts": dependency.evidence_counts(),
        "env_var_names": sorted(
            {e.token for e in dependency.evidence if e.evidence_type is EvidenceType.ENV_VAR_NAME}
        ),
        "contract": (
            {
                "source_type": contract.source.type.value,
                "source_ref": contract.source.ref,
                "fingerprint": contract.fingerprint.value,
                "algorithm": contract.fingerprint.algorithm,
                "normalization_version": contract.fingerprint.normalization_version,
                "summary": dict(contract.summary),
            }
            if contract is not None
            else None
        ),
        "metadata": dict(dependency.metadata),
    }


def inventory_to_dict(inventory: DependencyInventory) -> dict[str, Any]:
    return {
        "repository": inventory.repository,
        "commit_sha": inventory.commit_sha,
        "discovery_version": inventory.discovery_version,
        "files_scanned": inventory.files_scanned,
        "secret_store_files_skipped": inventory.secret_store_files_skipped,
        "dependencies": [dependency_to_dict(d) for d in inventory.dependencies],
        "graph": {
            "inventory": inventory_tree(inventory),
            "edges": [
                {
                    "kind": e.kind.value,
                    "source": {"kind": e.source.kind.value, "file": e.source.file_path,
                               "symbol": e.source.qualified_name},
                    "target": {"kind": e.target.kind.value, "id": e.target.qualified_name},
                    "reason": e.reason,
                }
                for e in build_dependency_graph(inventory)
            ],
        },
    }


def render_inventory_text(inventory: DependencyInventory, *, show_all_packages: bool = False) -> str:
    lines = [f"External dependencies detected in {inventory.repository}", ""]
    apis = [d for d in inventory.dependencies if d.kind is not ExternalDependencyKind.PACKAGE]
    packages = [d for d in inventory.dependencies if d.kind is ExternalDependencyKind.PACKAGE]
    if not apis:
        lines.append("  (no external API/SDK dependencies found)")
    for dep in apis:
        if dep.kind is ExternalDependencyKind.OPENAPI_CONTRACT:
            continue
        lines.append(dep.provider.display_name + (f" ({dep.ecosystem.value})" if dep.package_name else ""))
        if dep.package_name:
            lines.append(f"  package: {dep.package_name}")
        if dep.version.display:
            lines.append(f"  version: {dep.version.display}")
        lines.append(f"  usage sites: {len(dep.usage_sites)}")
        for site in dep.usage_sites:
            if site.evidence_type in (EvidenceType.SDK_CALL, EvidenceType.HTTP_ENDPOINT, EvidenceType.SDK_CONSTRUCTOR):
                lines.append(f"    - {site.location}:{site.line} {site.token}")
        if dep.contract is not None:
            lines.append(f"  contract: {dep.contract.source.type.value} {dep.contract.fingerprint.value[:12]}")
        env = sorted({e.token for e in dep.evidence if e.evidence_type is EvidenceType.ENV_VAR_NAME})
        if env:
            lines.append(f"  env var names: {', '.join(env)}")
        lines.append(f"  confidence: {dep.confidence.value}")
        lines.append("")
    specs = [d for d in inventory.dependencies if d.kind is ExternalDependencyKind.OPENAPI_CONTRACT]
    lines.append("OpenAPI contracts")
    lines.append(f"  specs: {len(specs)}")
    for spec in specs:
        summary = dict(spec.contract.summary) if spec.contract else {}
        fingerprint = spec.contract.fingerprint.value[:12] if spec.contract else "-"
        lines.append(
            f"  - {spec.provider.display_name} [{spec.contract.source.ref if spec.contract else spec.key}] "
            f"version {spec.version.declared or '?'}: {summary.get('paths', 0)} paths, "
            f"{summary.get('operations', 0)} operations, fingerprint {fingerprint}, "
            f"usage sites {len(spec.usage_sites)}"
        )
    lines.append("")
    lines.append(f"Packages: {len(packages)} declared")
    for dep in packages if show_all_packages else packages[:10]:
        lines.append(
            f"  - {dep.package_name} ({dep.ecosystem.value}) {dep.version.display or ''} "
            f"usage sites {len(dep.usage_sites)}".rstrip()
        )
    if not show_all_packages and len(packages) > 10:
        lines.append(f"  ... {len(packages) - 10} more (use --all)")
    lines.append("")
    lines.append(
        f"Scanned {inventory.files_scanned} files; {inventory.secret_store_files_skipped} secret-store "
        "file(s) skipped unread; no secret values read; no network or provider calls."
    )
    return "\n".join(lines) + "\n"


__all__ = ["dependency_to_dict", "inventory_to_dict", "render_inventory_text"]
