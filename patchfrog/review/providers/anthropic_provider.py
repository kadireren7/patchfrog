"""Real :class:`~patchfrog.review.provider.LLMProvider` backed by the
Claude API (Anthropic SDK).

Structured output is enforced via ``output_config.format`` (a JSON Schema)
rather than free-form text parsing or assistant-turn prefill -- the
provider is contractually incapable of returning anything that doesn't
match the requested schema, which is the first of several defense layers
before a proposal is trusted (see :mod:`patchfrog.review.validation`).

The model is given no tools, no shell, no filesystem, no network, and no
database access: this is a single ``client.messages.create`` call per
review/critique, nothing more. Extended thinking is intentionally left at
its model default (adaptive) rather than disabled -- ``output_config.format``
still constrains the *visible* response to schema-conforming JSON, so
there is no free-text channel for a stray ``<thinking>`` tag or a
plain-text tool call to leak into (that failure mode only applies to
free-form responses, see the skill guidance this was built against).
PatchFrog never reads or requests the model's raw chain of thought --
only ``reasoning_summary``, a short field the schema requires -- which
satisfies the "no hidden chain-of-thought" requirement without needing to
touch ``thinking.display`` at all.

Retry policy lives one layer up, in
:mod:`patchfrog.review.service` -- this adapter's job is only to classify
each failure as transient or fatal via the exception types in
:mod:`patchfrog.review.provider`, never to retry itself.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from patchfrog.review.provider import (
    ProviderFatalError,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
    ProviderTransientError,
    ProviderUsage,
)

#: Anthropic's own SDK already retries connection errors/408/409/429/5xx
#: with backoff by default; PatchFrog's orchestrator applies its own
#: bounded retry on top (see patchfrog.review.service), so the SDK-level
#: default is disabled here to avoid compounding retry delay silently.
_SDK_MAX_RETRIES = 0
_DEFAULT_TIMEOUT_SECONDS = 30.0


class AnthropicLLMProvider:
    """Implements :class:`~patchfrog.review.provider.LLMProvider` against
    the Claude API. Credentials are read from the environment / an
    injected key at construction time only -- never logged, never
    persisted, and never written into ``.patchfrog.yml``."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise ValueError("AnthropicLLMProvider requires a non-empty api_key")
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=_SDK_MAX_RETRIES)
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._identity = ProviderIdentity(provider="anthropic", model=model)

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    async def generate_structured(self, request: ProviderRequest) -> ProviderResult:
        start = time.monotonic()
        try:
            response = await self._client.with_options(timeout=self._timeout_seconds).messages.create(
                model=self._model,
                max_tokens=request.max_output_tokens,
                system=request.system_prompt,
                messages=[{"role": "user", "content": request.user_prompt}],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": request.json_schema,
                    }
                },
            )
        except anthropic.RateLimitError as exc:
            raise ProviderTransientError(f"rate limited: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderTransientError(f"connection error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code in (502, 503, 504) or exc.status_code >= 500:
                raise ProviderTransientError(f"server error {exc.status_code}: {exc}") from exc
            raise ProviderFatalError(f"API error {exc.status_code}: {exc}") from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderTransientError(f"timeout: {exc}") from exc

        latency_ms = (time.monotonic() - start) * 1000

        if response.stop_reason == "refusal":
            raise ProviderFatalError("provider refused the request (stop_reason=refusal)")

        text = _extract_text(response.content)
        if text is None:
            raise ProviderFatalError("provider response contained no text content block")

        usage = ProviderUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return ProviderResult(
            raw_json=text,
            usage=usage,
            latency_ms=latency_ms,
            stop_reason=response.stop_reason,
        )


def _extract_text(content: list[Any]) -> str | None:
    for block in content:
        if getattr(block, "type", None) == "text":
            return str(block.text)
    return None
