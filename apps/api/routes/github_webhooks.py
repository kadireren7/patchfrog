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
from apps.worker.tasks.sync_installation import (
    sync_installation_event_task,
    sync_installation_repositories_event_task,
)
from patchfrog.github.errors import WebhookPayloadError
from patchfrog.github.signatures import verify_signature
from patchfrog.github.webhooks import (
    parse_installation_event,
    parse_installation_repositories_event,
    parse_pull_request_event,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = structlog.get_logger(__name__)

#: GitHub's own webhook payloads are always well under 1 MB in practice;
#: this is a defensive ceiling against a forged/oversized request body,
#: never a real-world limit a legitimate delivery could hit.
_MAX_WEBHOOK_PAYLOAD_BYTES = 5 * 1024 * 1024


@router.post("/github")
async def receive_github_webhook(
    request: Request,
    settings: SettingsDep,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > _MAX_WEBHOOK_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    raw_body = await request.body()
    if len(raw_body) > _MAX_WEBHOOK_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

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
        pull_request_event = parse_pull_request_event(
            event_name=x_github_event, delivery_id=x_github_delivery, payload=payload
        )
        installation_event = parse_installation_event(
            event_name=x_github_event, delivery_id=x_github_delivery, payload=payload
        )
        installation_repositories_event = parse_installation_repositories_event(
            event_name=x_github_event, delivery_id=x_github_delivery, payload=payload
        )
    except WebhookPayloadError as exc:
        logger.warning(
            "webhook_payload_invalid", github_delivery_id=x_github_delivery, error=str(exc)
        )
        raise HTTPException(status_code=400, detail="malformed webhook payload") from exc

    if installation_event is not None:
        sync_installation_event_task.delay(
            delivery_id=installation_event.delivery_id,
            action=installation_event.action.value,
            github_installation_id=installation_event.installation.id,
            account_login=installation_event.account.login,
            account_type=installation_event.account.account_type,
        )
        logger.info(
            "installation_event_queued",
            github_delivery_id=installation_event.delivery_id,
            github_installation_id=installation_event.installation.id,
            action=installation_event.action.value,
        )
        return JSONResponse(status_code=202, content={"detail": "queued"})

    if installation_repositories_event is not None:
        sync_installation_repositories_event_task.delay(
            delivery_id=installation_repositories_event.delivery_id,
            action=installation_repositories_event.action.value,
            github_installation_id=installation_repositories_event.installation.id,
            account_login=installation_repositories_event.account.login,
            account_type=installation_repositories_event.account.account_type,
            repositories_added=[
                {"id": r.github_repository_id, "full_name": r.full_name}
                for r in installation_repositories_event.repositories_added
            ],
            repositories_removed=[
                {"id": r.github_repository_id, "full_name": r.full_name}
                for r in installation_repositories_event.repositories_removed
            ],
        )
        logger.info(
            "installation_repositories_event_queued",
            github_delivery_id=installation_repositories_event.delivery_id,
            github_installation_id=installation_repositories_event.installation.id,
            action=installation_repositories_event.action.value,
        )
        return JSONResponse(status_code=202, content={"detail": "queued"})

    if pull_request_event is None:
        logger.info(
            "webhook_event_ignored",
            github_delivery_id=x_github_delivery,
            github_event=x_github_event,
        )
        return JSONResponse(status_code=200, content={"detail": "ignored"})

    event = pull_request_event
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
