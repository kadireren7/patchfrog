"""Z15: policy-required executable verification.

Pure function only -- **never weakens the sandbox, never expands
execution privileges, never auto-enables unsafe execution**. This
module only ever answers "does policy require verification for this
candidate," a input to
:mod:`patchfrog.governance.merge_readiness_policy`'s own
``VerificationSignal.required`` -- S/S6's fail-closed sandbox semantics
(:mod:`patchfrog.executable_verification`) are never touched, imported,
or reimplemented here.
"""

from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory
from patchfrog.governance.domain import EffectivePolicy


def is_verification_required(policy: EffectivePolicy, *, category: FindingCategory, file_path: str) -> bool:
    """True if ``policy`` requires executable verification for a
    candidate of this category or under one of its required path
    prefixes. Explicit, bounded prefix matching only -- never a glob/
    regex engine (see :data:`patchfrog.governance.domain.MAX_PATH_PREFIXES`)."""

    if category in policy.verification_required_categories:
        return True
    return any(file_path.startswith(prefix) for prefix in policy.verification_required_path_prefixes)
