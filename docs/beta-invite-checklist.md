# Beta Invite Checklist

One page, per invited repository. Copy this template for each real
invite -- do not fill in real contact details or repository names here
in the committed copy; keep your filled-in copies wherever you track
operational state, not in this repository.

```
Repository:              <owner>/<repo>  (fill in when actually inviting)
Repository owner/contact: (fill in -- never commit a real name/email/handle here)
Date:                     (fill in)

[ ] GitHub App installed on this repository
[ ] Installation confirmed via `patchfrog ops installations`
[ ] (if BETA_ALLOWLIST_MODE=true) installation activated via
    `patchfrog ops installations --activate <id>`
[ ] `patchfrog ops doctor` reports no FAIL
[ ] Provider configured and credential present (doctor's
    review_provider_credential check)
[ ] Operator hard caps reviewed and appropriate for this repository
    (doctor's operator_hard_caps line)
[ ] Publication mode agreed with the repository owner:
      [ ] DRY_RUN only for now (validate quality before any real comment)
      [ ] PUBLISH from the start
[ ] If PUBLISH: installation `--allow-publication` set
[ ] Repository's own `.patchfrog.yml` present and valid (if the
    repository wants non-default behavior -- otherwise conservative
    defaults apply with no file at all)
[ ] `patchfrog ops preflight --repository <owner>/<repo>` reports the
    outcome that matches the agreed publication mode above
    (DRY_RUN or PUBLISH, never BLOCKED)
[ ] Test PR opened
[ ] First review completed (`patchfrog ops failed` shows nothing new
    for this repository, or `patchfrog telemetry review <run-id>`
    shows status=succeeded)
[ ] Telemetry reviewed for the first run (`patchfrog telemetry review
    <run-id>`) -- provider usage, effort tier, any findings, all
    sane
[ ] Feedback path explained to the repository owner (reactions,
    `/patchfrog useful`/`false-positive`/`fixed`/`ignore` commands --
    see docs/feedback.md)
[ ] Rollback/suspend path known to the operator for this repository
    (see docs/beta-runbook.md's "Suspend a repository" /
    "Disable publication for one installation")
```

See `docs/beta-runbook.md` for what each command above actually does,
and `docs/external-beta.md` for the limitations to set expectations
with the repository owner before inviting them.
