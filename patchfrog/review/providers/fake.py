"""Deterministic, scripted :class:`~patchfrog.review.provider.LLMProvider`
for tests.

Used across nearly every Phase 5 test category (candidate/prompt
plumbing, validation, critic, dedup, persistence/idempotency, cost
guards, dogfood) precisely so those tests never depend on real network
access or non-deterministic model output. Scripts are consumed in the
order provided, keyed by nothing but call order -- callers that need
per-candidate control build one script per expected call, and
:meth:`FakeLLMProvider.calls` records every request made so tests can
assert exactly what was sent (e.g. that a secret never appears in a
prompt).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from patchfrog.review.provider import (
    LLMProvider,
    ProviderError,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)


@dataclass(frozen=True, slots=True)
class ScriptedResponse:
    """One scripted reply. ``raw_json`` is returned verbatim as
    :attr:`ProviderResult.raw_json` -- deliberately not validated here, so
    a test can script malformed/hallucinated output to exercise
    :mod:`patchfrog.review.validation` and :mod:`patchfrog.review.critic`."""

    raw_json: str
    input_tokens: int = 100
    output_tokens: int = 50
    #: See :attr:`~patchfrog.review.provider.ProviderUsage.thinking_tokens`
    #: -- 0 by default, matching most scripted-response tests that don't
    #: care about it; set explicitly to exercise thinking-token
    #: accounting (Quality + Cost Guard, spec section 10).
    thinking_tokens: int = 0
    latency_ms: float = 5.0
    stop_reason: str | None = "end_turn"


class FakeLLMProvider:
    """Implements :class:`LLMProvider`. Not a subclass -- structural typing
    via ``Protocol`` is the point of the abstraction."""

    def __init__(
        self,
        responses: Sequence[ScriptedResponse | Exception] | None = None,
        *,
        provider_name: str = "fake",
        model_id: str = "fake-model-1",
        response_factory: Callable[[ProviderRequest], ScriptedResponse | Exception] | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._response_factory = response_factory
        self._identity = ProviderIdentity(provider=provider_name, model=model_id)
        self.calls: list[ProviderRequest] = []

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    async def generate_structured(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request)

        if self._response_factory is not None:
            outcome = self._response_factory(request)
        elif self._responses:
            outcome = self._responses.pop(0)
        else:
            raise ProviderError("FakeLLMProvider exhausted: no scripted response available")

        if isinstance(outcome, Exception):
            raise outcome

        return ProviderResult(
            raw_json=outcome.raw_json,
            usage=ProviderUsage(
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                thinking_tokens=outcome.thinking_tokens,
            ),
            latency_ms=outcome.latency_ms,
            stop_reason=outcome.stop_reason,
        )


# mypy structural check -- fails loudly at import time if the Protocol drifts.
def _assert_protocol_conformance(provider: LLMProvider) -> None:
    del provider


_assert_protocol_conformance(FakeLLMProvider([]))


_Route = ScriptedResponse | Exception | Callable[[ProviderRequest], "ScriptedResponse | Exception"]


def route_by_schema_name(
    routes: Mapping[str, _Route], *, default: ScriptedResponse | Exception | None = None
) -> Callable[[ProviderRequest], ScriptedResponse | Exception]:
    """A ``response_factory`` that dispatches deterministically on
    ``request.schema_name`` -- the discriminator Agent Orchestration v1
    uses to distinguish which specialist role (or the critic) made a
    given call (``"review_response:correctness"``,
    ``"review_response:security"``, ``"critic_verdict"``). Lets a test
    script each role's response independently without depending on call
    order, which becomes ambiguous once two specialist calls for one
    candidate can run concurrently (see
    :mod:`patchfrog.review.orchestration`).

    Each route may be a fixed :class:`ScriptedResponse`/``Exception``, or
    a callable for routes that need to vary per-request (e.g. matching
    on which candidate's prompt this is)."""

    def factory(request: ProviderRequest) -> ScriptedResponse | Exception:
        route = routes.get(request.schema_name, default)
        if route is None:
            raise ProviderError(
                f"route_by_schema_name: no route for schema_name={request.schema_name!r} and no default"
            )
        if callable(route):
            return route(request)
        return route

    return factory
