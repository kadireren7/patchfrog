"""M9.11: optional, opt-in harness for a *real* GitHub App migration-PR
publish -- never run as part of the normal test suite.

This test is skipped unless **all** of the following are explicitly set,
which they never are in CI or in a normal developer run:

- ``PATCHFROG_MIGRATION_PR_REAL_GITHUB_E2E=1`` -- the actual opt-in; every
  other variable below is inert without this one.
- ``PATCHFROG_MIGRATION_PR_E2E_INSTALLATION_ID`` -- a GitHub App
  installation ID the operator controls.
- ``PATCHFROG_MIGRATION_PR_E2E_REPOSITORY`` -- ``owner/repo`` of a
  disposable fixture repository that installation may write to. Never a
  production repository, and never PatchFrog's own.

No secret value is read or printed by this module itself --
``GitHubClient``/``InstallationTokenProvider`` resolve credentials the
same way the production worker does (`patchfrog/config/settings.py`),
entirely out of this test's own code path. This module exists so the
harness exists (M9.11's own requirement to "add" one) without ever
exercising it here: doing so would mutate a real GitHub repository, which
this milestone's own hard constraints forbid without already-configured
credentials and explicit, separate operator approval.

A real run (when someone deliberately opts in, outside of this milestone)
would: build a trivially `VERIFIED` `MigrationPRPlan` against the fixture
repository's current default-branch head, publish it once via
`MigrationPRPublisher` in `PUBLISH` mode against the real
`GitHubClientMigrationPublisher`, assert a real PR now exists, then close
it -- and never merge it.
"""

from __future__ import annotations

import os

import pytest

_ENABLED = os.environ.get("PATCHFROG_MIGRATION_PR_REAL_GITHUB_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not _ENABLED,
    reason=(
        "opt-in only -- set PATCHFROG_MIGRATION_PR_REAL_GITHUB_E2E=1 (plus "
        "PATCHFROG_MIGRATION_PR_E2E_INSTALLATION_ID and "
        "PATCHFROG_MIGRATION_PR_E2E_REPOSITORY) to run this against a real, "
        "disposable fixture repository; never enabled in CI"
    ),
)


def test_real_publish_opens_and_reconciles_one_pr() -> None:
    """Deliberately not implemented in this milestone -- see the module
    docstring. Reaching this line at all means the opt-in gate above has
    failed open, which would itself be the bug worth catching; the body
    fails loudly rather than silently doing nothing under a green check."""

    pytest.fail(
        "the real-GitHub migration PR E2E harness is not implemented in this milestone -- "
        "see docs/migration-pr.md's 'Known limitations' section"
    )
