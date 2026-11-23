# Apache-2.0 Migration — Audit and Requirements

Status: **audit complete, license NOT changed** (owner decision D3, 2026-11-22). The intended
final license is Apache-2.0. `LICENSE` (ELv2) stays authoritative until the owner explicitly
approves the switch. This is an engineering audit, not legal advice. Have counsel review it
before the switch if PatchFrog Cloud has contracts or investors that refer to the license.

## 1. Ownership and contributions (audited)

| Check | Finding | Evidence |
|---|---|---|
| Commit authors | One person, under two spellings of the same name and email domain | `git log --format='%an <%ae>' \| sort -u` |
| External contributions | None in the commit history: no other commit author on `main` (GitHub PR-level authorship wasn't checked, so confirm via `gh pr list --state merged` before the switch) | `git shortlog -sne HEAD` (54 commits) |
| CLA/DCO | None in place. `CONTRIBUTING.md` says contributions are licensed "under the same terms as the rest of the repository" | `CONTRIBUTING.md` |
| AI-assisted code | Commits carry `Co-Authored-By: Claude …` trailers | `git log --format='%(trailers:key=Co-Authored-By)'` |
| Third-party code copied into the repo | None found: no third-party copyright or SPDX headers, and no LICENSE files under `tests/fixtures/` or `validation/` | `grep -rliE "copyright \(c\)\|SPDX-License-Identifier"` |
| History | Apache-2.0 → ELv2 already happened once. Apache-2.0 grants for the old versions stay valid | `docs/licensing.md`, `docs/legal/LICENSE-APACHE-2.0-HISTORICAL.txt` |

**Conclusion:** as the only copyright holder, the owner can relicense without anyone else's
consent. On AI co-authorship: copyright in AI-assisted output belongs to the human author where
it is protectable at all, and the provider's terms assign outputs to the user. Apache-2.0
doesn't change this question, and the relicense doesn't depend on it.

## 2. Dependency license compatibility (audited)

The runtime dependency tree (114 distributions, transitive, resolved 2026-11-22 from
`pip install -e .`) was read from the installed package metadata:

| License family | Count | Compatible with an Apache-2.0 project? |
|---|---:|---|
| MIT / MIT-0 / BSD-2/3 / PSF / ISC-style | ~85 | yes |
| Apache-2.0 (incl. dual licenses) | ~25 | yes |
| **LGPL-2.1-or-later**: `semgrep` | 1 | yes as a **separately installed dependency invoked as a subprocess** (that's how `patchfrog/analysis/analyzers/semgrep.py` uses it). Obligation: images and bundles that **redistribute** semgrep (the Docker images) must carry its license and a source offer or pointer |
| **MPL-2.0**: `certifi` | 1 | yes when used unmodified (file-level copyleft only) |
| No metadata: `peewee` (MIT, by its license file), `face` (BSD-3-Clause, by its license file) | 2 | yes. Both come in through semgrep |

There are no GPL, AGPL, SSPL, BUSL or ELv2 dependencies. External system tools (cppcheck GPL-3,
clang-tidy Apache-2.0 with LLVM exception, bwrap LGPL, k6 AGPL-3) are **discovered, never
bundled**. That has to stay true, especially for k6 (AGPL): never vendor it into an image or
wheel.

## 3. Migration requirements (to do when D3 is executed)

1. Replace `LICENSE` with the unmodified Apache-2.0 text, and add a `NOTICE` file (project name,
   copyright line, third-party notices for anything redistributed in images).
2. `pyproject.toml`: `license = "Apache-2.0"` (an SPDX string) plus `license-files`. This also
   fixes the setuptools deprecation of the table form, which has a hard deadline of
   **2027-02-18**.
3. Update `README.md` badges and text, `docs/licensing.md` (add a new historical note:
   ELv2 period from the switch date back to the earlier Apache period), `CONTRIBUTING.md`, the
   relevant parts of `docs/product-boundary.md`, and `docs/external-beta.md`.
4. Choose an inbound contribution model **before** accepting outside PRs. Recommendation:
   **DCO** (`Signed-off-by`), enforced by a CI check. It is lighter than a CLA, and since
   Apache-2.0 §5 already covers inbound licensing, a CLA is only needed if a future relicense
   must stay possible without contributor consent.
5. Trademark: Apache-2.0 §6 grants no trademark rights, so `TRADEMARK.md` keeps governing the
   name, logo (`docs/assets/brand/`) and bot identity. State in `NOTICE`/README that the brand
   assets aren't covered by the code license.
6. Docker images: add the third-party license bundle for redistributed packages (semgrep's LGPL
   text, certifi's MPL notice).
7. Private Cloud: Apache-2.0 places no obligations on proprietary use beyond keeping notices.
   Check that the Cloud repo's dependency metadata and any customer-facing statements refer to
   the new license. Cloud code itself doesn't change.
8. Announce the change (CHANGELOG, release notes) with a clear effective commit. Older commits
   keep whatever license they were released under.
9. Add a CI license check for new runtime dependencies (allowlist: MIT/BSD/Apache/ISC/PSF/MPL;
   anything else needs an ADR).

## 4. Until the switch

- Don't describe PatchFrog as "open source" in user-facing material. Say "source-available".
- New runtime dependencies must already satisfy the §2 compatibility rules, so the switch stays a
  documentation change.
