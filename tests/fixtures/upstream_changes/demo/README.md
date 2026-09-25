# Deterministic migration demo (fictional `acme-ai` SDK)

`acme-ai` is a made-up SDK -- it exists only to show the flow end to end:
old contract `client.chat.create(prompt=...)` -> new contract
`client.responses.create(input=...)`. No real vendor API is described.

    python -m patchfrog.cli migrations demo
