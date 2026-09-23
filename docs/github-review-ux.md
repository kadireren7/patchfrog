# GitHub review lifecycle

PatchFrog uses one GitHub Check Run named `PatchFrog review` per repository,
pull request, and exact head SHA. The stable external identity is reconciled on
retries, so repeated webhook delivery updates the existing check instead of
creating duplicate comments or checks. A new head receives a new identity; a
superseded old head is completed as skipped.

The visible lifecycle is:

| Engine outcome | GitHub Check |
| --- | --- |
| accepted for scheduling | queued |
| review executing | in progress |
| completed with accepted findings | completed; success, neutral, or action required from merge readiness |
| completed with no accepted findings | completed; “No actionable findings were accepted” |
| provider/budget-limited partial run | completed; neutral/partial |
| engine or publication failure | completed; failure |
| superseded/ineligible work | completed; skipped |

Inline-capable findings remain inline review comments. Valid findings that
cannot map to a GitHub diff line remain in the review summary with their
structured mapping reason; they are never silently discarded. The Check Run
does not duplicate finding text. It reports execution state and the existing
public engine's merge-readiness decision.

Check Runs and pull-request reviews both use the same installation token, so
the GitHub App identity is preserved. Installations must grant
`checks: write` in addition to `contents: read`, `metadata: read`, and
`pull_requests: write`. No user token or Cloud-only identity is introduced.
