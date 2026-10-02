"""Bounded internal-contract support (M10.5).

Beyond external providers, one repository of a workspace may *produce* a
contract other repositories consume: a published package, an OpenAPI spec,
a generated client, an internal SDK. This module models only that, and only
through **explicit identity**:

* a package identity (ecosystem + package name), or
* an OpenAPI/API identity (declared API hosts), or
* an explicit M5 dependency key.

No relationship is ever inferred from repository names, organization,
directory layout or similarity. A consumer is related to an internal
contract only because its *own* M5 discovery evidence (manifest declaration,
import, OpenAPI server host, ...) matches that explicit identity -- the very
same matcher external changes use (:func:`patchfrog.upstream.consumers.match_dependency`).
There is deliberately no second graph and no new relation table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from patchfrog.dependencies.domain import Ecosystem
from patchfrog.upstream.domain import DependencyTarget


class InternalContractKind(StrEnum):
    PACKAGE = "package"
    OPENAPI = "openapi"
    GENERATED_CLIENT = "generated_client"
    INTERNAL_SDK = "internal_sdk"


class InternalContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InternalContract:
    contract_id: str
    kind: InternalContractKind
    #: The repository that owns/publishes the contract.
    producer_repository: str
    target: DependencyTarget

    def __post_init__(self) -> None:
        if not self.contract_id.strip():
            raise InternalContractError("an internal contract needs an explicit contract_id")
        if not self.producer_repository.strip():
            raise InternalContractError("an internal contract needs an explicit producer repository")
        has_package = bool(self.target.package_name) and self.target.ecosystem not in (None, Ecosystem.NONE)
        if not (has_package or self.target.api_hosts or self.target.dependency_key):
            raise InternalContractError(
                f"internal contract {self.contract_id!r} has no explicit identity: give a package "
                "(ecosystem + name), API hosts, or a dependency key -- repository names are never used"
            )

    @property
    def label(self) -> str:
        return self.target.display_name or self.contract_id


def contract_target(
    *,
    package_name: str | None = None,
    ecosystem: Ecosystem | None = None,
    api_hosts: Sequence[str] = (),
    dependency_key: str | None = None,
    modules: Sequence[str] = (),
    display_name: str | None = None,
) -> DependencyTarget:
    """Build the explicit identity of an internal contract. ``provider_key``
    is intentionally never set: a provider-key match would be a broader,
    weaker signal than the explicit identity this model demands."""

    return DependencyTarget(
        package_name=package_name, ecosystem=ecosystem, api_hosts=tuple(sorted(set(api_hosts))),
        dependency_key=dependency_key, modules=tuple(sorted(set(modules))), display_name=display_name,
    )


def producers(contracts: Sequence[InternalContract]) -> frozenset[str]:
    return frozenset(c.producer_repository for c in contracts)


__all__ = ["InternalContract", "InternalContractError", "InternalContractKind", "contract_target", "producers"]
