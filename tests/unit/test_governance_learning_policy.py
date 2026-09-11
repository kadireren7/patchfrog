"""Z16 -- governance controls learning corpus (spec tests Y11/Z19/Z20)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from patchfrog.analysis.domain import FindingCategory
from patchfrog.governance.domain import PolicyRule, PolicyScope
from patchfrog.governance.learning_policy import (
    filter_noise_records_for_policy,
    personalization_effects_allowed,
)
from patchfrog.governance.precedence import PLATFORM_POLICY, merge_policies
from patchfrog.learning_records.domain import (
    LearningEvidenceRef,
    LearningMaturity,
    LearningSurface,
    LearningType,
    RepositoryLearningRecord,
)

_REPO_ID = uuid.uuid4()


def _noise_record(category: FindingCategory) -> RepositoryLearningRecord:
    now = datetime.now(UTC).isoformat()
    return RepositoryLearningRecord(
        id=uuid.uuid4(),
        repository_id=_REPO_ID,
        learning_type=LearningType.NOISE_SUPPRESSION,
        surface=LearningSurface(file_path="a.py", qualified_name="a.f", category=category),
        maturity=LearningMaturity.ESTABLISHED,
        support_count=3,
        evidence=(LearningEvidenceRef(finding_id=uuid.uuid4(), review_run_id=uuid.uuid4(), observed_at=now),),
        first_observed_at=now,
        last_observed_at=now,
    )


def test_security_noise_suppression_forbidden_by_default_platform_policy() -> None:
    effective = merge_policies(PLATFORM_POLICY, None, None)
    records = (_noise_record(FindingCategory.SECURITY), _noise_record(FindingCategory.CORRECTNESS))
    filtered = filter_noise_records_for_policy(records, policy=effective)
    assert all(r.surface.category is not FindingCategory.SECURITY for r in filtered)
    assert any(r.surface.category is FindingCategory.CORRECTNESS for r in filtered)


def test_non_security_noise_suppression_unaffected() -> None:
    effective = merge_policies(PLATFORM_POLICY, None, None)
    records = (_noise_record(FindingCategory.CORRECTNESS),)
    assert filter_noise_records_for_policy(records, policy=effective) == records


def test_org_disabling_personalization_is_reflected_in_effective_policy() -> None:
    org = PolicyRule(scope=PolicyScope.ORGANIZATION, personalization_enabled=False)
    effective = merge_policies(PLATFORM_POLICY, org, None)
    assert personalization_effects_allowed(effective) is False


def test_personalization_enabled_by_default() -> None:
    effective = merge_policies(PLATFORM_POLICY, None, None)
    assert personalization_effects_allowed(effective) is True
