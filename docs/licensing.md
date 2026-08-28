# Licensing

## Current license

PatchFrog is **source available**, licensed under the **Elastic License 2.0
(ELv2)** as of this repository's current `LICENSE` file. This repository is
**not** an OSI-approved "open source" project under this license, and should
not be described as one going forward. "Source available" is the accurate
term: the source code is visible, self-hosting is permitted, and
modification is permitted, all subject to the terms of `LICENSE`.

The full, official, unmodified text of the Elastic License 2.0 is in
[`LICENSE`](../LICENSE) at the repository root
(source: <https://www.elastic.co/licensing/elastic-license>). Nothing in
this document alters, extends, or restates that text -- `LICENSE` alone is
authoritative. Where anything below and `LICENSE` appear to conflict,
`LICENSE` governs.

## Historical notice: prior Apache-2.0 releases

Earlier commits in this repository's history were released under the
Apache License, Version 2.0. **Rights already granted under Apache-2.0 to
anyone who obtained the software under those terms are not revoked or
retroactively changed by this license change.** Software you already
received under Apache-2.0 remains available to you under Apache-2.0's own
terms, for that historical version.

The Apache-2.0 text those historical versions were released under is
preserved for reference at
[`docs/legal/LICENSE-APACHE-2.0-HISTORICAL.txt`](legal/LICENSE-APACHE-2.0-HISTORICAL.txt)
(an exact copy of the `LICENSE` file as it stood immediately before this
change, also always recoverable from git history at any earlier commit).

**Going forward from this license change, all new and modified code in this
repository is released only under the Elastic License 2.0** in the current
`LICENSE` file -- there is no dual-licensing arrangement and no ongoing
Apache-2.0 track.

## What Elastic License 2.0 means for PatchFrog, in practice

This section describes PatchFrog's own intended, practical product model
under ELv2. It is **not** a restatement or extension of the license's legal
terms -- for the actual terms, read `LICENSE`. Where this section describes
something as "intent" rather than a guarantee, treat it exactly that way:
product framing, not a legal warranty.

### Self-hosted / source-available PatchFrog

Under ELv2, you may:

- View and read the full source code.
- Self-host PatchFrog for your own use, or your organization's internal
  use.
- Modify the software, subject to ELv2's terms (in particular, its
  "Notices" section: you must carry these license terms forward, and
  clearly mark any modified copies as modified).
- Connect your own supported AI provider credentials (see
  [BYOK](product-boundary.md#byok-bring-your-own-key-self-hosted)).
- Run your own GitHub App installation, with your own App identity (see
  [GitHub App boundary](product-boundary.md#github-app-boundary)).
- Operate PatchFrog for yourself or your organization.

### What ELv2 does not permit

Per `LICENSE`'s own "Limitations" section: you may not provide the software
to third parties as a hosted or managed service that gives those third
parties access to a substantial set of the software's features or
functionality. In practical terms for PatchFrog, this means the intent is
that you should not stand up a hosted PatchFrog-as-a-service offering for
others based on this source.

Separately (a product/trademark position, not a restatement of ELv2's own
terms -- see [`TRADEMARK.md`](../TRADEMARK.md)):

- Do not present a fork or self-hosted deployment as *the* official
  PatchFrog service.
- Do not use PatchFrog's branding (name, logo, `patchfrog[bot]` identity)
  in a way that misleadingly implies your deployment is official or
  endorsed.

If something isn't clearly settled by `LICENSE`'s own text, this document
does not attempt to invent a legal answer -- it states PatchFrog's product
intent instead, and you should read `LICENSE` (and seek your own legal
advice if needed) for anything that actually turns on the license's legal
meaning.

## See also

- [`TRADEMARK.md`](../TRADEMARK.md) -- name/logo/bot-identity usage policy.
- [`docs/product-boundary.md`](product-boundary.md) -- the self-hosted vs.
  PatchFrog Cloud architecture and product boundary.
