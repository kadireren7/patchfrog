"""Real :class:`~patchfrog.review.provider.LLMProvider` backed by the
Gemini API (Google GenAI SDK).

Structured output is enforced via ``response_mime_type="application/json"``
+ ``response_json_schema`` (PatchFrog's existing JSON Schema, passed
through unmodified -- see :mod:`patchfrog.review.schemas`) rather than
free-form text parsing -- the same contract-first approach as
:mod:`patchfrog.review.providers.anthropic_provider`. Gemini's JSON
Schema support is a documented subset of the spec (no ``$schema``,
limited keyword set), but every keyword PatchFrog's schemas actually use
(``type``, ``enum``, ``anyOf``, ``properties``, ``required``,
``additionalProperties``, ``items``) is supported, so no schema fork was
needed.

The model is given no tools, no shell, no filesystem, no network, and no
database access: this is a single ``generate_content`` call per
review/critique, nothing more. Extended thinking is left *enabled*
(never ``thinking_budget=0``), mirroring the Anthropic adapter's own
choice not to suppress a model's native reasoning step -- PatchFrog only
ever reads the final structured JSON, never any thought content,
regardless of whether thinking is on.

Unlike Anthropic, Gemini's thinking tokens are drawn from the *same*
``max_output_tokens`` budget as the visible JSON response, and its
default thinking budget is unbounded ("AUTOMATIC"). Live testing during
this provider's validation reproduced real, intermittent truncated-JSON
failures caused by exactly this: on some calls the model spent nearly
all of ``max_output_tokens`` thinking, leaving too little room to finish
writing the JSON. ``thinking_budget`` is therefore explicitly capped
(never disabled) to guarantee :data:`_MIN_RESERVED_OUTPUT_TOKENS` of
headroom for the answer -- a provider-boundary adaptation, not a schema
change (see :mod:`patchfrog.review.schemas`, untouched).

Retry policy lives one layer up, in :mod:`patchfrog.review.service` --
this adapter's job is only to classify each failure as transient or fatal
via the exception types in :mod:`patchfrog.review.provider`, never to
retry itself. The SDK's own default retry behavior is disabled for the
same reason ``_SDK_MAX_RETRIES = 0`` disables Anthropic's.
"""

from __future__ import annotations

import time

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from patchfrog.review.provider import (
    ProviderFatalError,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
    ProviderTransientError,
    ProviderUsage,
)

_DEFAULT_TIMEOUT_SECONDS = 30.0

#: Disables the SDK's own built-in retries (default 5 attempts with
#: backoff) -- PatchFrog's orchestrator applies its own bounded retry on
#: top (see patchfrog.review.service), so the SDK-level default would
#: silently compound retry delay, exactly the same reasoning as
#: Anthropic's _SDK_MAX_RETRIES = 0.
_NO_SDK_RETRY = genai_types.HttpRetryOptions(attempts=1)

#: finish_reason values that mean "a response was withheld" (content
#: safety/policy filtering, recitation, or the model otherwise refused to
#: answer) -- the Gemini analogue of Anthropic's ``stop_reason ==
#: "refusal"`` check. MAX_TOKENS is deliberately excluded: a truncated
#: response still gets normal text extraction and fails schema/JSON
#: validation naturally downstream, exactly like a truncated Anthropic
#: response would.
_REFUSAL_FINISH_REASONS = frozenset(
    {
        genai_types.FinishReason.SAFETY,
        genai_types.FinishReason.RECITATION,
        genai_types.FinishReason.LANGUAGE,
        genai_types.FinishReason.BLOCKLIST,
        genai_types.FinishReason.PROHIBITED_CONTENT,
        genai_types.FinishReason.SPII,
        genai_types.FinishReason.OTHER,
    }
)

#: Minimum tokens guaranteed to remain for the visible JSON answer after
#: thinking. Gemini's thinking tokens draw from the same
#: ``max_output_tokens`` budget as the answer -- reproduced live: an
#: unbounded ("AUTOMATIC") thinking budget intermittently consumed nearly
#: the entire budget, truncating the JSON mid-object. Chosen generously
#: relative to a single finding's typical size (a few hundred tokens) so
#: even a multi-finding response has room to complete.
_MIN_RESERVED_OUTPUT_TOKENS = 1024


class GeminiLLMProvider:
    """Implements :class:`~patchfrog.review.provider.LLMProvider` against
    the Gemini API. Credentials are read from the environment / an
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
            raise ValueError("GeminiLLMProvider requires a non-empty api_key")
        self._client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(
                timeout=int(timeout_seconds * 1000),
                retry_options=_NO_SDK_RETRY,
            ),
        )
        self._model = model
        self._identity = ProviderIdentity(provider="gemini", model=model)

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    async def generate_structured(self, request: ProviderRequest) -> ProviderResult:
        start = time.monotonic()
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=request.user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=request.system_prompt,
                    max_output_tokens=request.max_output_tokens,
                    response_mime_type="application/json",
                    response_json_schema=request.json_schema,
                    thinking_config=genai_types.ThinkingConfig(
                        thinking_budget=max(0, request.max_output_tokens - _MIN_RESERVED_OUTPUT_TOKENS)
                    ),
                ),
            )
        except genai_errors.ClientError as exc:
            if exc.code in (401, 403):
                raise ProviderFatalError(f"authentication error {exc.code}: {exc}") from exc
            if exc.code == 429:
                # Gemini uses 429/RESOURCE_EXHAUSTED for both ordinary
                # per-minute rate limiting and daily quota exhaustion --
                # the HTTP layer alone can't reliably distinguish them.
                # Treated as transient (bounded-retry-safe) to match
                # Anthropic's own RateLimitError handling; a *persistent*
                # 429 across retries is a session/operator-level signal
                # to stop, not something this adapter can detect alone.
                raise ProviderTransientError(f"rate limited or quota exhausted: {exc}") from exc
            raise ProviderFatalError(f"invalid request {exc.code}: {exc}") from exc
        except genai_errors.ServerError as exc:
            raise ProviderTransientError(f"server error {exc.code}: {exc}") from exc
        except httpx.RequestError as exc:
            # Connection failure, DNS error, or a timeout at the
            # transport layer (the SDK is httpx-based) -- raised before
            # any HTTP status is available, so never wrapped in
            # ClientError/ServerError above.
            raise ProviderTransientError(f"network error: {exc}") from exc

        latency_ms = (time.monotonic() - start) * 1000

        candidates = response.candidates or []
        finish_reason = candidates[0].finish_reason if candidates else None
        if finish_reason in _REFUSAL_FINISH_REASONS:
            raise ProviderFatalError(f"provider refused the request (finish_reason={finish_reason})")

        text = response.text
        if text is None:
            raise ProviderFatalError("provider response contained no text content block")

        usage_metadata = response.usage_metadata
        usage = ProviderUsage(
            input_tokens=(usage_metadata.prompt_token_count if usage_metadata else None) or 0,
            output_tokens=(usage_metadata.candidates_token_count if usage_metadata else None) or 0,
            thinking_tokens=(usage_metadata.thoughts_token_count if usage_metadata else None) or 0,
        )
        return ProviderResult(
            raw_json=text,
            usage=usage,
            latency_ms=latency_ms,
            stop_reason=finish_reason.value if finish_reason else None,
        )
