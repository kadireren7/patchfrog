"""A tiny, fully local, fictional stand-in for the "acme-ai" SDK's 2.0
surface -- given only to Migration *Verification* (M8), never to
dependency discovery/planning, so it cannot change what M6/M7 detect as
an external dependency. See
:data:`patchfrog.cli_changes.DEMO_SDK_STUB_ROOT`.

Deliberately implements only the 2.0 (``responses.create``) shape, never
``chat.create`` -- this simulates the environment already having the new
SDK installed. Code still written against the 1.4 surface
(``client.chat.create(...)``) genuinely raises ``AttributeError`` here,
exactly like the real upstream removal it stands in for; migrated code
(``client.responses.create(...)``) genuinely runs and returns a value.
This is what lets the M8 baseline-vs-patched comparison observe a real
``baseline_fail_patched_pass`` result instead of only a syntactic one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Response:
    output_text: str
    id: str


@dataclass(frozen=True, slots=True)
class Embedding:
    id: str
    vector: tuple[float, ...]


class _Responses:
    def create(self, *, model: str, input: str, temperature: float | None = None) -> Response:
        return Response(output_text=f"echo: {input}", id="resp_demo_1")


class _Embeddings:
    def create(self, *, model: str, input: str) -> Embedding:
        return Embedding(id="emb_demo_1", vector=(0.0, 0.0, 0.0))


class Client:
    def __init__(self) -> None:
        self.responses = _Responses()
        self.embeddings = _Embeddings()


__all__ = ["Client", "Embedding", "Response"]
