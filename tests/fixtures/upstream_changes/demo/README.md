# Deterministic migration demo (fictional `acme-ai` SDK)

`acme-ai` is a made-up SDK -- it exists only to show the flow end to end:
old contract `client.chat.create(prompt=...)` -> new contract
`client.responses.create(input=...)`. No real vendor API is described.

    python -m patchfrog.cli migrations demo

This fixture has one consumer usage the migration cannot fully automate
(a removed `stream` argument), so the full pipeline -- extended through
M8 verification and the M9 dry-run PR dossier -- always ends in
`HUMAN_REQUIRED` / `PLAN_ONLY` here. See the sibling `demo_verified/`
fixture (`python -m patchfrog.cli migrations demo --verified`) for the
fully-automatic counterpart, which reaches `VERIFIED` / `AUTO_OPEN` with
real baseline-vs-patched evidence.
