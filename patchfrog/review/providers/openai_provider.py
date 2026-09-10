"""Real :class:`~patchfrog.review.provider.LLMProvider` backed by the
OpenAI API (official ``openai`` Python SDK), via the current-generation
**Responses API** (``client.responses.create``) rather than the legacy
Chat Completions endpoint -- OpenAI's own documentation designates
Responses as the current recommended path for new integrations (see
``validation/model_router_merge_readiness/latest-summary.md`` section 4
for the exact sourcing).

Structured output is enforced via ``text={"format": {"type":
"json_schema", ...}}`` -- PatchFrog's schemas
(:mod:`patchfrog.review.schemas`) are already plain JSON Schema dicts
with ``additionalProperties: False`` set throughout, so ``strict=True``
is used unconditionally rather than adding a Pydantic-model translation
layer PatchFrog doesn't otherwise use for Anthropic/Gemini either -- the
same contract-first, dict-schema-in/raw-JSON-text-out shape as both
existing adapters.

The model is given no tools, no shell, no filesystem, no network, and no
database access: this is a single ``responses.create`` call per
review/critique, nothing more. Reasoning tokens (when the configured
model is a reasoning model) are billed from the same
``max_output_tokens`` budget as the visible JSON answer and reported
separately under ``usage.output_tokens_details.reasoning_tokens`` --
mirroring :mod:`patchfrog.review.providers.gemini_provider`'s own
``thinking_tokens`` distinction. Unlike Gemini, no live validation has
reproduced a truncation failure from this for PatchFrog's own prompt
sizes, so -- unlike Gemini's empirically-justified budget cap -- nothing
here artificially reserves headroom; ``max_output_tokens`` is passed
through unmodified, exactly like the Anthropic adapter. If live testing
later reproduces the same failure mode Gemini had, that adaptation
belongs here then, backed by the same kind of evidence, not invented
speculatively now.

Retry policy lives one layer up, in :mod:`patchfrog.review.service` --
this adapter's job is only to classify each failure as transient or
fatal via the exception types in :mod:`patchfrog.review.provider`, never
to retry itself. The SDK's own default retry behavior (2 attempts) is
disabled per-call for the same reason ``_SDK_MAX_RETRIES = 0``/
``_NO_SDK_RETRY`` disable it for Anthropic/Gemini.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

import httpx2
import openai

from patchfrog.review.provider import (
    ProviderFatalError,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
    ProviderTransientError,
    ProviderUsage,
)

_DEFAULT_TIMEOUT_SECONDS = 30.0

#: Disables the SDK's own built-in retries (default 2 attempts with
#: backoff, for connection errors/408/409/429/5xx) -- PatchFrog's
#: orchestrator applies its own bounded retry on top (see
#: patchfrog.review.service), so the SDK-level default would silently
#: compound retry delay, exactly the same reasoning as the Anthropic and
#: Gemini adapters' own SDK-retry disable.
_SDK_MAX_RETRIES = 0

#: The Responses API's ``text.format.name`` field is a schema identifier
#: with a narrower accepted character set than PatchFrog's own
#: ``schema_name`` values (e.g. ``"review_response:correctness"`` -- a
#: colon is not a safe assumption to send unmodified). Substituted
#: deterministically rather than validated/rejected, since this is
#: purely a request-shaping detail with no semantic meaning PatchFrog
#: reads back from the response.
_UNSAFE_SCHEMA_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_schema_name(schema_name: str) -> str:
    return _UNSAFE_SCHEMA_NAME_CHARS.sub("_", schema_name)


class OpenAILLMProvider:
    """Implements :class:`~patchfrog.review.provider.LLMProvider` against
    the OpenAI API. Credentials are read from the environment / an
    injected key at construction time only -- never logged, never
    persisted, and never written into ``.patchfrog.yml``."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        """``http_client``: test-only injection point. The installed
        ``openai`` SDK (3.x) is built on ``httpx2``, a distinct package
        from the classic ``httpx`` every other adapter/``respx`` in this
        codebase mocks at the transport level -- so this constructor
        accepts an optional pre-built client (backed by
        ``httpx2.MockTransport`` in tests) instead. Never used in
        production; ``None`` (the default) lets the SDK build its own
        real client exactly like the other two adapters."""

        if not api_key:
            raise ValueError("OpenAILLMProvider requires a non-empty api_key")
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=_SDK_MAX_RETRIES,
            http_client=http_client,
        )
        self._model = model
        self._identity = ProviderIdentity(provider="openai", model=model)

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    async def generate_structured(self, request: ProviderRequest) -> ProviderResult:
        start = time.monotonic()
        try:
            response = await self._client.responses.create(
                model=self._model,
                input=[
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_prompt},
                ],
                max_output_tokens=request.max_output_tokens,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": _sanitize_schema_name(request.schema_name),
                        "schema": request.json_schema,
                        "strict": True,
                    }
                },
            )
        except openai.RateLimitError as exc:
            raise ProviderTransientError(f"rate limited: {exc}") from exc
        except openai.APIConnectionError as exc:
            # Covers openai.APITimeoutError too -- APITimeoutError is a
            # subclass of APIConnectionError in this SDK, so catching the
            # parent alone is sufficient and avoids an unreachable
            # duplicate except clause.
            raise ProviderTransientError(f"connection/timeout error: {exc}") from exc
        except openai.InternalServerError as exc:
            raise ProviderTransientError(f"server error: {exc}") from exc
        except openai.APIStatusError as exc:
            raise ProviderFatalError(f"API error {exc.status_code}: {exc}") from exc

        latency_ms = (time.monotonic() - start) * 1000

        if response.status == "incomplete":
            reason = response.incomplete_details.reason if response.incomplete_details else None
            if reason == "content_filter":
                raise ProviderFatalError("provider refused the request (incomplete: content_filter)")
            # "max_output_tokens" (or an unrecognized reason): fall through
            # to normal text extraction, exactly like a truncated
            # Anthropic/Gemini response -- it fails JSON/schema parsing
            # naturally downstream rather than being special-cased here.

        if _any_refusal(response.output):
            raise ProviderFatalError("provider refused the request (refusal content item)")
        text = response.output_text
        if not text:
            raise ProviderFatalError("provider response contained no text content block")

        usage = response.usage
        reasoning_tokens = 0
        if usage is not None and usage.output_tokens_details is not None:
            reasoning_tokens = usage.output_tokens_details.reasoning_tokens or 0
        return ProviderResult(
            raw_json=text,
            usage=ProviderUsage(
                input_tokens=(usage.input_tokens if usage else None) or 0,
                output_tokens=(usage.output_tokens if usage else None) or 0,
                thinking_tokens=reasoning_tokens,
            ),
            latency_ms=latency_ms,
            stop_reason=response.status,
        )


def _any_refusal(output: Sequence[object]) -> bool:
    """Security correction: refusal must win regardless of block/message
    ordering -- a prior version returned the first ``output_text`` block
    it encountered without ever checking whether a *later* block or
    message also carried a refusal, so a response shaped
    ``[output_text, refusal]`` (same message or a later one) would have
    silently returned the text as though the request had not been
    refused. This scans every content block of every message item before
    deciding anything, so ordering can never matter.

    Also why ``response.output_text`` (the official SDK helper, not a
    hand-rolled "return the first block" walk) is used for the text
    itself below: OpenAI's own documentation states it is "not safe to
    assume that the model's text output is present at
    output[0].content[0].text" and that the helper "aggregates all text
    outputs from the model into a single string" -- exactly the multiple-
    output_text-block case this correction also had to account for,
    already solved correctly by the SDK rather than reimplemented here.
    """

    for item in output:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", []) or []:
            if getattr(block, "type", None) == "refusal":
                return True
    return False
