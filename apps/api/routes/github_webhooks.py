"""``POST /webhooks/github`` — the sole entrypoint for GitHub App events.

Deliberately does the minimum amount of work before responding: verify the
signature, parse just enough of the payload to decide if it's relevant,
and enqueue a Celery task. All GitHub API calls and persistence happen in
the worker (:mod:`apps.worker.tasks.process_pull_request`).
"""

from __future__ import annotations

import json
from typing import Annotated

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from apps.api.dependencies import SettingsDep
from apps.worker.tasks.process_pull_request import process_pull_request_event
from patchfrog.github.errors import WebhookPayloadError
from patchfrog.github.signatures import verify_signature
from patchfrog.github.webhooks import parse_pull_request_event

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = structlog.get_logger(__name__)


@router.post("/github")
async def receive_github_webhook(
    request: Request,
    settings: SettingsDep,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    raw_body = await request.body()

    if not verify_signature(
        secret=settings.github_webhook_secret,
        payload=raw_body,
        signature_header=x_hub_signature_256,
    ):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    if not x_github_event or not x_github_delivery:
        raise HTTPException(status_code=400, detail="missing required GitHub webhook headers")

    try:
        payload = json.loads(raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="malformed JSON payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="malformed JSON payload")

    try:
        event = parse_pull_request_event(
            event_name=x_github_event, delivery_id=x_github_delivery, payload=payload
        )
    except WebhookPayloadError as exc:
        logger.warning(
            "webhook_payload_invalid", github_delivery_id=x_github_delivery, error=str(exc)
        )
        raise HTTPException(status_code=400, detail="malformed webhook payload") from exc

    if event is None:
        logger.info(
            "webhook_event_ignored",
            github_delivery_id=x_github_delivery,
            github_event=x_github_event,
        )
        return JSONResponse(status_code=200, content={"detail": "ignored"})

    process_pull_request_event.delay(
        delivery_id=event.delivery_id,
        action=event.action.value,
        github_repository_id=event.repository.github_repository_id,
        owner=event.repository.owner,
        name=event.repository.name,
        full_name=event.repository.full_name,
        installation_id=event.repository.installation.id,
        pull_request_number=event.pull_request_number,
        pull_request_title=event.pull_request_title,
        pull_request_body=event.pull_request_body,
        author=event.author,
        base_branch=event.base_branch,
        head_branch=event.head_branch,
        base_sha=event.base_sha,
        head_sha=event.head_sha,
        html_url=event.html_url,
    )

    logger.info(
        "pull_request_event_queued",
        github_delivery_id=event.delivery_id,
        repository=event.repository.full_name,
        pull_request_number=event.pull_request_number,
        event_action=event.action.value,
    )

    return JSONResponse(status_code=202, content={"detail": "queued"})
